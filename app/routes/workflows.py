"""
routes/workflows.py — Endpoints para TemporalWorkflow nodes no grafo Cortex.

Endpoints:
  POST /api/v1/ingest/workflows      → ingesta TemporalWorkflow nodes do backend
  GET  /api/v1/workflows             → lista todos os workflows (filtros opcionais)
  GET  /api/v1/workflows/{id}        → detalhe de um workflow + spec + service
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

router = APIRouter(tags=["workflows"])
bearer = HTTPBearer()

_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


def _load_cortex_config() -> dict:
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    logger.warning("cortex.config.yaml não encontrado em %s", _CONFIG_PATH)
    return {}


# ─── Response models ──────────────────────────────────────────────────────────


class WorkflowSummary(BaseModel):
    id: str
    name: str
    category: str
    status: str
    service_id: str
    schedule: str | None = None
    mode_resolver: str | None = None
    replaces: str | None = None
    spec_ids: list[str] = []


class WorkflowDetailResponse(BaseModel):
    workflow: WorkflowSummary
    service: dict | None = None
    implements_specs: list[dict] = []


class IngestWorkflowsResponse(BaseModel):
    nodes_upserted: int
    edges_upserted: int
    message: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _props_to_workflow(props: dict) -> WorkflowSummary:
    return WorkflowSummary(
        id=props.get("id", ""),
        name=props.get("name", ""),
        category=props.get("category", ""),
        status=props.get("status", "active"),
        service_id=props.get("service_id", ""),
        schedule=props.get("schedule"),
        mode_resolver=props.get("mode_resolver"),
        replaces=props.get("replaces"),
        spec_ids=props.get("spec_ids", []),
    )


async def _fetch_manifest_from_github(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    branch: str,
    manifest_path: str,
    github_token: str = "",
) -> str | None:
    """Busca o arquivo manifest via GitHub raw content API."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{manifest_path}"
    headers = {"User-Agent": "cortex-ingestor/2.0"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"

    try:
        response = await client.get(url, headers=headers, follow_redirects=True, timeout=15.0)
        if response.status_code == 200:
            logger.info("✅ Fetched %s/%s@%s/%s", owner, repo_name, branch, manifest_path)
            return response.text
        logger.warning(
            "⚠️  GitHub fetch %s/%s@%s/%s → HTTP %d",
            owner, repo_name, branch, manifest_path, response.status_code,
        )
    except httpx.RequestError as e:
        logger.error("❌ Erro ao buscar %s/%s: %s", repo_name, manifest_path, e)
    return None


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.post("/ingest/workflows", response_model=IngestWorkflowsResponse)
async def ingest_workflows(
    settings=Depends(get_settings),
    _token: str = Depends(_verify_token),
) -> IngestWorkflowsResponse:
    """
    Ingesta TemporalWorkflow nodes buscando temporal-workflows.yaml do repositório
    de backend via GitHub raw content API.

    Cria nós :TemporalWorkflow e arestas:
    - (:TemporalWorkflow)-[:BELONGS_TO]->(:Service)
    - (:Service)-[:HAS_WORKFLOW]->(:TemporalWorkflow)
    - (:TemporalWorkflow)-[:IMPLEMENTS]->(:Spec)  [apenas se spec_ids preenchidos]
    """
    from app.core.parsers.temporal_workflow_yaml_parser import TemporalWorkflowYamlParser
    from app.core.graph_builder import ingest_nodes, ingest_edges

    cfg = _load_cortex_config()
    github_owner = cfg.get("github_owner", "rodrigoroldan")
    github_default_branch = cfg.get("github_default_branch", "develop")

    wf_cfg = cfg.get("temporal_workflow_manifest", {})
    repo_name = wf_cfg.get("repo", "backend-role-organizado")
    branch = wf_cfg.get("branch", github_default_branch)
    manifest_path = wf_cfg.get("path", "temporal-workflows.yaml")

    async with httpx.AsyncClient() as http_client:
        content = await _fetch_manifest_from_github(
            http_client,
            owner=github_owner,
            repo_name=repo_name,
            branch=branch,
            manifest_path=manifest_path,
            github_token=settings.github_token,
        )

    if content is None:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=(
                f"Falha ao buscar {manifest_path} de {github_owner}/{repo_name}@{branch}. "
                "Verifique GITHUB_TOKEN e a existência do arquivo."
            ),
        )

    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix="_temporal-workflows.yaml",
        encoding="utf-8",
        delete=False,
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        parser = TemporalWorkflowYamlParser()
        parse_result = parser.parse(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not parse_result.nodes:
        return IngestWorkflowsResponse(
            nodes_upserted=0,
            edges_upserted=0,
            message="Nenhum workflow encontrado no manifesto.",
        )

    driver = get_driver()
    nodes_ok = await ingest_nodes(driver, parse_result.nodes)
    edges_ok = await ingest_edges(driver, parse_result.edges)

    logger.info(
        "Workflows ingestados: %d nós, %d arestas (repo: %s@%s/%s)",
        nodes_ok, edges_ok, repo_name, branch, manifest_path,
    )

    return IngestWorkflowsResponse(
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=(
            f"Ingestão de workflows concluída: {nodes_ok} workflows upsertados, "
            f"{edges_ok} arestas (BELONGS_TO + HAS_WORKFLOW + IMPLEMENTS), "
            f"fonte: {github_owner}/{repo_name}@{branch}/{manifest_path}"
        ),
    )


@router.get("/workflows", response_model=list[WorkflowSummary])
async def list_workflows(
    category: str | None = None,
    status: str | None = None,
    _token: str = Depends(_verify_token),
) -> list[WorkflowSummary]:
    """
    Lista todos os workflows indexados no grafo.

    Filtros opcionais:
    - ?category=scheduled|event-triggered|sandbox
    - ?status=active|deprecated
    """
    driver = get_driver()

    where_clauses: list[str] = []
    params: dict = {}
    if category:
        where_clauses.append("w.category = $category")
        params["category"] = category
    if status:
        where_clauses.append("w.status = $status")
        params["status"] = status

    where_str = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    cypher = f"""
        MATCH (w:TemporalWorkflow)
        {where_str}
        RETURN w {{.*}} AS props
        ORDER BY w.category, w.name
    """

    async with driver.session() as session:
        result = await session.run(cypher, **params)
        records = await result.data()

    return [_props_to_workflow(r["props"]) for r in records]


@router.get("/workflows/{workflow_id}", response_model=WorkflowDetailResponse)
async def get_workflow(
    workflow_id: str,
    _token: str = Depends(_verify_token),
) -> WorkflowDetailResponse:
    """
    Retorna um workflow pelo ID com:
    - Serviço ao qual pertence (BELONGS_TO)
    - Specs implementadas (IMPLEMENTS)
    """
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            """
            MATCH (w:TemporalWorkflow {id: $id})
            OPTIONAL MATCH (w)-[:BELONGS_TO]->(svc:Service)
            OPTIONAL MATCH (w)-[:IMPLEMENTS]->(spec:Spec)
            RETURN w {.*} AS workflow_props,
                   svc {.*} AS service_props,
                   collect(DISTINCT {
                       id: spec.id,
                       number: spec.number,
                       title: spec.title,
                       status: spec.status
                   }) AS implements_specs
            """,
            id=workflow_id,
        )
        records = await result.data()

    if not records or not records[0]["workflow_props"]:
        raise HTTPException(
            status_code=404,
            detail=f"Workflow '{workflow_id}' não encontrado",
        )

    row = records[0]
    specs = [s for s in row.get("implements_specs", []) if s and s.get("id")]

    return WorkflowDetailResponse(
        workflow=_props_to_workflow(row["workflow_props"]),
        service=row.get("service_props"),
        implements_specs=specs,
    )
