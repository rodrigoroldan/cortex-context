"""
ingestor/spec_pipeline.py — Ingestão da dimensão spec no modo "spec = pasta" (group_by: numbered_folder).

Duas fontes alimentam o mesmo nó (:Spec:Intent) `spec-NNN`:

  - pastas de spec no filesystem (app/core/spec_folders.py): title/summary/status/frontmatter vêm do
    arquivo primário, `files`/`source_folder`/`has_folder` descrevem a pasta, e cada .md gera chunks;
  - issues do GitHub (app/core/spec_issues.py), opcionais: issue_* + chunks do corpo; título, summary e
    status só são definidos pela issue quando o nó NÃO tem pasta.

Dois modos, escolhidos pelo corpo de `POST /api/v1/ingest/spec`:

  - completo (sem corpo, ou corpo vazio): todas as pastas + todas as issues, seguido de PODA do que
    sumiu da fonte (nós folder-origin que não foram regravados, issues que perderam o label, chunks
    velhos);
  - incremental (`spec_paths` e/ou `issue_numbers`): reparse só das pastas afetadas (chunks velhos da
    pasta são apagados antes do upsert; pasta removida do disco tem chunks e nó apagados) e busca só
    das issues pedidas. Não faz a varredura de poda: só remove o que o próprio pedido cobre (pastas
    listadas que sumiram do disco, issues listadas que perderam o label).

Ordem: remoções de pasta -> issues -> pastas (-> poda, no completo). A regra "pasta manda" é aplicada
na própria escrita da issue (ver SpecGraphStore.upsert_issue_nodes), então não depende da ordem.

Salvaguardas contra apagar o grafo por engano: sem diretório fonte (ex: volume não montado) ou sem
nenhuma pasta encontrada, nenhuma remoção/poda acontece; falha do GitHub nunca poda dados de issue;
pasta que falhou no parse não é podada.
"""
from __future__ import annotations

import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated, Any

import httpx
from pydantic import BaseModel, Field

from app.core.dimension_loader import DimensionConfig
from app.core.embedder import is_embedder_enabled
from app.core.graph_builder import ingest_nodes
from app.core.parsers.base import EdgeData, NodeData
from app.core.parsers.builtin.markdown_frontmatter import MarkdownFrontmatterParser
from app.core.spec_folders import (
    SpecFolder,
    SpecFolderParse,
    discover_spec_folders,
    extract_spec_folder_name,
    parse_spec_folder,
)
from app.core.spec_issues import (
    MappedIssue,
    SpecIssuesConfig,
    SpecIssuesError,
    fetch_issues_by_number,
    fetch_spec_issues,
    load_spec_issues_config,
    map_issue,
)

logger = logging.getLogger(__name__)

SPEC_LABEL = "Spec"
SPEC_PILLAR = "Intent"

# Pastas parseadas/gravadas por lote (limita memória e o tamanho de cada transação).
FOLDER_BATCH = 25
# Tamanho máximo de listas passadas em `IN $ids` numa única query.
ID_BATCH = 500


# ─── Contrato de entrada ──────────────────────────────────────────────────────


class SpecIngestRequest(BaseModel):
    """Corpo opcional de POST /api/v1/ingest/spec."""

    spec_paths: list[Annotated[str, Field(max_length=1024)]] = Field(
        default_factory=list,
        max_length=5000,
        description=(
            "Arquivos alterados, relativos ao repo (ex: 'specs/861-x/RUNBOOK.md') ou à pasta de specs "
            "(ex: '861-x/spec.md'). Vale o segmento `\\d{3,}-...` mais próximo do arquivo."
        ),
    )
    issue_numbers: list[Annotated[int, Field(gt=0)]] = Field(
        default_factory=list,
        max_length=1000,
        description="Números das issues do GitHub a (re)ingerir (requer spec_issues.repo configurado).",
    )

    @property
    def is_incremental(self) -> bool:
        return bool(self.spec_paths or self.issue_numbers)


@dataclass
class SpecIngestResult:
    nodes_upserted: int = 0
    edges_upserted: int = 0
    details: dict[str, Any] = field(default_factory=dict)
    # Chunks a embedar em background (só preenchido com o embedder habilitado).
    embedding_chunks: list[NodeData] = field(default_factory=list)


# ─── Persistência (Neo4j) ─────────────────────────────────────────────────────

_UPSERT_ISSUES = """
UNWIND $rows AS row
MERGE (n:Spec {id: row.id, domain_id: $domain_id})
SET n:Intent
SET n += row.issue_props
FOREACH (_ IN CASE WHEN coalesce(n.has_folder, false) THEN [] ELSE [1] END |
    SET n += row.doc_props
)
SET n.canonical_id = coalesce(n.canonical_id, row.id),
    n.branch = coalesce(n.branch, 'main'),
    n.is_draft = coalesce(n.is_draft, false),
    n.ingested_at = $ingested_at,
    n.valid_from = coalesce(n.valid_from, $ingested_at)
RETURN count(n) AS affected
"""

# Referências cruzadas entre specs (DEPENDS_ON, SUPERSEDES, ...). Com os dois lados rotulados o Neo4j usa o
# índice (domain_id, id); o upsert genérico (ingest_edges) casa nós sem label e varre o grafo inteiro a cada
# linha — o que pesa depois que os chunks multiplicam o número de nós. O tipo da relação não é parametrizável.
_LINK_SPECS = """
UNWIND $rows AS row
MATCH (a:Spec {id: row.from_id, domain_id: $domain_id})
MATCH (b:Spec {id: row.to_id, domain_id: $domain_id})
MERGE (a)-[r:%s]->(b)
SET r.domain_id = $domain_id, r.branch = 'main', r.is_draft = false
RETURN count(r) AS affected
"""
_REL_TYPE = re.compile(r"^[A-Z][A-Z0-9_]*$")

_LINK_CHUNKS = """
UNWIND $rows AS row
MATCH (c:DocumentChunk {id: row.chunk_id, domain_id: $domain_id})
MATCH (p:Spec {id: row.parent_id, domain_id: $domain_id})
MERGE (c)-[r:CHUNK_OF]->(p)
SET r.domain_id = $domain_id, r.branch = 'main', r.is_draft = false
RETURN count(r) AS affected
"""

# Chunks de origem folder do pai. Inclui os chunks legados do parser antigo (sem chunk_source, id no
# formato "{pai}__chunk_{i}"), que duplicariam o conteúdo da pasta.
_DELETE_FOLDER_CHUNKS = """
MATCH (c:DocumentChunk {domain_id: $domain_id})
WHERE c.parent_id IN $parent_ids
  AND (c.chunk_source = 'folder'
       OR (c.chunk_source IS NULL AND c.id = c.parent_id + '__chunk_' + toString(c.chunk_index)))
DETACH DELETE c
RETURN count(c) AS affected
"""

_DELETE_CHUNKS_OF = """
MATCH (c:DocumentChunk {domain_id: $domain_id})
WHERE c.parent_id IN $parent_ids AND ($source IS NULL OR c.chunk_source = $source)
DETACH DELETE c
RETURN count(c) AS affected
"""

_PURGE_STALE_CHUNKS = """
MATCH (c:DocumentChunk {domain_id: $domain_id})
WHERE c.chunk_source = $source AND c.ingested_at < $started_at
DETACH DELETE c
RETURN count(c) AS affected
"""

_PURGE_LEGACY_CHUNKS = """
MATCH (p:Spec {domain_id: $domain_id})
WHERE p.has_folder = true
MATCH (c:DocumentChunk {domain_id: $domain_id, parent_id: p.id})
WHERE c.chunk_source IS NULL AND c.id = p.id + '__chunk_' + toString(c.chunk_index)
DETACH DELETE c
RETURN count(c) AS affected
"""

_DELETE_NODES = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.id IN $ids
DETACH DELETE n
RETURN count(n) AS affected
"""

_FIND_BY_FOLDER = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.source_folder IN $names
RETURN n.id AS id, coalesce(n.has_issue, false) AS has_issue, n.issue_number AS issue_number
"""

_LIST_FOLDER_NAMES = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.has_folder = true AND n.source_folder IS NOT NULL
RETURN DISTINCT n.source_folder AS folder
"""

_STRIP_FOLDER = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.id IN $ids
REMOVE n.has_folder, n.files, n.files_str, n.source_folder, n.primary_file, n.file_path
RETURN count(n) AS affected
"""

# Nós de origem folder (ou legados, do parser por arquivo: file_path sob a raiz de specs) sem dados
# de issue que NÃO foram regravados nesta execução.
_FIND_PRUNABLE = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE (n.has_folder = true OR n.file_path STARTS WITH $root_prefix)
  AND coalesce(n.has_issue, false) = false
  AND n.ingested_at < $started_at
  AND NOT (coalesce(n.source_folder, '') IN $skip_folders)
RETURN n.id AS id
"""

_FIND_ISSUE_NODES_NOT_IN = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.has_issue = true AND NOT (n.id IN $keep_ids)
RETURN n.id AS id, coalesce(n.has_folder, false) AS has_folder
"""

_FIND_ISSUE_NODES_BY_NUMBER = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.issue_number = $number AND ($exclude_id IS NULL OR n.id <> $exclude_id)
RETURN n.id AS id, coalesce(n.has_folder, false) AS has_folder
"""

_STRIP_ISSUE = """
MATCH (n:Spec {domain_id: $domain_id})
WHERE n.id IN $ids
REMOVE n.has_issue, n.issue_number, n.issue_url, n.issue_state, n.issue_state_reason, n.issue_updated_at
RETURN count(n) AS affected
"""


def _batches(items: list, size: int) -> list[list]:
    return [items[i : i + size] for i in range(0, len(items), size)]


class SpecGraphStore:
    """
    Operações de grafo da ingestão de specs, sempre restritas a `domain_id`.

    Fica separada da orquestração para que esta seja testável com um store em memória.
    """

    def __init__(self, driver: Any, domain_id: str = "default"):
        self.driver = driver
        self.domain_id = domain_id or "default"

    # -- helpers -------------------------------------------------------------

    async def _count(self, cypher: str, **params: Any) -> int:
        async with self.driver.session() as session:
            result = await session.run(cypher, {"domain_id": self.domain_id, **params})
            record = await result.single()
        try:
            return int(record["affected"] or 0) if record else 0
        except (KeyError, TypeError, ValueError):
            return 0

    async def _rows(self, cypher: str, **params: Any) -> list[dict]:
        async with self.driver.session() as session:
            result = await session.run(cypher, {"domain_id": self.domain_id, **params})
            data = await result.data()
        return list(data or [])

    async def _count_batched(self, cypher: str, ids: list[str], key: str, **params: Any) -> int:
        total = 0
        for batch in _batches(list(dict.fromkeys(ids)), ID_BATCH):
            total += await self._count(cypher, **{key: batch}, **params)
        return total

    # -- escrita -------------------------------------------------------------

    async def upsert_spec_nodes(self, nodes: list[NodeData]) -> int:
        return await ingest_nodes(self.driver, nodes, domain_id=self.domain_id)

    async def upsert_edges(self, edges: list[EdgeData]) -> int:
        """Arestas Spec -> Spec. Alvos inexistentes (ex: "depends on" de uma palavra qualquer) são ignorados."""
        by_type: dict[str, list[dict]] = defaultdict(list)
        for e in edges:
            if _REL_TYPE.match(e.relationship):
                by_type[e.relationship].append({"from_id": e.from_id, "to_id": e.to_id})
            else:
                logger.warning("Spec: tipo de relação inválido ignorado: %r", e.relationship)
        total = 0
        for rel_type, rows in by_type.items():
            for batch in _batches(rows, ID_BATCH):
                try:
                    total += await self._count(_LINK_SPECS % rel_type, rows=batch)
                except Exception as e:  # uma relação com problema não derruba a ingestão
                    logger.error("Spec: erro ao gravar %d arestas %s: %s", len(batch), rel_type, e)
        return total

    async def upsert_chunks(self, chunks: list[NodeData]) -> int:
        """Grava os DocumentChunk e liga cada um ao pai por CHUNK_OF (query com labels: usa índice)."""
        if not chunks:
            return 0
        count = await ingest_nodes(self.driver, chunks, domain_id=self.domain_id)
        rows = [{"chunk_id": c.node_id, "parent_id": c.properties.get("parent_id")} for c in chunks]
        for batch in _batches(rows, ID_BATCH):
            await self._count(_LINK_CHUNKS, rows=batch)
        return count

    async def upsert_issue_nodes(self, mapped: list[MappedIssue]) -> int:
        """
        MERGE do nó Spec com os dados da issue. issue_* sempre; title/summary/status/labels só quando
        o nó não tem pasta (has_folder) — decisão tomada no próprio Cypher, atômica por nó.
        """
        if not mapped:
            return 0
        rows = [{"id": m.node_id, "issue_props": m.issue_props, "doc_props": m.doc_props} for m in mapped]
        total = 0
        for batch in _batches(rows, ID_BATCH):
            total += await self._count(_UPSERT_ISSUES, rows=batch, ingested_at=datetime.now(tz=timezone.utc))
        return total

    # -- remoção -------------------------------------------------------------

    async def delete_folder_chunks(self, parent_ids: list[str]) -> int:
        return await self._count_batched(_DELETE_FOLDER_CHUNKS, parent_ids, "parent_ids")

    async def delete_chunks(self, parent_ids: list[str], source: str | None = None) -> int:
        return await self._count_batched(_DELETE_CHUNKS_OF, parent_ids, "parent_ids", source=source)

    async def delete_nodes(self, ids: list[str]) -> int:
        """Apaga os nós Spec e TODOS os seus chunks (qualquer origem)."""
        if not ids:
            return 0
        await self.delete_chunks(ids, None)
        return await self._count_batched(_DELETE_NODES, ids, "ids")

    async def strip_folder_data(self, ids: list[str]) -> int:
        """Rebaixa nós que perderam a pasta (mas têm issue) para "só-issue"."""
        return await self._count_batched(_STRIP_FOLDER, ids, "ids")

    async def strip_issue_data(self, ids: list[str]) -> int:
        return await self._count_batched(_STRIP_ISSUE, ids, "ids")

    async def purge_stale_chunks(self, source: str, started_at: datetime) -> int:
        return await self._count(_PURGE_STALE_CHUNKS, source=source, started_at=started_at)

    async def purge_legacy_chunks(self) -> int:
        return await self._count(_PURGE_LEGACY_CHUNKS)

    # -- consulta ------------------------------------------------------------

    async def list_folder_names(self) -> set[str]:
        return {r["folder"] for r in await self._rows(_LIST_FOLDER_NAMES) if r.get("folder")}

    async def find_nodes_by_folder(self, names: list[str]) -> list[dict]:
        if not names:
            return []
        return await self._rows(_FIND_BY_FOLDER, names=names)

    async def find_prunable_folder_nodes(self, started_at: datetime, root_prefix: str, skip_folders: list[str]) -> list[str]:
        rows = await self._rows(
            _FIND_PRUNABLE, started_at=started_at, root_prefix=root_prefix, skip_folders=skip_folders
        )
        return [r["id"] for r in rows if r.get("id")]

    async def find_issue_nodes_not_in(self, keep_ids: list[str]) -> list[dict]:
        return await self._rows(_FIND_ISSUE_NODES_NOT_IN, keep_ids=keep_ids)

    async def find_issue_nodes_by_number(self, number: int, exclude_id: str | None = None) -> list[dict]:
        return await self._rows(_FIND_ISSUE_NODES_BY_NUMBER, number=number, exclude_id=exclude_id)


# ─── Orquestração ─────────────────────────────────────────────────────────────


@dataclass
class _Stats:
    folders_parsed: int = 0
    folders_deleted: int = 0
    folders_failed: int = 0
    chunks_upserted: int = 0
    issues_parsed: int = 0
    issues_error: str | None = None
    ignored_paths: list[str] = field(default_factory=list)
    pruned_nodes: int = 0
    files_parsed: int = 0
    files_failed: int = 0
    nodes_upserted: int = 0
    edges_upserted: int = 0
    warnings: list[str] = field(default_factory=list)
    duplicate_folders: list[str] = field(default_factory=list)


async def _drop_issue_data(store: SpecGraphStore, rows: list[dict]) -> int:
    """
    Remove os dados de issue de nós cuja issue sumiu (perdeu o label, foi apagada, mudou de NNN).
    Nó que também tem pasta só perde issue_*; nó só-issue é apagado (com os chunks). Retorna os apagados.
    """
    if not rows:
        return 0
    ids = [r["id"] for r in rows]
    await store.delete_chunks(ids, "issue")
    with_folder = [r["id"] for r in rows if r.get("has_folder")]
    only_issue = [r["id"] for r in rows if not r.get("has_folder")]
    if with_folder:
        await store.strip_issue_data(with_folder)
    if only_issue:
        await store.delete_nodes(only_issue)
    return len(only_issue)


async def _release_folders(
    store: SpecGraphStore, names: list[str], *, delete_orphans: bool, stats: _Stats
) -> list[int]:
    """
    Trata pastas que não existem mais no disco.

    Os chunks de folder dos nós afetados são sempre apagados. Nós sem dados de issue são apagados quando
    `delete_orphans` (incremental; no completo ficam para a poda). Nós com issue perdem só os dados de
    pasta (viram "só-issue"); devolve os números dessas issues, para o chamador re-sincronizá-las
    (o título/summary/status voltam a ser os da issue).
    """
    nodes = await store.find_nodes_by_folder(names)
    if not nodes:
        return []
    with_issue = [n for n in nodes if n.get("has_issue")]
    without_issue = [n for n in nodes if not n.get("has_issue")]

    affected = [n["id"] for n in with_issue] + ([n["id"] for n in without_issue] if delete_orphans else [])
    await store.delete_folder_chunks(affected)
    if delete_orphans and without_issue:
        await store.delete_nodes([n["id"] for n in without_issue])
    if with_issue:
        await store.strip_folder_data([n["id"] for n in with_issue])

    stats.folders_deleted += len(names) if delete_orphans else len(with_issue)
    return [int(n["issue_number"]) for n in with_issue if n.get("issue_number") is not None]


async def _apply_issues(
    store: SpecGraphStore,
    issues: list,
    issues_cfg: SpecIssuesConfig,
    *,
    incremental: bool,
    stats: _Stats,
    embedding_chunks: list[NodeData],
) -> list[str]:
    """Grava as issues (nó + chunks do corpo). Devolve os ids dos nós gravados."""
    mapped: list[MappedIssue] = []
    seen: dict[str, int] = {}
    for issue in issues:
        m = map_issue(issue, issues_cfg)
        if m.node_id in seen:
            logger.warning(
                "Issue #%d resolve para %s, já ocupado pela issue #%d — ignorando a segunda",
                issue.number, m.node_id, seen[m.node_id],
            )
            continue
        seen[m.node_id] = issue.number
        mapped.append(m)
    if not mapped:
        return []

    if incremental:
        # Issue que mudou de NNN deixa um nó fantasma com o mesmo issue_number; chunks velhos saem antes.
        for m in mapped:
            await _drop_issue_data(store, await store.find_issue_nodes_by_number(m.issue_number, exclude_id=m.node_id))
        await store.delete_chunks([m.node_id for m in mapped], "issue")

    stats.nodes_upserted += await store.upsert_issue_nodes(mapped)
    chunks = [c for m in mapped for c in m.chunks]
    stats.chunks_upserted += await store.upsert_chunks(chunks)
    stats.issues_parsed += len(mapped)
    if is_embedder_enabled():
        embedding_chunks.extend(chunks)
    return [m.node_id for m in mapped]


async def run_spec_ingest(
    store: SpecGraphStore,
    dim_config: DimensionConfig,
    *,
    cfg: dict | None = None,
    request: SpecIngestRequest | None = None,
    github_token: str = "",
    exclude_patterns: list[str] | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> SpecIngestResult:
    """Executa a ingestão da dimensão spec (completa ou incremental). Ver docstring do módulo."""
    cfg = cfg or {}
    started_at = datetime.now(tz=timezone.utc)
    incremental = request is not None and request.is_incremental
    stats = _Stats()
    embedding_chunks: list[NodeData] = []

    # ── Fonte filesystem ──────────────────────────────────────────────────────
    root = Path(dim_config.source_path) if dim_config.source_path else None
    source_ok = bool(root and root.is_dir())
    folders: dict[str, SpecFolder] = {}
    if source_ok and root:
        folders = discover_spec_folders(root, dim_config.source_patterns, exclude_patterns)
    else:
        logger.warning("Spec: diretório fonte não encontrado: %s", dim_config.source_path)
        stats.warnings.append("diretório fonte de specs não encontrado: remoções e poda ignoradas")
    # Só mexemos em remoções/poda se o filesystem parece saudável (volume não montado = vazio).
    trusted = source_ok and bool(folders)
    if source_ok and not folders:
        stats.warnings.append("nenhuma pasta de spec encontrada: remoções e poda ignoradas")

    # ── Alvos ────────────────────────────────────────────────────────────────
    if incremental:
        assert request is not None
        names: list[str] = []
        for raw in request.spec_paths:
            name = extract_spec_folder_name(raw)
            if name is None:
                stats.ignored_paths.append(raw)
            elif name not in names:
                names.append(name)
        target_names = [n for n in names if n in folders]
        removed_names = [n for n in names if n not in folders]
    else:
        target_names = list(folders)
        removed_names = []

    # ── 1. Pastas removidas do disco ──────────────────────────────────────────
    demoted_issue_numbers: list[int] = []
    if incremental and removed_names:
        if trusted:
            demoted_issue_numbers = await _release_folders(store, removed_names, delete_orphans=True, stats=stats)
        else:
            stats.warnings.append(f"pastas ausentes não removidas do grafo: {', '.join(removed_names)}")
    elif not incremental and trusted:
        vanished = sorted(await store.list_folder_names() - set(folders))
        if vanished:
            demoted_issue_numbers = await _release_folders(store, vanished, delete_orphans=False, stats=stats)

    # ── 2. Issues do GitHub ───────────────────────────────────────────────────
    issues_ok = False
    kept_issue_ids: list[str] = []
    issues_cfg: SpecIssuesConfig | None = None
    try:
        issues_cfg = load_spec_issues_config(cfg)
        if issues_cfg is None:
            if incremental and request and request.issue_numbers:
                stats.issues_error = "spec_issues.repo não configurado: issue_numbers ignorados"
        elif incremental:
            assert request is not None
            wanted = list(dict.fromkeys([*request.issue_numbers, *demoted_issue_numbers]))
            if wanted:
                found, gone = await fetch_issues_by_number(issues_cfg, wanted, github_token, client=http_client)
                kept_issue_ids = await _apply_issues(
                    store, found, issues_cfg, incremental=True, stats=stats, embedding_chunks=embedding_chunks
                )
                for number in gone:
                    stats.pruned_nodes += await _drop_issue_data(store, await store.find_issue_nodes_by_number(number))
        else:
            all_issues = await fetch_spec_issues(issues_cfg, github_token, client=http_client)
            kept_issue_ids = await _apply_issues(
                store, all_issues, issues_cfg, incremental=False, stats=stats, embedding_chunks=embedding_chunks
            )
            issues_ok = True
    except SpecIssuesError as e:
        stats.issues_error = str(e)
        logger.warning("Spec: issues do GitHub não ingeridas: %s", e)

    # ── 3. Pastas: parse + upsert em lotes ────────────────────────────────────
    parser = MarkdownFrontmatterParser()
    failed_folders: list[str] = []
    all_edges: list[EdgeData] = []
    seen_ids: dict[str, str] = {}

    for batch_names in _batches(target_names, FOLDER_BATCH):
        parsed: list[SpecFolderParse] = []
        for name in batch_names:
            try:
                result = parse_spec_folder(folders[name], dim_config, parser)
            except Exception as e:  # uma pasta quebrada não derruba as demais
                logger.error("Spec: erro ao parsear a pasta %s: %s", name, e)
                result = None
            if result is None:
                failed_folders.append(name)
                continue
            node_id = result.node.node_id
            if node_id in seen_ids:
                logger.warning("Spec: pasta %s resolve para %s, já ocupado por %s — ignorando", name, node_id, seen_ids[node_id])
                stats.duplicate_folders.append(name)
                continue
            seen_ids[node_id] = name
            parsed.append(result)
            stats.files_parsed += result.files_parsed
            stats.files_failed += result.files_failed
        if not parsed:
            continue

        if incremental:
            # Arquivos removidos/encolhidos não podem deixar chunks velhos para trás.
            await store.delete_folder_chunks([p.node.node_id for p in parsed])

        chunks = [c for p in parsed for c in p.chunks]
        nodes_ok = await store.upsert_spec_nodes([p.node for p in parsed])
        chunks_ok = await store.upsert_chunks(chunks)
        stats.nodes_upserted += nodes_ok
        stats.chunks_upserted += chunks_ok
        if nodes_ok < len(parsed) or chunks_ok < len(chunks):
            # O upsert em lote engole erros do Neo4j (só loga). O que não foi regravado teria
            # ingested_at antigo e a poda o apagaria: trata o lote inteiro como falho.
            logger.error("Spec: gravação parcial no lote %s — pastas poupadas da poda", batch_names)
            failed_folders.extend(p.node.properties["source_folder"] for p in parsed)
            continue
        stats.folders_parsed += len(parsed)
        all_edges.extend(e for p in parsed for e in p.edges)
        if is_embedder_enabled():
            embedding_chunks.extend(chunks)

    stats.folders_failed = len(failed_folders)
    # Referências cruzadas só depois de TODOS os nós existirem (o alvo pode estar em outro lote).
    stats.edges_upserted += await store.upsert_edges(all_edges)

    # ── 4. Poda (só na ingestão completa) ─────────────────────────────────────
    if not incremental:
        if issues_ok:
            stale = await store.find_issue_nodes_not_in(kept_issue_ids)
            stats.pruned_nodes += await _drop_issue_data(store, stale)
            await store.purge_stale_chunks("issue", started_at)
        if trusted:
            root_prefix = str(root).rstrip("/") + "/"
            prunable = await store.find_prunable_folder_nodes(started_at, root_prefix, failed_folders)
            await store.delete_nodes(prunable)
            stats.pruned_nodes += len(prunable)
            if failed_folders:
                stats.warnings.append("chunks velhos não podados: houve pastas com falha no parse")
            else:
                await store.purge_stale_chunks("folder", started_at)
                await store.purge_legacy_chunks()

    details: dict[str, Any] = {
        "mode": "incremental" if incremental else "full",
        "folders_parsed": stats.folders_parsed,
        "folders_deleted": stats.folders_deleted,
        "folders_failed": stats.folders_failed,
        "chunks_upserted": stats.chunks_upserted,
        "issues_parsed": stats.issues_parsed,
        "issues_error": stats.issues_error,
        "issues_enabled": issues_cfg is not None,
        "ignored_paths": stats.ignored_paths,
        "pruned_nodes": stats.pruned_nodes,
        "files_parsed": stats.files_parsed,
        "files_failed": stats.files_failed,
    }
    if stats.warnings:
        details["warnings"] = stats.warnings
    if stats.duplicate_folders:
        details["duplicate_folders"] = stats.duplicate_folders

    return SpecIngestResult(
        nodes_upserted=stats.nodes_upserted,
        edges_upserted=stats.edges_upserted,
        details=details,
        embedding_chunks=embedding_chunks,
    )
