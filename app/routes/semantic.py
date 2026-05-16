"""
routes/semantic.py — Busca semântica por similaridade vetorial (Vector RAG).

Endpoints:
  POST /api/v1/query/semantic
    Body: {"query": "como funciona o rateio de eventos?", "top_k": 8, "hops": 1, "pillar": "Intent"}
    Retorna: nós mais similares + vizinhos 1-hop no grafo

Requer CORTEX_EMBEDDING_PROVIDER != "none" (openai ou local).
Se o embedder estiver desabilitado, retorna 503 com instrução de configuração.

O fluxo é Hybrid GraphRAG:
  1. Converte a query em embedding.
  2. Busca ANN no índice vetorial (DocumentChunk nodes).
  3. Resolve os nós pai de cada chunk.
  4. Expande 1-hop no grafo para contexto de vizinhança.
  5. Retorna subgrafo + score médio dos chunks encontrados.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.embedder import EmbedderError, embed_texts, is_embedder_enabled
from app.db.neo4j import get_driver, vector_search

logger = logging.getLogger(__name__)

router = APIRouter(tags=["semantic"])
bearer = HTTPBearer()


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Request / Response models ──────────────────────────────────────────────────


class SemanticSearchRequest(BaseModel):
    query: str = Field(..., min_length=3, description="Pergunta em linguagem natural")
    top_k: int = Field(default=8, ge=1, le=50, description="Número de chunks similares a buscar")
    hops: int = Field(default=1, ge=0, le=2, description="Hops de expansão no grafo")
    pillar: str | None = Field(default=None, description="Filtrar por pilar I.S.I.R (Intent|System|Implementation|Runtime)")


class SemanticNodeResult(BaseModel):
    id: str
    labels: list[str]
    pillar: str
    title: str = ""
    summary: str = ""
    status: str = ""
    properties: dict[str, Any] = {}
    chunk_score: float | None = None   # Score do chunk filho que trouxe este nó


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


# ─── Route ──────────────────────────────────────────────────────────────────────


@router.post(
    "/query/semantic",
    response_model=SemanticSearchResponse,
    summary="Busca semântica por similaridade vetorial (Hybrid GraphRAG)",
    description=(
        "Converte a query em embedding, busca os DocumentChunks mais similares "
        "no índice vetorial Neo4j, resolve os nós pai, e expande vizinhança no grafo. "
        "Requer CORTEX_EMBEDDING_PROVIDER != 'none'."
    ),
)
async def semantic_search(
    payload: SemanticSearchRequest,
    _token: str = Depends(_verify_token),
) -> SemanticSearchResponse:
    # ── Guard: embedder deve estar ativo ──────────────────────────────────────
    if not is_embedder_enabled():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                "Busca semântica requer CORTEX_EMBEDDING_PROVIDER != 'none'. "
                "Configure 'openai' ou 'local' no .env e reinicie o Cortex."
            ),
        )

    # ── Gerar embedding da query ──────────────────────────────────────────────
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
    )

    if not chunk_results:
        return SemanticSearchResponse(
            nodes=[],
            edges=[],
            token_estimate=0,
            query_meta={"query": payload.query, "chunk_count": 0},
        )

    # Mapear parent_id → score máximo do chunk filho
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

        cypher = f"""
        MATCH (seed) WHERE seed.id IN $parent_ids AND NOT seed:DocumentChunk
        {"OPTIONAL MATCH path = " + hop_clause if hops > 0 else ""}
        {"WHERE neighbor IS NOT NULL AND neighbor.id IS NOT NULL AND NOT neighbor:DocumentChunk" if hops > 0 else ""}
        WITH collect(DISTINCT seed) {("+ collect(DISTINCT neighbor)" if hops > 0 else "")} AS all_nodes,
             {("collect(DISTINCT r) AS all_rels" if hops > 0 else "[] AS all_rels")}
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

        result = await session.run(cypher, parent_ids=parent_ids)
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

    # Ordenar nós por score do chunk descendente
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
        },
    )
