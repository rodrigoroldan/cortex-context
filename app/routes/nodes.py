"""
routes/nodes.py — Router genérico para consulta de qualquer dimensão do grafo.

Endpoints:
  GET /api/v1/nodes                     → lista dimensões ativas + contagem de nós
  GET /api/v1/nodes/{dim_key}           → lista nós da dimensão (dict genérico + filtros)
  GET /api/v1/nodes/{dim_key}/{node_id} → detalhe do nó + vizinhos 1-hop

Agnóstico ao produto: dim_key é qualquer chave em active_dimensions do cortex.config.yaml.
Exemplos: spec, service, temporal_workflow, bff_route (futuro), api_endpoint (futuro).
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.core.dimension_loader import DimensionConfig, load_dimensions
from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["nodes"])
bearer = HTTPBearer(auto_error=False)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


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


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def _load_dimension_map() -> dict[str, DimensionConfig]:
    """Retorna {dim_key: DimensionConfig} para todas as dimensões ativas."""
    cfg = _load_config()
    dimensions_dir = Path(__file__).parent.parent.parent / cfg.get("dimensions_dir", "app/dimensions")
    active = cfg.get("active_dimensions", [])
    dims = load_dimensions(dimensions_dir, active)
    return {d.dimension: d for d in dims}


# ─── Response models ──────────────────────────────────────────────────────────


class DimensionInfo(BaseModel):
    key: str
    node_label: str
    count: int


class DimensionsResponse(BaseModel):
    dimensions: list[DimensionInfo]
    total_nodes: int


class NodeSummary(BaseModel):
    id: str
    label: str
    properties: dict


class NeighborSummary(BaseModel):
    id: str
    label: str
    relationship: str
    direction: str  # "outbound" | "inbound"
    properties: dict


class NodeDetailResponse(BaseModel):
    node: NodeSummary
    neighbors: list[NeighborSummary]


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/nodes", response_model=DimensionsResponse)
async def list_dimensions(
    _token: str = Depends(_verify_token),
) -> DimensionsResponse:
    """
    Lista todas as dimensões ativas e a contagem de nós de cada uma.

    Retorna o catálogo vivo do grafo — qualquer nova dimensão adicionada ao
    cortex.config.yaml aparece automaticamente aqui.
    """
    dim_map = _load_dimension_map()
    driver = get_driver()
    result: list[DimensionInfo] = []

    for dim_key, dim_cfg in dim_map.items():
        async with driver.session() as session:
            r = await session.run(
                f"MATCH (n:{dim_cfg.node_label}) RETURN count(n) AS cnt"
            )
            records = await r.data()
        cnt = records[0]["cnt"] if records else 0
        result.append(DimensionInfo(key=dim_key, node_label=dim_cfg.node_label, count=cnt))

    result.sort(key=lambda d: d.key)
    return DimensionsResponse(dimensions=result, total_nodes=sum(d.count for d in result))


@router.get("/nodes/{dim_key}", response_model=list[NodeSummary])
async def list_nodes(
    dim_key: str,
    request: Request,
    _token: str = Depends(_verify_token),
) -> list[NodeSummary]:
    """
    Lista todos os nós de uma dimensão.

    Qualquer query param extra é interpretado como filtro de propriedade do nó
    (ex: ?category=scheduled, ?status=active).

    Exemplos:
      GET /api/v1/nodes/spec
      GET /api/v1/nodes/spec?status=completed
      GET /api/v1/nodes/temporal_workflow?category=scheduled
    """
    dim_map = _load_dimension_map()
    dim_cfg = dim_map.get(dim_key)
    if not dim_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Dimensão '{dim_key}' não encontrada. Dimensões ativas: {list(dim_map.keys())}",
        )

    # Extrai filtros dinâmicos do query string (ignora parâmetros internos do FastAPI)
    _reserved = {"skip", "limit"}
    filters = {
        k: v
        for k, v in request.query_params.items()
        if k not in _reserved
    }

    driver = get_driver()

    if filters:
        where_parts = [f"n.{k} = ${k}" for k in filters]
        where_clause = "WHERE " + " AND ".join(where_parts)
        cypher = f"MATCH (n:{dim_cfg.node_label}) {where_clause} RETURN n {{.*}} AS props ORDER BY n.id"
        async with driver.session() as session:
            result = await session.run(cypher, **filters)
            records = await result.data()
    else:
        async with driver.session() as session:
            result = await session.run(
                f"MATCH (n:{dim_cfg.node_label}) RETURN n {{.*}} AS props ORDER BY n.id"
            )
            records = await result.data()

    return [
        NodeSummary(id=r["props"].get("id", ""), label=dim_cfg.node_label, properties=_sanitize_props(dict(r["props"])))
        for r in records
    ]


@router.get("/nodes/{dim_key}/{node_id}", response_model=NodeDetailResponse)
async def get_node(
    dim_key: str,
    node_id: str,
    _token: str = Depends(_verify_token),
) -> NodeDetailResponse:
    """
    Retorna um nó pelo ID com todos os vizinhos de 1-hop e o tipo de relacionamento.

    Resposta agnóstica: retorna as propriedades brutas do Neo4j + vizinhos com
    label e tipo de aresta, sem assumir nada sobre a dimensão.

    Exemplos:
      GET /api/v1/nodes/spec/spec-070
      GET /api/v1/nodes/service/service-backend
      GET /api/v1/nodes/temporal_workflow/workflow-finance-reconciliation
    """
    dim_map = _load_dimension_map()
    dim_cfg = dim_map.get(dim_key)
    if not dim_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Dimensão '{dim_key}' não encontrada. Dimensões ativas: {list(dim_map.keys())}",
        )

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            f"""
            MATCH (n:{dim_cfg.node_label} {{id: $node_id}})
            OPTIONAL MATCH (n)-[r_out]->(neighbor_out)
            OPTIONAL MATCH (n)<-[r_in]-(neighbor_in)
            RETURN
                n {{.*}} AS props,
                collect(DISTINCT {{
                    id: neighbor_out.id,
                    label: labels(neighbor_out)[0],
                    relationship: type(r_out),
                    direction: 'outbound',
                    properties: neighbor_out {{.*}}
                }}) AS outbound,
                collect(DISTINCT {{
                    id: neighbor_in.id,
                    label: labels(neighbor_in)[0],
                    relationship: type(r_in),
                    direction: 'inbound',
                    properties: neighbor_in {{.*}}
                }}) AS inbound
            """,
            node_id=node_id,
        )
        records = await result.data()

    if not records or not records[0]["props"]:
        raise HTTPException(
            status_code=404,
            detail=f"Nó '{node_id}' não encontrado na dimensão '{dim_key}' (:{dim_cfg.node_label})",
        )

    row = records[0]

    neighbors: list[NeighborSummary] = []
    for n in row.get("outbound", []):
        if n and n.get("id") and n.get("relationship"):
            neighbors.append(NeighborSummary(
                id=n["id"],
                label=n.get("label", ""),
                relationship=n["relationship"],
                direction="outbound",
                properties=_sanitize_props(dict(n.get("properties") or {})),
            ))
    for n in row.get("inbound", []):
        if n and n.get("id") and n.get("relationship"):
            neighbors.append(NeighborSummary(
                id=n["id"],
                label=n.get("label", ""),
                relationship=n["relationship"],
                direction="inbound",
                properties=_sanitize_props(dict(n.get("properties") or {})),
            ))

    return NodeDetailResponse(
        node=NodeSummary(
            id=row["props"].get("id", node_id),
            label=dim_cfg.node_label,
            properties=_sanitize_props(dict(row["props"])),
        ),
        neighbors=neighbors,
    )
