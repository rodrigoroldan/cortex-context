"""
routes/query.py — Consulta semântica cross-dimension ao grafo Cortex Context com isolamento por domain_id.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from neo4j import Query
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver
from app.routes.dependencies import get_branch, get_domain_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
bearer = HTTPBearer(auto_error=False)

# Timeout (segundos) aplicado no servidor Neo4j para a expansão de vizinhos.
# Sem isso, uma combinação de muitos seeds + hops=2 pode rodar indefinidamente
# e a conexão HTTP cai como 502 genérico em vez de um erro claro (issue #33).
_EXPAND_QUERY_TIMEOUT_S = 10.0

# Timeout no nível do asyncio para a rota inteira (seed + expand). O timeout do
# Neo4j acima é aplicado no servidor, mas o driver async nem sempre propaga a
# própria falha de volta pro `await` (transação marcada "Terminated" no Neo4j
# sem o cliente Python nunca ser notificado — visto ao vivo via `SHOW TRANSACTIONS`
# em 2026-09-12). Este wait_for garante que a rota SEMPRE responde dentro do
# limite, mesmo se o driver ficar pendurado.
_ROUTE_TIMEOUT_S = 15.0


def _verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if not settings.cortex_api_token:
        return ""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token obrigatório")
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Response models ──────────────────────────────────────────────────────────


class NodeContext(BaseModel):
    """Nó genérico do grafo — qualquer dimensão/pilar."""
    id: str
    labels: list[str]         # ex: ["Spec", "Intent"]
    pillar: str               # ex: "Intent"
    properties: dict[str, Any]
    title: str = ""
    summary: str = ""
    status: str = ""
    # Só nos seeds achados pelo corpo (índice doc_chunk_fts): "<arquivo>: …trecho…".
    match_snippet: str | None = None


class EdgeContext(BaseModel):
    from_id: str
    to_id: str
    relationship: str


class SubgraphResponse(BaseModel):
    nodes: list[NodeContext]
    edges: list[EdgeContext]
    token_estimate: int
    query_meta: dict[str, Any] = {}


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _sanitize_props(props: dict) -> dict:
    result = {}
    for k, v in props.items():
        if hasattr(v, "iso_format"):
            result[k] = v.iso_format()
        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("neo4j"):
            result[k] = str(v)
        else:
            result[k] = v
    return result


def _neo4j_node_to_context(node_data: dict, labels: list[str]) -> NodeContext:
    pillar = node_data.get("pillar", "")
    if not pillar:
        for lbl in labels:
            if lbl in ("Intent", "System", "Implementation", "Runtime"):
                pillar = lbl
                break

    return NodeContext(
        id=node_data.get("id", ""),
        labels=labels,
        pillar=pillar,
        properties=_sanitize_props(dict(node_data)),
        title=str(node_data.get("title", "")),
        summary=str(node_data.get("summary", "")),
        status=str(node_data.get("status", "")),
    )


def _estimate_tokens(nodes: list[NodeContext]) -> int:
    total = sum(len(n.title) + len(n.summary) + len(n.id) + 20 for n in nodes)
    return total // 4


# Índice fulltext do corpo das specs (DocumentChunk.content) — criado em app/dimensions/spec.yaml.
_CHUNK_FTS_INDEX = "doc_chunk_fts"
_SNIPPET_CHARS = 240
_LUCENE_NOISE = re.compile(r'[+\-&|!(){}\[\]^"~*?:\\/]')


def _literal_keywords(keyword_list: list[str]) -> list[str]:
    """Keywords como o usuário digitou (minúsculas, sem aspas em volta), para casar por substring."""
    literals: list[str] = []
    for kw in keyword_list:
        lit = kw.strip().strip('"').strip().lower()
        if lit and lit not in literals:
            literals.append(lit)
    return literals


def _snippet_terms(keyword_list: list[str]) -> list[str]:
    """Termos para localizar no texto: cada keyword inteira e suas palavras (a busca fulltext casa por token)."""
    terms: list[str] = []
    for kw in keyword_list:
        cleaned = _LUCENE_NOISE.sub(" ", kw).strip()
        for term in [cleaned, *cleaned.split()]:
            if len(term) >= 2 and term.lower() not in {t.lower() for t in terms}:
                terms.append(term)
    return terms


def _first_position(lowered: str, terms: list[str]) -> int:
    """Menor índice em que algum dos termos aparece em `lowered` (-1 se nenhum)."""
    first = -1
    for term in terms:
        pos = lowered.find(term.lower())
        if pos != -1 and (first == -1 or pos < first):
            first = pos
    return first


def _build_match_snippet(content: str, source_file: str, keyword_list: list[str], width: int = _SNIPPET_CHARS) -> str:
    """
    "<source_file>: …<trecho>…" — ~`width` caracteres em volta da primeira ocorrência de uma keyword.

    A ocorrência LITERAL da keyword (ex: "api-staging") tem prioridade: o analyzer do fulltext quebra
    "https://api-staging.exemplo.dev" em tokens soltos, e ancorar o trecho em "api" mostraria um lugar
    qualquer do texto. Sem literal, tenta as palavras da keyword (sem operadores Lucene) e, sem
    ocorrência nenhuma, usa o início do texto.
    """
    text = re.sub(r"\s+", " ", content or "").strip()
    lowered = text.lower()  # lower() (e não casefold) para que os índices valham também em `text`
    first = _first_position(lowered, _literal_keywords(keyword_list))
    if first == -1:
        first = _first_position(lowered, _snippet_terms(keyword_list))
    start = max(first - width // 3, 0) if first != -1 else 0
    snippet = text[start : start + width].strip()
    prefix = f"{source_file}: " if source_file else ""
    return f"{prefix}…{snippet}…"


def _merge_nodes_priority(nodes: list[NodeContext], branch: str) -> list[NodeContext]:
    """
    Applies left-outer priority join: for entities with duplicate canonical_id or id,
    branch draft nodes take priority over main canonical nodes.
    """
    if branch == "main" or not nodes:
        return nodes

    entity_map: dict[str, NodeContext] = {}
    for node in nodes:
        raw_key = node.properties.get("canonical_id") or node.id
        if str(raw_key).startswith("draft:"):
            parts = str(raw_key).split(":", 2)
            canonical_key = parts[2] if len(parts) == 3 else str(raw_key)
        else:
            canonical_key = str(raw_key)

        node_branch = str(node.properties.get("branch", "main"))
        node_is_draft = bool(node.properties.get("is_draft", False)) or node.status == "draft" or node_branch == branch

        if canonical_key not in entity_map:
            entity_map[canonical_key] = node
        else:
            existing = entity_map[canonical_key]
            existing_branch = str(existing.properties.get("branch", "main"))
            if (node_branch == branch or node_is_draft) and not (existing_branch == branch):
                entity_map[canonical_key] = node

    return list(entity_map.values())


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/query",
    response_model=SubgraphResponse,
    summary="Busca semântica cross-dimension por keywords com isolamento por domain_id e branch",
)
async def query_context(
    keywords: str,
    limit: int = 8,
    hops: int = 1,
    pillar: str | None = None,
    dimension: str | None = None,
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> SubgraphResponse:
    driver = get_driver()
    keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    fts_query = " OR ".join(keyword_list)
    hops = min(max(hops, 1), 2)

    try:
        return await asyncio.wait_for(
            _run_query_context(
                driver, keyword_list, fts_query, limit, hops, pillar, dimension, domain_id, branch
            ),
            timeout=_ROUTE_TIMEOUT_S,
        )
    except asyncio.TimeoutError as e:
        logger.error(
            "Query context excedeu %ss (keywords=%s, hops=%d) — driver não respondeu a tempo",
            _ROUTE_TIMEOUT_S, keyword_list, hops,
        )
        raise HTTPException(
            status_code=status.HTTP_504_GATEWAY_TIMEOUT,
            detail="Consulta ao grafo excedeu o tempo limite — tente reduzir keywords/hops.",
        ) from e


async def _run_query_context(
    driver,
    keyword_list: list[str],
    fts_query: str,
    limit: int,
    hops: int,
    pillar: str | None,
    dimension: str | None,
    domain_id: str,
    branch: str,
) -> SubgraphResponse:
    async with driver.session() as session:
        # ── Seed: FTS cross-dimension ─────────────────────────────────────────
        fts_indexes = ["spec_fts", "service_fulltext", "workflow_fulltext"]
        all_seed_ids: list[str] = []
        seed_props: dict[str, dict] = {}

        for idx_name in fts_indexes:
            try:
                seed_result = await session.run(
                    f"""
                    CALL db.index.fulltext.queryNodes('{idx_name}', $query)
                    YIELD node, score
                    WHERE (node.domain_id = $domain_id OR ($domain_id = 'default' AND node.domain_id IS NULL))
                      AND (node.branch = $branch OR node.branch = 'main' OR node.branch IS NULL OR node.is_draft = false)
                    RETURN node {{.*}} AS props,
                           labels(node) AS labels,
                           score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    {"query": fts_query, "limit": limit, "domain_id": domain_id, "branch": branch},
                )
                records = await seed_result.data()
                for r in records:
                    props = r.get("props") or {}
                    nid = props.get("id", "")
                    node_labels: list[str] = r.get("labels") or []
                    if not nid:
                        continue
                    if "__chunk_" in nid:
                        continue
                    if pillar and props.get("pillar", "") != pillar:
                        if not any(lbl == pillar for lbl in node_labels):
                            continue
                    if nid not in all_seed_ids:
                        all_seed_ids.append(nid)
                        seed_props[nid] = {"props": props, "labels": node_labels}
            except Exception:
                pass

        # ── Seed: corpo das specs (chunks) ─────────────────────────────────────
        # Roda SEMPRE: os 3 índices de título costumam encher `limit` sozinhos, e o corpo não pode
        # depender de sobra. Cada chunk achado vira seed do seu pai (se ainda não for seed), até
        # max(2, limit // 3) pais de corpo ALÉM dos seeds de título — o total pode passar de `limit`.
        # Cada seed de corpo guarda o trecho que casou.
        body_snippets: dict[str, str] = {}
        body_quota = max(2, limit // 3)
        try:
            chunk_result = await session.run(
                f"""
                CALL db.index.fulltext.queryNodes('{_CHUNK_FTS_INDEX}', $query)
                YIELD node, score
                WHERE (node.domain_id = $domain_id OR ($domain_id = 'default' AND node.domain_id IS NULL))
                  AND (node.branch = $branch OR node.branch = 'main' OR node.branch IS NULL OR node.is_draft = false)
                  AND node.parent_id IS NOT NULL
                RETURN node.parent_id AS parent_id,
                       node.source_file AS source_file,
                       node.content AS content,
                       score
                ORDER BY score DESC
                LIMIT $chunk_limit
                """,
                {
                    "query": fts_query,
                    "chunk_limit": max(limit * 10, 50),
                    "domain_id": domain_id,
                    "branch": branch,
                },
            )
            chunk_hits = await chunk_result.data()

            # O analyzer padrão quebra "https://api-staging.exemplo.dev/x" em "api" + "staging.exemplo.dev":
            # uma busca por "api-staging" casa por "api" em centenas de chunks e o score sozinho não põe o
            # que tem o texto literal na frente. Chunks com a keyword literal (substring, sem diferenciar
            # maiúsculas) vêm primeiro; dentro de cada grupo vale a ordem de score (sort estável).
            literals = _literal_keywords(keyword_list)
            chunk_hits = sorted(
                chunk_hits,
                key=lambda h: 0 if any(lit in str(h.get("content") or "").lower() for lit in literals) else 1,
            )

            # pai -> melhor chunk (o primeiro da ordem acima)
            best_chunk: dict[str, dict] = {}
            for hit in chunk_hits:
                parent_id = hit.get("parent_id")
                if parent_id and parent_id not in best_chunk:
                    best_chunk[parent_id] = hit
            new_parent_ids = [pid for pid in best_chunk if pid not in seed_props]

            if new_parent_ids:
                parent_result = await session.run(
                    """
                    MATCH (p) WHERE p.id IN $parent_ids AND NOT p:DocumentChunk
                      AND (p.domain_id = $domain_id OR ($domain_id = 'default' AND p.domain_id IS NULL))
                      AND (p.branch = $branch OR p.branch = 'main' OR p.branch IS NULL OR p.is_draft = false)
                    RETURN p {.*} AS props, labels(p) AS labels
                    """,
                    {"parent_ids": new_parent_ids, "domain_id": domain_id, "branch": branch},
                )
                parents_by_id: dict[str, dict] = {}
                for r in await parent_result.data():
                    props = r.get("props") or {}
                    if props.get("id"):
                        parents_by_id[props["id"]] = {"props": props, "labels": r.get("labels") or []}

                for parent_id in new_parent_ids:  # literal primeiro, depois score
                    if len(body_snippets) >= body_quota:
                        break
                    parent = parents_by_id.get(parent_id)
                    if parent is None:
                        continue
                    if pillar and parent["props"].get("pillar", "") != pillar:
                        if not any(lbl == pillar for lbl in parent["labels"]):
                            continue
                    hit = best_chunk[parent_id]
                    all_seed_ids.append(parent_id)
                    seed_props[parent_id] = parent
                    body_snippets[parent_id] = _build_match_snippet(
                        str(hit.get("content") or ""), str(hit.get("source_file") or ""), keyword_list
                    )
        except Exception as e:
            # Índice ausente (ainda não criado) ou query inválida: a busca por título segue valendo.
            logger.warning("Busca no corpo (%s) indisponível: %s", _CHUNK_FTS_INDEX, e)

        if not all_seed_ids:
            return SubgraphResponse(
                nodes=[],
                edges=[],
                token_estimate=0,
                query_meta={"keywords": keyword_list, "seed_count": 0, "domain_id": domain_id, "branch": branch},
            )

        # ── Expand: 1-hop neighbors ───────────────────────────────────────────
        try:
            expand_query = Query(
                f"""
                MATCH (seed) WHERE seed.id IN $seed_ids AND (seed.domain_id = $domain_id OR ($domain_id = 'default' AND seed.domain_id IS NULL))
                  AND (seed.branch = $branch OR seed.branch = 'main' OR seed.branch IS NULL OR seed.is_draft = false)
                OPTIONAL MATCH path = (seed)-[r*1..{hops}]-(neighbor)
                WHERE neighbor IS NOT NULL AND neighbor.id IS NOT NULL
                  AND ALL(n IN nodes(path) WHERE n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL))
                  AND (neighbor.branch = $branch OR neighbor.branch = 'main' OR neighbor.branch IS NULL OR neighbor.is_draft = false)
                WITH collect(DISTINCT seed) + collect(DISTINCT neighbor) AS all_nodes,
                     collect(DISTINCT r) AS all_rels
                UNWIND all_nodes AS n
                WITH collect(DISTINCT n) AS nodes,
                     reduce(flat = [], rel_list IN all_rels | flat + rel_list) AS flat_rels
                RETURN nodes,
                       [rel IN flat_rels | {{
                           from: startNode(rel).id,
                           to: endNode(rel).id,
                           type: type(rel)
                       }}] AS edges
                """,
                timeout=_EXPAND_QUERY_TIMEOUT_S,
            )
            expand_result = await session.run(
                expand_query,
                seed_ids=all_seed_ids,
                domain_id=domain_id,
                branch=branch,
            )
            expand_records = await expand_result.data()
        except (Neo4jError, ServiceUnavailable, SessionExpired) as e:
            logger.error("Timeout/erro na expansão de vizinhos (keywords=%s, hops=%d): %s", keyword_list, hops, e)
            raise HTTPException(
                status_code=status.HTTP_504_GATEWAY_TIMEOUT,
                detail="Consulta ao grafo excedeu o tempo limite — tente reduzir keywords/hops.",
            ) from e

    # ── Montar resposta ───────────────────────────────────────────────────────
    nodes: list[NodeContext] = []
    edges: list[EdgeContext] = []

    if expand_records:
        row = expand_records[0]
        for n in row.get("nodes", []):
            if n and n.get("id"):
                if "__chunk_" in str(n.get("id", "")):
                    continue
                node_labels = list(n.labels) if hasattr(n, "labels") else []
                if n["id"] in seed_props:
                    node_labels = seed_props[n["id"]]["labels"]
                ctx = _neo4j_node_to_context(dict(n), node_labels)
                snippet = body_snippets.get(n["id"])
                if snippet:
                    # O MCP publicado só exibe id/title/summary/...: o trecho viaja no summary da RESPOSTA
                    # (nada é gravado no grafo) e também em match_snippet.
                    ctx.match_snippet = snippet
                    ctx.summary = f"{ctx.summary}\n> 🔎 {snippet}" if ctx.summary else f"> 🔎 {snippet}"
                nodes.append(ctx)

        seen_edges: set[tuple[str, str, str]] = set()
        for e in row.get("edges", []):
            if e and e.get("from") and e.get("to"):
                if e.get("type") == "CHUNK_OF":
                    continue
                if "__chunk_" in str(e.get("from", "")) or "__chunk_" in str(e.get("to", "")):
                    continue
                key = (e["from"], e["to"], e.get("type", ""))
                if key in seen_edges:
                    continue
                seen_edges.add(key)
                edges.append(EdgeContext(
                    from_id=e["from"],
                    to_id=e["to"],
                    relationship=e["type"],
                ))

    nodes = _merge_nodes_priority(nodes, branch)

    return SubgraphResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
        query_meta={
            "keywords": keyword_list,
            "seed_count": len(all_seed_ids),
            "body_seed_count": len(body_snippets),
            "pillar_filter": pillar,
            "dimension_filter": dimension,
            "domain_id": domain_id,
            "branch": branch,
        },
    )
