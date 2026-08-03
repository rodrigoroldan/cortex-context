"""
routes/semantic.py — Busca semântica por similaridade vetorial (Vector RAG) com isolamento por domain_id.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.embedder import EmbedderError, embed_texts, is_embedder_enabled
from app.db.neo4j import get_driver, vector_search
from app.routes.dependencies import get_branch, get_domain_id

logger = logging.getLogger(__name__)

router = APIRouter(tags=["semantic"])
bearer = HTTPBearer(auto_error=False)


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


# ─── Request / Response models ──────────────────────────────────────────────────


class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=3, description="Pergunta em linguagem natural")
    top_k: int = Field(default=8, ge=1, le=50, description="Número de chunks similares a buscar")
    hops: int = Field(default=1, ge=0, le=2, description="Hops de expansão no grafo")
    pillar: str | None = Field(default=None, description="Filtrar por pilar I.S.I.R (Intent|System|Implementation|Runtime)")
    domain_id: str = Field(default="default", description="Identificador do domínio para isolamento multi-tenant")
    branch: str = Field(default="main", description="Nome da branch git para busca com overlay de shadow graph")


class SemanticNodeResult(BaseModel):
    id: str
    labels: list[str]
    pillar: str
    title: str = ""
    summary: str = ""
    status: str = ""
    properties: dict[str, Any] = {}
    chunk_score: float | None = None


class SemanticEdgeResult(BaseModel):
    from_id: str
    to_id: str
    relationship: str


class SemanticSearchResponse(BaseModel):
    nodes: list[SemanticNodeResult]
    edges: list[SemanticEdgeResult]
    token_estimate: int
    query_meta: dict[str, Any] = {}


def _estimate_tokens(nodes: list[SemanticNodeResult]) -> int:
    total = sum(len(n.title) + len(n.summary) + len(n.id) + 20 for n in nodes)
    return total // 4


def _merge_semantic_nodes_priority(nodes: list[SemanticNodeResult], branch: str) -> list[SemanticNodeResult]:
    if branch == "main" or not nodes:
        return nodes
    entity_map: dict[str, SemanticNodeResult] = {}
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


# ─── Route ──────────────────────────────────────────────────────────────────────


@router.post(
    "/query/semantic",
    response_model=SemanticSearchResponse,
    summary="Busca semântica por similaridade vetorial (Hybrid GraphRAG)",
)
async def semantic_search(
    payload: SemanticSearchRequest,
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> SemanticSearchResponse:
    effective_domain = payload.domain_id if (payload.domain_id and payload.domain_id != "default") else domain_id
    effective_branch = payload.branch if (payload.branch and payload.branch != "main") else branch

    if not is_embedder_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Busca semântica requer CORTEX_EMBEDDING_PROVIDER != 'none'. "
                "Configure 'openai' ou 'local' no .env e reinicie o Cortex."
            ),
        )

    try:
        embeddings = await embed_texts([payload.query])
    except EmbedderError as exc:
        logger.error("Falha ao gerar embedding da query: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro ao gerar embedding: {exc}",
        ) from exc

    if not embeddings:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Embedder retornou resultado vazio.",
        )

    query_embedding = embeddings[0]

    # ── Busca vetorial ANN nos DocumentChunks ─────────────────────────────────
    chunk_results = await vector_search(
        query_embedding=query_embedding,
        top_k=payload.top_k,
        pillar_filter=payload.pillar,
        domain_id=effective_domain,
    )

    if not chunk_results:
        return SemanticSearchResponse(
            nodes=[],
            edges=[],
            token_estimate=0,
            query_meta={"query": payload.query, "chunk_count": 0, "domain_id": effective_domain, "branch": effective_branch},
        )

    parent_scores: dict[str, float] = {}
    for row in chunk_results:
        pid = row.get("parent_id", "")
        score = float(row.get("score", 0.0))
        if pid and (pid not in parent_scores or score > parent_scores[pid]):
            parent_scores[pid] = score

    parent_ids = list(parent_scores.keys())

    # ── Expandir 1-hop no grafo a partir dos nós pai ──────────────────────────
    hops = min(max(payload.hops, 0), 2)
    driver = get_driver()

    async with driver.session() as session:
        hop_clause = f"(seed)-[r*1..{hops}]-(neighbor)" if hops > 0 else "(seed)"

        match_seed = (
            "MATCH (seed) WHERE seed.id IN $parent_ids AND NOT seed:DocumentChunk "
            "AND (seed.domain_id = $domain_id OR ($domain_id = 'default' AND seed.domain_id IS NULL)) "
            "AND (seed.branch = $branch OR seed.branch = 'main' OR seed.branch IS NULL OR seed.is_draft = false)"
        )
        opt_match = f"OPTIONAL MATCH path = {hop_clause}" if hops > 0 else ""
        opt_where = (
            "WHERE neighbor IS NOT NULL AND neighbor.id IS NOT NULL AND NOT neighbor:DocumentChunk "
            "AND ALL(n IN nodes(path) WHERE n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL)) "
            "AND (neighbor.branch = $branch OR neighbor.branch = 'main' OR neighbor.branch IS NULL OR neighbor.is_draft = false)"
            if hops > 0 else ""
        )
        with_nodes = f"WITH collect(DISTINCT seed) {('+ collect(DISTINCT neighbor)' if hops > 0 else '')} AS all_nodes,"
        with_rels = f"{('collect(DISTINCT r) AS all_rels' if hops > 0 else '[] AS all_rels')}"

        cypher = f"""
        {match_seed}
        {opt_match}
        {opt_where}
        {with_nodes}
             {with_rels}
        UNWIND all_nodes AS n
        WITH collect(DISTINCT n) AS nodes, all_rels
        UNWIND all_rels AS rel_list
        UNWIND rel_list AS rel
        RETURN nodes,
               collect(DISTINCT {{
                   from: startNode(rel).id,
                   to: endNode(rel).id,
                   type: type(rel)
               }}) AS edges
        """

        result = await session.run(cypher, parent_ids=parent_ids, domain_id=effective_domain, branch=effective_branch)
        records = await result.data()

    # ── Montar resposta ────────────────────────────────────────────────────────
    nodes: list[SemanticNodeResult] = []
    edges: list[SemanticEdgeResult] = []

    if records:
        row = records[0]
        for n in row.get("nodes", []):
            if not n or not n.get("id"):
                continue
            nid = n["id"]
            node_labels = list(n.labels) if hasattr(n, "labels") else []
            pillar = n.get("pillar", "")
            if not pillar:
                for lbl in node_labels:
                    if lbl in ("Intent", "System", "Implementation", "Runtime"):
                        pillar = lbl
                        break

            nodes.append(SemanticNodeResult(
                id=nid,
                labels=node_labels,
                pillar=pillar,
                title=str(n.get("title", "")),
                summary=str(n.get("summary", "")),
                status=str(n.get("status", "")),
                properties=dict(n),
                chunk_score=parent_scores.get(nid),
            ))

        for e in row.get("edges", []):
            if e and e.get("from") and e.get("to"):
                edges.append(SemanticEdgeResult(
                    from_id=e["from"],
                    to_id=e["to"],
                    relationship=e["type"],
                ))

    nodes = _merge_semantic_nodes_priority(nodes, effective_branch)
    nodes.sort(key=lambda n: n.chunk_score or 0.0, reverse=True)

    return SemanticSearchResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
        query_meta={
            "query": payload.query,
            "chunk_count": len(chunk_results),
            "parent_count": len(parent_ids),
            "pillar_filter": payload.pillar,
            "domain_id": effective_domain,
            "branch": effective_branch,
        },
    )
