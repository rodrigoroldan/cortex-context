"""
routes/nodes.py — Router genérico para consulta de qualquer dimensão do grafo com isolamento por domain_id.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.core.dimension_loader import DimensionConfig, load_dimensions
from app.db.neo4j import get_driver
from app.routes.dependencies import get_branch, get_domain_id

logger = logging.getLogger(__name__)

IDENTIFIER_REGEX = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")

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
    result = {}
    for k, v in props.items():
        if hasattr(v, "iso_format"):
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


def _merge_node_summaries_priority(items: list[NodeSummary], branch: str) -> list[NodeSummary]:
    if branch == "main" or not items:
        return items
    entity_map: dict[str, NodeSummary] = {}
    for item in items:
        raw_key = item.properties.get("canonical_id") or item.id
        if str(raw_key).startswith("draft:"):
            parts = str(raw_key).split(":", 2)
            canonical_key = parts[2] if len(parts) == 3 else str(raw_key)
        else:
            canonical_key = str(raw_key)

        item_branch = str(item.properties.get("branch", "main"))
        item_is_draft = bool(item.properties.get("is_draft", False)) or item.properties.get("status") == "draft" or item_branch == branch

        if canonical_key not in entity_map:
            entity_map[canonical_key] = item
        else:
            existing = entity_map[canonical_key]
            existing_branch = str(existing.properties.get("branch", "main"))
            if (item_branch == branch or item_is_draft) and not (existing_branch == branch):
                entity_map[canonical_key] = item
    return list(entity_map.values())


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/nodes", response_model=DimensionsResponse)
async def list_dimensions(
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> DimensionsResponse:
    """
    Lista todas as dimensões ativas e a contagem de nós de cada uma para o domain_id e branch.
    """
    dim_map = _load_dimension_map()
    driver = get_driver()
    result: list[DimensionInfo] = []

    for dim_key, dim_cfg in dim_map.items():
        async with driver.session() as session:
            cypher_cnt = (
                f"MATCH (n:{dim_cfg.node_label}) "
                "WHERE (n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL)) "
                "AND (n.branch = $branch OR n.branch = 'main' OR n.branch IS NULL OR n.is_draft = false) "
                "RETURN count(n) AS cnt"
            )
            r = await session.run(cypher_cnt, domain_id=domain_id, branch=branch)
            records = await r.data()
        cnt = records[0]["cnt"] if records else 0
        result.append(DimensionInfo(key=dim_key, node_label=dim_cfg.node_label, count=cnt))

    result.sort(key=lambda d: d.key)
    return DimensionsResponse(dimensions=result, total_nodes=sum(d.count for d in result))


@router.get("/nodes/{dim_key}", response_model=list[NodeSummary])
async def list_nodes(
    dim_key: str,
    request: Request,
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> list[NodeSummary]:
    """
    Lista todos os nós de uma dimensão filtrados por domain_id, branch e parâmetros extra.
    """
    dim_map = _load_dimension_map()
    dim_cfg = dim_map.get(dim_key)
    if not dim_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Dimensão '{dim_key}' não encontrada. Dimensões ativas: {list(dim_map.keys())}",
        )

    _reserved = {"skip", "limit", "domain_id", "branch", "X-Domain-ID", "X-Cortex-Domain", "X-Cortex-Branch", "X-Branch-Name", "X-Branch"}
    filters = {
        k: v
        for k, v in request.query_params.items()
        if k not in _reserved
    }

    raw_limit = request.query_params.get("limit")
    limit: int | None = None
    if raw_limit is not None:
        try:
            limit = int(raw_limit)
        except ValueError:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"'limit' inválido: '{raw_limit}'. Deve ser um inteiro.",
            )

    driver = get_driver()

    # `parameters=` (não **kwargs) evita colisão entre uma chave de filtro (ex: "query",
    # usada como filtro pela tool MCP query_history) e o parâmetro posicional `query` do
    # próprio AsyncSession.run(query, parameters=None, **kwargs) do driver neo4j (#24).
    params: dict = {"domain_id": domain_id, "branch": branch, **filters}
    limit_clause = ""
    if limit is not None:
        params["limit"] = limit
        limit_clause = " LIMIT $limit"

    try:
        if filters:
            for k in filters:
                if not IDENTIFIER_REGEX.match(k):
                    raise HTTPException(
                        status_code=status.HTTP_400_BAD_REQUEST,
                        detail=f"Chave de filtro inválida: '{k}'. Chaves de propriedade devem corresponder ao padrão '^[a-zA-Z_][a-zA-Z0-9_]*$'.",
                    )
            where_parts = [f"n.{k} = ${k}" for k in filters]
            where_parts.append("(n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL))")
            where_parts.append("(n.branch = $branch OR n.branch = 'main' OR n.branch IS NULL OR n.is_draft = false)")
            where_clause = "WHERE " + " AND ".join(where_parts)
            cypher = f"MATCH (n:{dim_cfg.node_label}) {where_clause} RETURN n {{.*}} AS props ORDER BY n.id{limit_clause}"
        else:
            cypher = (
                f"MATCH (n:{dim_cfg.node_label}) "
                "WHERE (n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL)) "
                "AND (n.branch = $branch OR n.branch = 'main' OR n.branch IS NULL OR n.is_draft = false) "
                f"RETURN n {{.*}} AS props ORDER BY n.id{limit_clause}"
            )
        async with driver.session() as session:
            result = await session.run(cypher, parameters=params)
            records = await result.data()
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Erro ao listar nós da dimensão '%s' com filtros %s", dim_key, filters)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Erro interno ao consultar dimensão '{dim_key}': {exc}",
        ) from exc

    summaries = [
        NodeSummary(id=r["props"].get("id", ""), label=dim_cfg.node_label, properties=_sanitize_props(dict(r["props"])))
        for r in records
    ]
    return _merge_node_summaries_priority(summaries, branch)


@router.get("/nodes/{dim_key}/{node_id}", response_model=NodeDetailResponse)
async def get_node(
    dim_key: str,
    node_id: str,
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> NodeDetailResponse:
    """
    Retorna um nó pelo ID com todos os vizinhos de 1-hop filtrados por domain_id e branch.
    Se existir nó draft para a branch atual, ele sobrescreve o nó canônico main.
    """
    dim_map = _load_dimension_map()
    dim_cfg = dim_map.get(dim_key)
    if not dim_cfg:
        raise HTTPException(
            status_code=404,
            detail=f"Dimensão '{dim_key}' não encontrada. Dimensões ativas: {list(dim_map.keys())}",
        )

    draft_id = f"draft:{branch}:{node_id}" if not node_id.startswith("draft:") else node_id

    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            f"""
            MATCH (n:{dim_cfg.node_label})
            WHERE (n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL))
              AND (n.id = $node_id OR n.id = $draft_id OR (n.canonical_id = $node_id AND n.branch = $branch) OR n.canonical_id = $node_id)
              AND (n.branch = $branch OR n.branch = 'main' OR n.branch IS NULL OR n.is_draft = false)
            OPTIONAL MATCH (n)-[r_out]->(neighbor_out)
            WHERE neighbor_out IS NULL
               OR ((neighbor_out.domain_id = $domain_id OR ($domain_id = 'default' AND neighbor_out.domain_id IS NULL))
                   AND (neighbor_out.branch = $branch OR neighbor_out.branch = 'main'
                        OR neighbor_out.branch IS NULL OR neighbor_out.is_draft = false))
            OPTIONAL MATCH (n)<-[r_in]-(neighbor_in)
            WHERE neighbor_in IS NULL
               OR ((neighbor_in.domain_id = $domain_id OR ($domain_id = 'default' AND neighbor_in.domain_id IS NULL))
                   AND (neighbor_in.branch = $branch OR neighbor_in.branch = 'main'
                        OR neighbor_in.branch IS NULL OR neighbor_in.is_draft = false))
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
            draft_id=draft_id,
            domain_id=domain_id,
            branch=branch,
        )
        records = await result.data()

    if not records or not records[0]["props"]:
        raise HTTPException(
            status_code=404,
            detail=f"Nó '{node_id}' não encontrado na dimensão '{dim_key}' (:{dim_cfg.node_label}) para o domínio '{domain_id}'",
        )

    # Pick draft node for requested branch if returned alongside canonical main node
    selected_row = records[0]
    for row in records:
        p = row.get("props") or {}
        if str(p.get("branch", "")) == branch or bool(p.get("is_draft", False)) or str(p.get("id", "")).startswith("draft:"):
            selected_row = row
            break

    row = selected_row

    outbound_raw = row.get("outbound", [])
    inbound_raw = row.get("inbound", [])

    # Filter out empty neighbors
    neighbors: list[NeighborSummary] = []
    seen_neighbors: set[tuple[str, str, str]] = set()

    for n in outbound_raw:
        if n and n.get("id") and n.get("relationship"):
            key = (n["id"], n["relationship"], "outbound")
            if key not in seen_neighbors:
                seen_neighbors.add(key)
                neighbors.append(NeighborSummary(
                    id=n["id"],
                    label=n.get("label", ""),
                    relationship=n["relationship"],
                    direction="outbound",
                    properties=_sanitize_props(dict(n.get("properties") or {})),
                ))
    for n in inbound_raw:
        if n and n.get("id") and n.get("relationship"):
            key = (n["id"], n["relationship"], "inbound")
            if key not in seen_neighbors:
                seen_neighbors.add(key)
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
