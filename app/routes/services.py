"""
routes/services.py — Endpoints de consulta aos nós :Service.

Endpoints:
  GET  /api/v1/services              → lista todos os serviços
  GET  /api/v1/services/{service_id} → detalhe + specs que afetam o serviço (AFFECTS inbound)
  POST /api/v1/ingest/agents         → re-ingesta apenas Service nodes (AGENTS.md)
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])
bearer = HTTPBearer()


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Response models ──────────────────────────────────────────────────────────


class ServiceSummary(BaseModel):
    id: str
    name: str
    repo: str
    tech: list[str] = []
    capabilities: list[str] = []
    port: int | None = None
    url: str = ""
    version: str = ""


class ServiceDetailResponse(BaseModel):
    service: ServiceSummary
    affecting_specs: list[dict]  # [{id, title, status, number}]
    affecting_specs_count: int


class IngestAgentsResponse(BaseModel):
    nodes_upserted: int
    edges_upserted: int
    message: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _row_to_service(props: dict) -> ServiceSummary:
    return ServiceSummary(
        id=props.get("id", ""),
        name=props.get("name", ""),
        repo=props.get("repo", ""),
        tech=props.get("tech", []),
        capabilities=props.get("capabilities", []),
        port=props.get("port"),
        url=props.get("url", ""),
        version=props.get("version", ""),
    )


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.get("/services", response_model=list[ServiceSummary])
async def list_services(
    _token: str = Depends(_verify_token),
) -> list[ServiceSummary]:
    """Lista todos os serviços indexados no grafo."""
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (s:Service) RETURN s {.*} AS props ORDER BY s.name"
        )
        records = await result.data()
    return [_row_to_service(r["props"]) for r in records]


@router.get("/services/{service_id}", response_model=ServiceDetailResponse)
async def get_service(
    service_id: str,
    _token: str = Depends(_verify_token),
) -> ServiceDetailResponse:
    """
    Retorna um serviço pelo ID com todas as specs que o afetam via AFFECTS.

    Exemplo: GET /api/v1/services/service-bff
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (svc:Service {id: $service_id})
            OPTIONAL MATCH (spec:Spec)-[:AFFECTS]->(svc)
            RETURN svc {.*} AS service_props,
                   collect(DISTINCT {
                       id: spec.id,
                       number: spec.number,
                       title: spec.title,
                       status: spec.status,
                       summary: spec.summary
                   }) AS affecting_specs
            """,
            service_id=service_id,
        )
        records = await result.data()

    if not records or not records[0]["service_props"]:
        raise HTTPException(status_code=404, detail=f"Serviço '{service_id}' não encontrado")

    row = records[0]
    affecting = [s for s in row.get("affecting_specs", []) if s and s.get("id")]

    return ServiceDetailResponse(
        service=_row_to_service(row["service_props"]),
        affecting_specs=affecting,
        affecting_specs_count=len(affecting),
    )


@router.post("/ingest/agents", response_model=IngestAgentsResponse)
async def ingest_agents(
    settings=Depends(get_settings),
    _token: str = Depends(_verify_token),
) -> IngestAgentsResponse:
    """
    Re-ingesta todos os Service nodes a partir dos AGENTS.md dos repos configurados.

    Usado pelo GitHub Actions quando um AGENTS.md é modificado.
    Não re-parseia specs — apenas atualiza os nós :Service e reconstrói AFFECTS edges.
    """
    from pathlib import Path as PathLib

    import yaml

    from app.core.parsers.agents_md_parser import AgentsMdParser
    from app.core.graph_builder import ingest_nodes, ingest_edges
    from app.core.parsers.base import NodeData
    from app.ingestor.relationship_builder import build_affects_edges

    driver = get_driver()
    parser = AgentsMdParser()

    # Carrega a lista de repos do cortex.config.yaml
    config_path = PathLib(settings.cortex_config_path) if hasattr(settings, "cortex_config_path") else PathLib("cortex.config.yaml")
    if not config_path.exists():
        # Fallback: usa lista estática dos repos conhecidos
        repo_sources = [
            {"name": "backend-role-organizado", "service_id": "service-backend", "agents_path": "AGENTS.md"},
            {"name": "bff-role-organizado", "service_id": "service-bff", "agents_path": "agents.md"},
            {"name": "webview-role-organizado", "service_id": "service-webview", "agents_path": "agents.md"},
            {"name": "frontend-admin-role-organizado", "service_id": "service-admin", "agents_path": "agents.md"},
            {"name": "app-android-role-organizado", "service_id": "service-android", "agents_path": "agents.md"},
            {"name": "app-ios-role-organizado", "service_id": "service-ios", "agents_path": "agents.md"},
            {"name": "lambda-notifications-role-organizado", "service_id": "service-lambda", "agents_path": "AGENTS.md"},
            {"name": "landing-role-organizado", "service_id": "service-landing", "agents_path": "agents.md"},
        ]
        repos_mount_path = PathLib("/repos")
    else:
        cfg = yaml.safe_load(config_path.read_text(encoding="utf-8"))
        repo_sources = cfg.get("repo_sources", [])
        repos_mount_path = PathLib(cfg.get("repos_mount_path", "/repos"))

    service_nodes: list[NodeData] = []

    for repo_cfg in repo_sources:
        repo_name = repo_cfg.get("name", "")
        agents_path = repo_cfg.get("agents_path", "agents.md")
        file_path = repos_mount_path / repo_name / agents_path

        if not file_path.exists():
            logger.warning("AGENTS.md não encontrado: %s", file_path)
            continue

        if not parser.can_parse(file_path):
            logger.debug("Parser não aceita: %s", file_path)
            continue

        try:
            parse_result = parser.parse(file_path)
            service_nodes.extend(parse_result.nodes)
        except Exception as e:
            logger.error("Erro ao parsear %s: %s", file_path, e)

    nodes_ok = await ingest_nodes(driver, service_nodes)
    logger.info("Service nodes upsertados: %d", nodes_ok)

    # Reconstrói AFFECTS: busca Spec nodes do grafo
    async with driver.session() as session:
        result = await session.run(
            "MATCH (s:Spec) RETURN s {.*} AS props"
        )
        spec_records = await result.data()

    from app.core.parsers.base import NodeData as ND
    spec_nodes = [
        ND(node_label="Spec", node_id=r["props"]["id"], properties=r["props"])
        for r in spec_records
        if r.get("props") and r["props"].get("id")
    ]

    affects_edges = build_affects_edges(spec_nodes, service_nodes)
    edges_ok = await ingest_edges(driver, affects_edges)

    return IngestAgentsResponse(
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=f"Ingestão de agents concluída: {nodes_ok} serviços, {edges_ok} AFFECTS edges",
    )
