"""
routes/services.py — Endpoints de consulta aos nós :Service.

Endpoints:
  GET  /api/v1/services              → lista todos os serviços
  GET  /api/v1/services/{service_id} → detalhe + specs que afetam o serviço (AFFECTS inbound)
  POST /api/v1/ingest/agents         → re-ingesta Service nodes (busca AGENTS.md do GitHub)
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["services"])
bearer = HTTPBearer()

# Caminho do config relativo à raiz do repo
_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


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
    repos_fetched: int
    repos_failed: int
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


def _load_cortex_config() -> dict:
    """Carrega cortex.config.yaml. Retorna dict vazio se não encontrado."""
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    logger.warning("cortex.config.yaml não encontrado em %s", _CONFIG_PATH)
    return {}


async def _fetch_agents_md_from_github(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    branch: str,
    agents_path: str,
    github_token: str = "",
) -> str | None:
    """
    Busca o conteúdo de AGENTS.md diretamente do GitHub raw content.
    Retorna o conteúdo como string, ou None em caso de erro.
    """
    url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{agents_path}"
    headers = {"User-Agent": "cortex-ingestor/2.0"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    try:
        response = await client.get(url, headers=headers, follow_redirects=True, timeout=15.0)
        if response.status_code == 200:
            logger.info("✅ Fetched %s/%s@%s/%s", owner, repo_name, branch, agents_path)
            return response.text
        logger.warning(
            "⚠️  GitHub fetch %s/%s@%s/%s → HTTP %d",
            owner, repo_name, branch, agents_path, response.status_code,
        )
    except httpx.RequestError as e:
        logger.error("❌ Erro ao buscar %s/%s: %s", repo_name, agents_path, e)
    return None


def _fetch_agents_md_from_local(
    repos_mount_path: Path,
    repo_name: str,
    agents_path: str,
) -> str | None:
    """Fallback: lê AGENTS.md do volume local /repos."""
    file_path = repos_mount_path / repo_name / agents_path
    if file_path.exists():
        logger.info("📂 Local fallback: %s", file_path)
        return file_path.read_text(encoding="utf-8")
    return None


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
    Re-ingesta todos os Service nodes buscando AGENTS.md diretamente do GitHub.

    Estratégia (em ordem de prioridade):
    1. GitHub raw content API — sempre a fonte mais atualizada
    2. Volume local /repos — fallback quando GitHub não disponível

    Não re-parseia specs — apenas atualiza os nós :Service e reconstrói AFFECTS edges.
    """
    from app.core.parsers.agents_md_parser import AgentsMdParser
    from app.core.graph_builder import ingest_nodes, ingest_edges
    from app.core.parsers.base import NodeData
    from app.ingestor.relationship_builder import build_affects_edges

    cfg = _load_cortex_config()
    repo_sources = cfg.get("repo_sources", [])
    github_owner = cfg.get("github_owner", "rodrigoroldan")
    github_default_branch = cfg.get("github_default_branch", "main")
    repos_mount_path = Path(cfg.get("repos_mount_path", "/repos"))
    ingest_strategy = cfg.get("ingest_strategy", "github")

    driver = get_driver()
    parser = AgentsMdParser()
    service_nodes: list[NodeData] = []
    repos_fetched = 0
    repos_failed = 0

    async with httpx.AsyncClient() as http_client:
        for repo_cfg in repo_sources:
            repo_name = repo_cfg.get("name", "")
            agents_path = repo_cfg.get("agents_path", "agents.md")
            branch = repo_cfg.get("branch", github_default_branch)

            content: str | None = None

            # Estratégia GitHub (primária)
            if ingest_strategy == "github":
                content = await _fetch_agents_md_from_github(
                    http_client,
                    owner=github_owner,
                    repo_name=repo_name,
                    branch=branch,
                    agents_path=agents_path,
                    github_token=settings.github_token,
                )

            # Fallback local (volume /repos)
            if content is None:
                content = _fetch_agents_md_from_local(repos_mount_path, repo_name, agents_path)

            if content is None:
                logger.warning("⚠️  Sem conteúdo para %s/%s — ignorado", repo_name, agents_path)
                repos_failed += 1
                continue

            # Salva em arquivo temporário para o parser (que espera Path)
            with tempfile.NamedTemporaryFile(
                mode="w",
                suffix=f"_{agents_path}",
                encoding="utf-8",
                delete=False,
            ) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)

            try:
                if parser.can_parse(tmp_path):
                    parse_result = parser.parse(tmp_path)
                    service_nodes.extend(parse_result.nodes)
                    repos_fetched += 1
                else:
                    logger.debug("Parser não aceita: %s", agents_path)
                    repos_fetched += 1  # conteúdo ok, apenas não é agents.md
            except Exception as e:
                logger.error("Erro ao parsear %s: %s", repo_name, e)
                repos_failed += 1
            finally:
                tmp_path.unlink(missing_ok=True)

    nodes_ok = await ingest_nodes(driver, service_nodes)
    logger.info("Service nodes upsertados: %d", nodes_ok)

    # Reconstrói AFFECTS: busca Spec nodes do grafo
    async with driver.session() as session:
        result = await session.run("MATCH (s:Spec) RETURN s {.*} AS props")
        spec_records = await result.data()

    spec_nodes = [
        NodeData(node_label="Spec", node_id=r["props"]["id"], properties=r["props"])
        for r in spec_records
        if r.get("props") and r["props"].get("id")
    ]

    affects_edges = build_affects_edges(spec_nodes, service_nodes)
    edges_ok = await ingest_edges(driver, affects_edges)

    return IngestAgentsResponse(
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        repos_fetched=repos_fetched,
        repos_failed=repos_failed,
        message=(
            f"Ingestão de agents concluída: {nodes_ok} serviços upsertados, "
            f"{edges_ok} AFFECTS edges, {repos_fetched} repos OK, {repos_failed} falhas"
        ),
    )



def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Response models ──────────────────────────────────────────────────────────
