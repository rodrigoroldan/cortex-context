"""
routes/query.py — Consulta semântica cross-dimension ao grafo.

Endpoints:
  GET /api/v1/query?keywords=chat,ai   → subgrafo ~500 tokens de specs relevantes

Os endpoints /specs, /specs/{id} e POST /ingest foram migrados para:
  GET  /api/v1/nodes/spec              (lista specs)
  GET  /api/v1/nodes/spec/{id}         (spec + vizinhos 1-hop)
  POST /api/v1/ingest/spec             (re-ingesta specs)
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver

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


class ServiceRef(BaseModel):
    id: str
    name: str
    repo: str


class SubgraphResponse(BaseModel):
    nodes: list[SpecSummary]
    edges: list[EdgeSummary]
    token_estimate: int
    affected_services: list[ServiceRef] = []


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

    hops = min(hops, 2)

    async with driver.session() as session:
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

    affected_services: list[ServiceRef] = []
    if seed_ids:
        async with driver.session() as session:
            svc_result = await session.run(
                """
                MATCH (s:Spec)-[:AFFECTS]->(svc:Service)
                WHERE s.id IN $seed_ids
                RETURN DISTINCT svc.id AS id, svc.name AS name, svc.repo AS repo
                ORDER BY svc.name
                """,
                seed_ids=seed_ids,
            )
            svc_records = await svc_result.data()
            affected_services = [
                ServiceRef(id=r["id"], name=r["name"] or "", repo=r["repo"] or "")
                for r in svc_records
                if r.get("id")
            ]

    return SubgraphResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
        affected_services=affected_services,
    )
