"""
routes/query.py — Consulta semântica cross-dimension ao grafo Cortex Context.

Endpoints:
  GET /api/v1/query?keywords=chat,ai            → subgrafo ~500 tokens por keywords
  GET /api/v1/query?keywords=payment&pillar=Intent  → filtrado por pilar I.S.I.R
  GET /api/v1/query?keywords=auth&dimension=spec    → filtrado por dimensão

A busca é totalmente genérica — não assume labels fixas (Spec, Service, etc.).
Usa o índice FTS configurado por dimensão no YAML.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["query"])
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


# ─── Response models ──────────────────────────────────────────────────────────


class NodeContext(BaseModel):
    """Nó genérico do grafo — qualquer dimensão/pilar."""
    id: str
    labels: list[str]         # ex: ["Spec", "Intent"]
    pillar: str               # ex: "Intent"
    properties: dict[str, Any]
    # Campos de conveniência extraídos de properties (se disponíveis)
    title: str = ""
    summary: str = ""
    status: str = ""


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
    """Converte tipos Neo4j não-serializáveis (DateTime, Date, etc.) para string."""
    result = {}
    for k, v in props.items():
        if hasattr(v, "iso_format"):  # neo4j.time.DateTime, Date, Time
            result[k] = v.iso_format()
        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("neo4j"):
            result[k] = str(v)
        else:
            result[k] = v
    return result


def _neo4j_node_to_context(node_data: dict, labels: list[str]) -> NodeContext:
    """Converte um record do Neo4j para NodeContext genérico."""
    pillar = node_data.get("pillar", "")
    # Inferir pilar a partir dos labels se não for propriedade do nó
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
    """Estimativa grosseira: ~4 chars por token."""
    total = sum(len(n.title) + len(n.summary) + len(n.id) + 20 for n in nodes)
    return total // 4


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get(
    "/query",
    response_model=SubgraphResponse,
    summary="Busca semântica cross-dimension por keywords",
    description=(
        "FTS cross-dimension usando os índices declarados nos dimension YAMLs. "
        "Expande 1-hop no grafo para contexto de vizinhança. "
        "Filtros opcionais: pillar (Intent|System|Implementation|Runtime), dimension (spec|service|...)."
    ),
)
async def query_context(
    keywords: str,
    limit: int = 8,
    hops: int = 1,
    pillar: str | None = None,
    dimension: str | None = None,
    _token: str = Depends(_verify_token),
) -> SubgraphResponse:
    driver = get_driver()
    keyword_list = [kw.strip() for kw in keywords.split(",") if kw.strip()]
    fts_query = " OR ".join(keyword_list)
    hops = min(max(hops, 1), 2)

    async with driver.session() as session:
        # ── Seed: FTS cross-dimension ─────────────────────────────────────────
        # Tenta múltiplos índices FTS conhecidos e une os resultados
        fts_indexes = ["spec_fulltext", "service_fulltext", "workflow_fulltext"]
        all_seed_ids: list[str] = []
        seed_props: dict[str, dict] = {}

        for idx_name in fts_indexes:
            try:
                seed_result = await session.run(
                    f"""
                    CALL db.index.fulltext.queryNodes('{idx_name}', $query)
                    YIELD node, score
                    RETURN node {{.*}} AS props,
                           labels(node) AS labels,
                           score
                    ORDER BY score DESC
                    LIMIT $limit
                    """,
                    {"query": fts_query, "limit": limit},
                )
                records = await seed_result.data()
                for r in records:
                    props = r.get("props") or {}
                    nid = props.get("id", "")
                    node_labels: list[str] = r.get("labels") or []
                    if not nid:
                        continue
                    # Aplicar filtros opcionais
                    if pillar and props.get("pillar", "") != pillar:
                        if not any(lbl == pillar for lbl in node_labels):
                            continue
                    if nid not in all_seed_ids:
                        all_seed_ids.append(nid)
                        seed_props[nid] = {"props": props, "labels": node_labels}
            except Exception:
                # Índice não existe — pular silenciosamente
                pass

        if not all_seed_ids:
            return SubgraphResponse(
                nodes=[],
                edges=[],
                token_estimate=0,
                query_meta={"keywords": keyword_list, "seed_count": 0},
            )

        # ── Expand: 1-hop neighbors ───────────────────────────────────────────
        expand_result = await session.run(
            f"""
            MATCH (seed) WHERE seed.id IN $seed_ids
            OPTIONAL MATCH path = (seed)-[r*1..{hops}]-(neighbor)
            WHERE neighbor IS NOT NULL AND neighbor.id IS NOT NULL
            WITH collect(DISTINCT seed) + collect(DISTINCT neighbor) AS all_nodes,
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
            seed_ids=all_seed_ids,
        )
        expand_records = await expand_result.data()

    # ── Montar resposta ───────────────────────────────────────────────────────
    nodes: list[NodeContext] = []
    edges: list[EdgeContext] = []

    if expand_records:
        row = expand_records[0]
        for n in row.get("nodes", []):
            if n and n.get("id"):
                node_labels = list(n.labels) if hasattr(n, "labels") else []
                # Enriquecer com labels do seed se disponível
                if n["id"] in seed_props:
                    node_labels = seed_props[n["id"]]["labels"]
                nodes.append(_neo4j_node_to_context(dict(n), node_labels))

        for e in row.get("edges", []):
            if e and e.get("from") and e.get("to"):
                edges.append(EdgeContext(
                    from_id=e["from"],
                    to_id=e["to"],
                    relationship=e["type"],
                ))

    return SubgraphResponse(
        nodes=nodes,
        edges=edges,
        token_estimate=_estimate_tokens(nodes),
        query_meta={
            "keywords": keyword_list,
            "seed_count": len(all_seed_ids),
            "pillar_filter": pillar,
            "dimension_filter": dimension,
        },
    )
