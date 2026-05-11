"""
query.py — Rotas de consulta ao grafo de specs.

Endpoints:
  GET /api/v1/query?keywords=chat,ai   → subgrafo ~500 tokens de specs relevantes
  GET /api/v1/specs                    → lista todas as specs (id, title, status)
  GET /api/v1/specs/{spec_id}          → spec completa + vizinhos 1-hop
  POST /api/v1/ingest                  → re-ingesta specs (usado pelo GitHub Action)
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver
from app.ingestor.graph_builder import ingest_edges, ingest_nodes
from app.ingestor.spec_parser import parse_all_specs, parse_spec_dir, _extract_number_and_slug
from app.models.spec import SpecEdge, SpecNode

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
bearer = HTTPBearer()


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token inválido",
        )
    return credentials.credentials


# ─── Response models ──────────────────────────────────────────────────────────


class SpecSummary(BaseModel):
    id: str
    number: int
    title: str
    status: str
    labels: list[str]
    summary: str
    repos: list[str]
    file_path: str


class EdgeSummary(BaseModel):
    from_id: str
    to_id: str
    relationship: str


class SubgraphResponse(BaseModel):
    nodes: list[SpecSummary]
    edges: list[EdgeSummary]
    token_estimate: int


class IngestRequest(BaseModel):
    spec_paths: list[str] | None = None  # None = ingestão completa


class IngestResponse(BaseModel):
    nodes_upserted: int
    edges_upserted: int
    message: str


# ─── Helpers ──────────────────────────────────────────────────────────────────

def _node_row_to_summary(row: dict) -> SpecSummary:
    return SpecSummary(
        id=row.get("id", ""),
        number=row.get("number", 0),
        title=row.get("title", ""),
        status=row.get("status", ""),
        labels=row.get("labels", []),
        summary=row.get("summary", ""),
        repos=row.get("repos", []),
        file_path=row.get("file_path", ""),
    )


def _estimate_tokens(nodes: list[SpecSummary]) -> int:
    """Estimativa grosseira: ~4 chars por token."""
    total_chars = sum(
        len(n.id) + len(n.title) + len(n.summary) + len(" ".join(n.labels)) + len(" ".join(n.repos))
        for n in nodes
    )
    return total_chars // 4


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/query", response_model=SubgraphResponse)
async def query_context(
    keywords: str,
    limit: int = 8,
    hops: int = 1,
    _token: str = Depends(_verify_token),
) -> SubgraphResponse:
    """
    Busca specs por keywords usando FTS e expande 1-hop no grafo.

    - `keywords`: string separada por vírgulas, ex: `chat,ai,event`
    - `limit`: máximo de nós seed retornados pelo FTS (default 8)
    - `hops`: profundidade da expansão no grafo (default 1, max 2)
    """
    driver = get_driver()
    keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    fts_query = " OR ".join(keyword_list)

    # Limita expansão para não explodir o contexto
    hops = min(hops, 2)

    async with driver.session() as session:
        # 1. FTS seed nodes
        seed_result = await session.run(
            """
            CALL db.index.fulltext.queryNodes('spec_fts', $query)
            YIELD node, score
            RETURN node {.*} AS props, score
            ORDER BY score DESC
            LIMIT $limit
            """,
            {"query": fts_query, "limit": limit},
        )
        seed_records = await seed_result.data()
        seed_ids = [r["props"]["id"] for r in seed_records]

        if not seed_ids:
            return SubgraphResponse(nodes=[], edges=[], token_estimate=0)

        # 2. Expande N hops a partir dos seeds
        expand_result = await session.run(
            f"""
            MATCH (s:Spec) WHERE s.id IN $seed_ids
            OPTIONAL MATCH path = (s)-[r*1..{hops}]-(neighbor:Spec)
            WITH collect(DISTINCT s) + collect(DISTINCT neighbor) AS all_nodes,
                 collect(DISTINCT r) AS all_rels
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
            """,
            seed_ids=seed_ids,
        )
        expand_records = await expand_result.data()

        nodes: list[SpecSummary] = []
        edges: list[EdgeSummary] = []

        if expand_records:
            row = expand_records[0]
            for n in row.get("nodes", []):
                if n:
                    nodes.append(_node_row_to_summary(dict(n)))
            for e in row.get("edges", []):
                if e and e.get("from") and e.get("to"):
                    edges.append(
                        EdgeSummary(
                            from_id=e["from"],
                            to_id=e["to"],
                            relationship=e["type"],
                        )
                    )

    return SubgraphResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
    )


@router.get("/specs", response_model=list[SpecSummary])
async def list_specs(
    spec_status: Optional[str] = None,
    _token: str = Depends(_verify_token),
) -> list[SpecSummary]:
    """Lista todas as specs. Filtra por `status` (completed, in-progress, planned, todo)."""
    driver = get_driver()

    async with driver.session() as session:
        if spec_status:
            result = await session.run(
                "MATCH (s:Spec {status: $status}) RETURN s {.*} AS props ORDER BY s.number",
                status=spec_status,
            )
        else:
            result = await session.run(
                "MATCH (s:Spec) RETURN s {.*} AS props ORDER BY s.number"
            )
        records = await result.data()

    return [_node_row_to_summary(r["props"]) for r in records]


@router.get("/specs/{spec_id}", response_model=SubgraphResponse)
async def get_spec(
    spec_id: str,
    _token: str = Depends(_verify_token),
) -> SubgraphResponse:
    """Retorna uma spec pelo ID (ex: spec-070) com vizinhos de 1-hop."""
    driver = get_driver()

    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (s:Spec {id: $spec_id})
            OPTIONAL MATCH (s)-[r]-(neighbor:Spec)
            RETURN s {.*} AS spec_props,
                   collect(DISTINCT neighbor {.*}) AS neighbor_props,
                   collect(DISTINCT {
                       from: startNode(r).id,
                       to: endNode(r).id,
                       type: type(r)
                   }) AS edges
            """,
            spec_id=spec_id,
        )
        records = await result.data()

    if not records or not records[0]["spec_props"]:
        raise HTTPException(status_code=404, detail=f"Spec {spec_id} não encontrada")

    row = records[0]
    nodes = [_node_row_to_summary(row["spec_props"])]
    for n in row.get("neighbor_props", []):
        if n:
            nodes.append(_node_row_to_summary(dict(n)))

    edges = [
        EdgeSummary(from_id=e["from"], to_id=e["to"], relationship=e["type"])
        for e in row.get("edges", [])
        if e and e.get("from") and e.get("to")
    ]

    return SubgraphResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
    )


@router.post("/ingest", response_model=IngestResponse)
async def ingest(
    req: IngestRequest,
    settings=Depends(get_settings),
    _token: str = Depends(_verify_token),
) -> IngestResponse:
    """
    Re-ingesta specs no Neo4j.

    - `spec_paths`: lista de caminhos (ex: ["specs/070-ai-event-chat-v2/plan.md"]).
      Se omitido, re-ingesta todas as specs.
    """
    specs_dir = Path(settings.specs_dir)
    driver = get_driver()

    if req.spec_paths:
        # Ingestão incremental: apenas specs alteradas
        nodes_list = []
        for path_str in req.spec_paths:
            # Resolve o diretório de spec a partir do caminho
            p = Path(path_str)
            # "specs/070-ai-event-chat-v2/plan.md" → "070-ai-event-chat-v2"
            parts = p.parts
            spec_dir_name = None
            for i, part in enumerate(parts):
                if part == "specs" and i + 1 < len(parts):
                    spec_dir_name = parts[i + 1]
                    break

            if spec_dir_name is None:
                logger.warning("Caminho inesperado: %s", path_str)
                continue

            spec_dir = specs_dir / spec_dir_name
            if spec_dir.exists() and spec_dir.is_dir():
                node = parse_spec_dir(spec_dir)
                if node:
                    nodes_list.append(node)
                    logger.info("Parseado: %s", node.id)

        # Arestas só para os nodes afetados
        if nodes_list:
            from app.ingestor.spec_parser import parse_edges
            all_nodes, _ = parse_all_specs(specs_dir)
            nodes_by_number = {n.number: n for n in all_nodes}
            all_edges = parse_edges(nodes_by_number, specs_dir)
            affected_ids = {n.id for n in nodes_list}
            edges_list = [
                e for e in all_edges if e.from_id in affected_ids or e.to_id in affected_ids
            ]
        else:
            edges_list = []
    else:
        # Ingestão completa
        nodes_list, edges_list = parse_all_specs(specs_dir)

    nodes_ok = await ingest_nodes(driver, nodes_list)
    edges_ok = await ingest_edges(driver, edges_list)

    return IngestResponse(
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=f"Ingestão concluída: {nodes_ok} nós, {edges_ok} arestas",
    )
