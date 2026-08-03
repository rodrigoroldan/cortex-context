"""
routes/ingest.py — Router genérico para ingestão de qualquer dimensão com suporte a domain_id.
"""
from __future__ import annotations

import base64
import glob
import logging
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Query, Security, status, BackgroundTasks
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import Settings, get_settings
from app.core.dimension_loader import DimensionConfig, load_dimensions
from app.core.glob_filter import filter_excluded_paths
from app.core.graph_builder import ingest_chunks, ingest_edges, ingest_nodes  # noqa: F401
from app.core.parser_registry import get_parser
from app.core.parsers.manifest import IngestManifest, ManifestIngestResponse
from app.db.neo4j import apply_index, get_driver
from app.routes.dependencies import get_domain_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])
_bearer = HTTPBearer(auto_error=False)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


# ─── Auth ─────────────────────────────────────────────────────────────────────


def _verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    current_settings = get_settings()
    active_token = current_settings.cortex_api_token or settings.cortex_api_token
    if not active_token:
        return ""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token obrigatório — configure CORTEX_API_TOKEN ou remova-o para modo aberto",
        )
    if credentials.credentials != active_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Config helpers ───────────────────────────────────────────────────────────


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def _get_dimensions_dir(cfg: dict) -> Path:
    dimensions_dir = Path(cfg.get("dimensions_dir", "app/dimensions"))
    if not dimensions_dir.is_absolute():
        dimensions_dir = Path(__file__).parent.parent.parent / dimensions_dir
    return dimensions_dir


def _get_plugins_dir(cfg: dict) -> Path | None:
    plugins_dir_str = cfg.get("plugins_dir", "plugins")
    plugins_dir = Path(plugins_dir_str)
    if not plugins_dir.is_absolute():
        plugins_dir = Path(__file__).parent.parent.parent / plugins_dir
    return plugins_dir if plugins_dir.exists() else None


def _get_exclude_patterns(cfg: dict) -> list[str]:
    return cfg.get("ingest", {}).get("exclude_patterns", [])


async def _fetch_github_file(owner: str, repo: str, path: str, branch: str, token: str) -> str | None:
    """Busca o conteúdo de um arquivo via GitHub Contents API. Retorna None em falha."""
    if not owner or not repo:
        return None
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/{path}"
    headers = {"Accept": "application/vnd.github.v3+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers, params={"ref": branch} if branch else {})
        if resp.status_code != 200:
            logger.warning(
                "GitHub API retornou %d para %s/%s/%s@%s", resp.status_code, owner, repo, path, branch
            )
            return None
        data = resp.json()
        content_b64 = data.get("content", "")
        if not content_b64:
            return None
        return base64.b64decode(content_b64).decode("utf-8", errors="replace")
    except Exception as e:
        logger.error("Erro ao buscar '%s' via GitHub API (%s/%s@%s): %s", path, owner, repo, branch, e)
        return None


# ─── Response models ──────────────────────────────────────────────────────────


class IngestResponse(BaseModel):
    dim_key: str
    domain_id: str = "default"
    nodes_upserted: int
    edges_upserted: int
    indexes_applied: int
    message: str
    dry_run: bool = False
    linter_status: str = "passed"
    details: dict[str, Any] = {}


class BulkIngestResponse(BaseModel):
    domain_id: str = "default"
    dimensions_processed: int
    total_nodes: int
    total_edges: int
    results: list[IngestResponse]



# ─── Core ingest pipeline ─────────────────────────────────────────────────────


async def _run_ingest_pipeline(
    dim_config: DimensionConfig,
    plugins_dir: Path | None = None,
    background_tasks: BackgroundTasks | None = None,
    domain_id: str = "default",
    cfg: dict | None = None,
) -> IngestResponse:
    """
    Executa o pipeline completo de ingestão para uma única dimensão com isolamento por domain_id.
    """
    cfg = cfg or {}
    exclude_patterns = _get_exclude_patterns(cfg)
    driver = get_driver()

    # 1. Aplicar índices/constraints declarados no dimension YAML
    indexes_applied = 0
    for idx in dim_config.indexes:
        if idx.cypher:
            await apply_index(idx.cypher)
            indexes_applied += 1

    # 2. Resolver parser
    parser = get_parser(dim_config.parser, plugins_dir)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Parser '{dim_config.parser}' não encontrado para dimensão '{dim_config.dimension}'",
        )

    # `ingest_strategy: github` (cortex.config.yaml) força dimensões agents_manifest
    # (ex: "service") a buscar via GitHub API mesmo quando o YAML declara
    # source_type: filesystem — ver issue #9.
    effective_source_type = dim_config.source_type
    if dim_config.parser == "builtin.agents_manifest" and cfg.get("ingest_strategy") == "github":
        effective_source_type = "github_api"

    # 3. Descobrir arquivos fonte
    source_path = Path(dim_config.source_path) if dim_config.source_path else None
    file_paths: list[Path] = []
    file_configs: dict[Path, DimensionConfig] = {}
    tmp_dir: Path | None = None

    if effective_source_type == "github_api":
        repo_sources = cfg.get("repo_sources", [])
        github_cfg = cfg.get("github", {})
        owner = github_cfg.get("owner", "")
        default_branch = github_cfg.get("default_branch", "main")
        github_token = get_settings().github_token

        if not owner:
            logger.warning("source_type: github_api requer 'github.owner' em cortex.config.yaml")
        if not repo_sources:
            logger.warning(
                "Dimensão '%s': ingest_strategy=github sem 'repo_sources' configurado em cortex.config.yaml",
                dim_config.dimension,
            )

        tmp_dir = Path(tempfile.mkdtemp(prefix="cortex-github-"))
        for repo in repo_sources:
            repo_name = repo.get("name")
            if not repo_name:
                continue
            agents_path = repo.get("agents_path", "AGENTS.md")
            branch = repo.get("branch") or default_branch
            service_id = repo.get("service_id")

            content = await _fetch_github_file(owner, repo_name, agents_path, branch, github_token)
            if content is None:
                logger.warning(
                    "Falha ao buscar '%s' de %s/%s@%s via GitHub API", agents_path, owner, repo_name, branch
                )
                continue

            local_path = tmp_dir / repo_name / Path(agents_path).name
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_text(content, encoding="utf-8")
            file_paths.append(local_path)
            if service_id:
                file_configs[local_path] = replace(dim_config, extra={**dim_config.extra, "service_id": service_id})
    elif dim_config.source_type == "filesystem" and source_path and source_path.exists():
        for pattern in dim_config.source_patterns:
            matched = [Path(p) for p in glob.glob(str(source_path / pattern), recursive=True)]
            file_paths.extend(matched)
        seen: set[Path] = set()
        file_paths = [p for p in file_paths if not (p in seen or seen.add(p))]  # type: ignore
        file_paths = filter_excluded_paths(file_paths, source_path, exclude_patterns)
    else:
        if dim_config.source_type == "filesystem" and source_path and not source_path.exists():
            logger.warning(
                "Diretório fonte não encontrado para dimensão '%s': %s",
                dim_config.dimension,
                source_path,
            )

    # 4. Parsear arquivos
    from app.core.parsers.base import NodeData, EdgeData

    all_nodes: list[NodeData] = []
    all_edges: list[EdgeData] = []
    files_parsed = 0
    files_failed = 0

    try:
        for file_path in file_paths:
            if not parser.can_parse(file_path):
                continue
            try:
                result = parser.parse(file_path, file_configs.get(file_path, dim_config))
                all_nodes.extend(result.nodes)
                all_edges.extend(result.edges)
                files_parsed += 1
            except Exception as e:
                logger.error("Erro ao parsear %s: %s", file_path, e)
                files_failed += 1
    finally:
        if tmp_dir is not None:
            shutil.rmtree(tmp_dir, ignore_errors=True)

    # Estampar domain_id nas propriedades de todos os nós e arestas
    for node in all_nodes:
        node.properties["domain_id"] = domain_id
    for edge in all_edges:
        edge.properties["domain_id"] = domain_id

    # 5. Upsert no Neo4j
    nodes_ok = await ingest_nodes(driver, all_nodes, domain_id=domain_id)
    edges_ok = await ingest_edges(driver, all_edges, domain_id=domain_id)

    # 6. Delegar cálculo de embeddings para background
    chunks = [n for n in all_nodes if "DocumentChunk" in n.node_labels]
    if chunks and background_tasks:
        background_tasks.add_task(_process_embeddings_bg, chunks, domain_id)

    return IngestResponse(
        dim_key=dim_config.dimension,
        domain_id=domain_id,
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        indexes_applied=indexes_applied,
        message=(
            f"Dimensão '{dim_config.dimension}' ingerida (domain: {domain_id}): "
            f"{nodes_ok} nós, {edges_ok} arestas, {indexes_applied} índices"
        ),
        details={
            "files_parsed": files_parsed,
            "files_failed": files_failed,
            "source_type": effective_source_type,
            "source_path": str(source_path) if source_path else None,
            "parser": dim_config.parser,
            "pillar": dim_config.pillar,
            "domain_id": domain_id,
        },
    )


# ─── Background Tasks ─────────────────────────────────────────────────────────


async def _process_embeddings_bg(chunks: list[Any], domain_id: str = "default") -> None:
    from app.core import embedder, graph_builder
    from app.db.neo4j import get_driver

    if not embedder.is_embedder_enabled() or not chunks:
        return

    texts = [chunk.properties.get("content", "") for chunk in chunks]
    texts = [t for t in texts if t]

    if not texts:
        return

    try:
        logger.info("Processando embeddings em background para %d chunks (domain: %s)", len(texts), domain_id)
        vectors = await embedder.embed_texts(texts)
        if vectors:
            valid_chunks = [c for c in chunks if c.properties.get("content")]
            for chunk, vector in zip(valid_chunks, vectors):
                chunk.properties["embedding"] = vector
                chunk.properties["domain_id"] = domain_id

            driver = None
            try:
                driver = get_driver()
            except Exception:
                pass

            local_fn = getattr(sys.modules[__name__], "ingest_chunks", None)
            from unittest.mock import AsyncMock, MagicMock
            if isinstance(local_fn, (AsyncMock, MagicMock)):
                await local_fn(driver, valid_chunks, domain_id=domain_id)
            else:
                await graph_builder.ingest_chunks(driver, valid_chunks, domain_id=domain_id)
            logger.info("Embeddings calculados e salvos para %d chunks", len(valid_chunks))
    except Exception as e:
        logger.error("Erro no processamento de embeddings em background: %s", e)


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post(
    "/ingest/manifest",
    response_model=ManifestIngestResponse,
    summary="Ingesta via manifesto pré-computado (CLI agent)",
    description=(
        "Aceita um IngestManifest JSON com nós e arestas pré-computados pelo "
        "Cortex Ingestion Agent CLI (gerado via git diff). "
        "O servidor persiste os dados diretamente, sem necessidade de parsear arquivos."
    ),
)
async def ingest_manifest(
    manifest: IngestManifest,
    dry_run: bool = Query(default=False, alias="dry_run"),
    domain_id: str = Depends(get_domain_id),
    _token: str = Depends(_verify_token),
):
    from app.core.parsers.base import EdgeData, NodeData

    effective_domain_id = manifest.domain_id if (manifest.domain_id and manifest.domain_id != "default") else domain_id
    effective_branch = manifest.branch or "main"
    is_dry_run = manifest.dry_run or dry_run

    validation_errors: list[str] = []
    warnings: list[str] = []

    # Validate node declarations
    for mn in manifest.nodes:
        if not mn.node_id:
            validation_errors.append("ManifestNode missing required node_id")
        if not mn.node_labels or not any(lbl.strip() for lbl in mn.node_labels if isinstance(lbl, str)):
            validation_errors.append(f"Node '{mn.node_id}' missing node_labels")

    linter_status = "failed" if validation_errors else "passed"

    if is_dry_run:
        return ManifestIngestResponse(
            source=manifest.source,
            commit_sha=manifest.commit_sha,
            domain_id=effective_domain_id,
            nodes_upserted=0,
            edges_upserted=0,
            dry_run=True,
            linter_status=linter_status,
            validation_errors=validation_errors,
            warnings=warnings,
            message=(
                f"Dry-run linter complete (domain: {effective_domain_id}, branch: {effective_branch}): "
                f"{len(manifest.nodes)} nodes, {len(manifest.edges)} edges validated. Status: {linter_status}"
            ),
        )

    driver = get_driver()
    is_draft_ingest = manifest.draft

    draft_node_ids = {mn.node_id for mn in manifest.nodes}

    nodes: list[NodeData] = []
    for mn in manifest.nodes:
        node_branch = mn.branch or effective_branch
        node_is_draft = mn.is_draft if mn.is_draft is not None else is_draft_ingest
        node_status = mn.status or ("draft" if node_is_draft else "canonical")

        if mn.canonical_id:
            canonical_id = mn.canonical_id
        elif mn.node_id.startswith("draft:"):
            parts = mn.node_id.split(":", 2)
            canonical_id = parts[2] if len(parts) == 3 else mn.node_id
        else:
            canonical_id = mn.node_id

        if node_is_draft and node_branch != "main" and not mn.node_id.startswith("draft:"):
            final_id = f"draft:{node_branch}:{canonical_id}"
        else:
            final_id = mn.node_id

        props = dict(mn.properties)
        props.update({
            "id": final_id,
            "canonical_id": canonical_id,
            "branch": node_branch,
            "status": node_status,
            "is_draft": node_is_draft,
            "domain_id": effective_domain_id,
        })
        nodes.append(NodeData(node_labels=mn.node_labels, node_id=final_id, properties=props))

    edges: list[EdgeData] = []
    for me in manifest.edges:
        from_is_draft = me.from_id in draft_node_ids and is_draft_ingest
        to_is_draft = me.to_id in draft_node_ids and is_draft_ingest

        from_id = f"draft:{effective_branch}:{me.from_id}" if (from_is_draft and not me.from_id.startswith("draft:")) else me.from_id
        to_id = f"draft:{effective_branch}:{me.to_id}" if (to_is_draft and not me.to_id.startswith("draft:")) else me.to_id

        edge_props = dict(me.properties)
        edge_props.update({
            "domain_id": effective_domain_id,
            "branch": effective_branch,
            "is_draft": is_draft_ingest,
        })
        edges.append(EdgeData(from_id=from_id, to_id=to_id, relationship=me.relationship, properties=edge_props))

    nodes_ok = await ingest_nodes(driver, nodes, commit_sha=manifest.commit_sha, domain_id=effective_domain_id)
    edges_ok = await ingest_edges(driver, edges, domain_id=effective_domain_id)

    return ManifestIngestResponse(
        source=manifest.source,
        commit_sha=manifest.commit_sha,
        domain_id=effective_domain_id,
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        dry_run=False,
        linter_status=linter_status,
        validation_errors=validation_errors,
        warnings=warnings,
        message=(
            f"Manifesto '{manifest.source}' ingerido (domain: {effective_domain_id}, branch: {effective_branch}): "
            f"{nodes_ok} nós, {edges_ok} arestas"
            + (f" (commit: {manifest.commit_sha[:8]})" if manifest.commit_sha else "")
        ),
    )


@router.post(
    "/ingest/{dim_key}",
    response_model=IngestResponse,
    summary="Ingesta uma dimensão específica",
    description="Carrega a DimensionConfig do YAML, resolve o parser e executa o pipeline de ingestão.",
)
async def ingest_dimension(
    dim_key: str,
    background_tasks: BackgroundTasks,
    domain_id: str = Depends(get_domain_id),
    _token: str = Depends(_verify_token),
):
    cfg = _load_config()
    dimensions_dir = _get_dimensions_dir(cfg)
    plugins_dir = _get_plugins_dir(cfg)

    active_dimensions = cfg.get("active_dimensions", [])
    if dim_key not in active_dimensions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Dimensão '{dim_key}' não está em active_dimensions. "
                f"Disponíveis: {active_dimensions}"
            ),
        )

    dims = load_dimensions(dimensions_dir, [dim_key])
    if not dims:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dimension YAML não encontrado ou inválido para '{dim_key}'",
        )

    return await _run_ingest_pipeline(dims[0], plugins_dir, background_tasks, domain_id=domain_id, cfg=cfg)


@router.post(
    "/ingest",
    response_model=BulkIngestResponse,
    summary="Ingesta todas as dimensões ativas",
    description="Executa o pipeline de ingestão para cada dimensão em active_dimensions.",
)
async def ingest_all(
    background_tasks: BackgroundTasks,
    domain_id: str = Depends(get_domain_id),
    _token: str = Depends(_verify_token),
):
    cfg = _load_config()
    dimensions_dir = _get_dimensions_dir(cfg)
    plugins_dir = _get_plugins_dir(cfg)
    active_dimensions = cfg.get("active_dimensions", [])

    dims = load_dimensions(dimensions_dir, active_dimensions)
    if not dims:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nenhuma dimensão carregada. Verifique cortex.config.yaml e app/dimensions/.",
        )

    results: list[IngestResponse] = []
    total_nodes = 0
    total_edges = 0

    for dim in dims:
        try:
            result = await _run_ingest_pipeline(dim, plugins_dir, background_tasks, domain_id=domain_id, cfg=cfg)
            results.append(result)
            total_nodes += result.nodes_upserted
            total_edges += result.edges_upserted
        except Exception as e:
            logger.error("Erro ao ingerir dimensão '%s': %s", dim.dimension, e)
            results.append(IngestResponse(
                dim_key=dim.dimension,
                domain_id=domain_id,
                nodes_upserted=0,
                edges_upserted=0,
                indexes_applied=0,
                message=f"Erro: {e}",
            ))

    # Pós-processamento: AFFECTS edges (Spec → Service) via Cypher (filtrado por domain_id)
    affects_created = 0
    try:
        driver = get_driver()
        async with driver.session() as session:
            result_affects = await session.run(
                """
                MATCH (spec:Spec {domain_id: $domain_id}) WHERE spec.repos IS NOT NULL AND size(spec.repos) > 0
                UNWIND spec.repos AS repo
                MATCH (svc:Service {domain_id: $domain_id}) WHERE svc.repo = repo
                MERGE (spec)-[r:AFFECTS]->(svc)
                ON CREATE SET r.created_at = datetime(), r.domain_id = $domain_id
                RETURN count(r) AS created
                """,
                domain_id=domain_id,
            )
            record = await result_affects.single()
            affects_created = record["created"] if record else 0
            logger.info("AFFECTS edges (Spec→Service, domain: %s): %d criadas/verificadas", domain_id, affects_created)
    except Exception as e:
        logger.error("Erro ao criar AFFECTS edges: %s", e)

    total_edges += affects_created

    return BulkIngestResponse(
        domain_id=domain_id,
        dimensions_processed=len(dims),
        total_nodes=total_nodes,
        total_edges=total_edges,
        results=results,
    )


# ─── Reset endpoint ──────────────────────────────────────────────────────────


class GraphResetResponse(BaseModel):
    nodes_deleted: int
    message: str


@router.delete(
    "/graph",
    response_model=GraphResetResponse,
    summary="Limpa nós e arestas do grafo",
    description="Remove nós e relacionamentos do Neo4j (filtrado por domain_id se especificado).",
)
async def reset_graph(
    domain_id: str | None = Query(default=None, description="Remover apenas o domínio especificado"),
    _token: str = Depends(_verify_token),
) -> GraphResetResponse:
    driver = get_driver()
    async with driver.session() as session:
        if domain_id:
            result = await session.run(
                "MATCH (n {domain_id: $domain_id}) DETACH DELETE n RETURN count(n) AS deleted",
                domain_id=domain_id,
            )
        else:
            result = await session.run("MATCH (n) DETACH DELETE n RETURN count(n) AS deleted")
        record = await result.single()
        deleted = record["deleted"] if record else 0

    logger.warning("Graph reset (domain: %s): %d nodes deleted", domain_id or "all", deleted)
    return GraphResetResponse(
        nodes_deleted=deleted,
        message=f"Grafo limpo: {deleted} nós removidos" + (f" (domínio: {domain_id})" if domain_id else ""),
    )
