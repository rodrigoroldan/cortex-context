"""
routes/ingest.py — Router unificado para ingestão de qualquer dimensão.

Endpoint:
  POST /api/v1/ingest/{dim_key}  → dispara ingestão da dimensão indicada

Estratégia de despacho por dim_key:
  spec              → lê filesystem local (/specs), aceita body {spec_paths: [...]}
  service           → busca AGENTS.md de cada repo via GitHub API (repo_sources config)
  temporal_workflow → busca temporal-workflows.yaml via GitHub API (temporal_workflow_manifest config)

Para adicionar suporte a uma nova dimensão: implemente um _ingest_<dim_key>() abaixo
e registre-o em _INGEST_DISPATCH.
"""
from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])
bearer = HTTPBearer()

_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


def _verify_token(
    credentials: HTTPAuthorizationCredentials = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


# ─── Response model ───────────────────────────────────────────────────────────


class IngestResponse(BaseModel):
    dim_key: str
    nodes_upserted: int
    edges_upserted: int
    message: str
    details: dict[str, Any] = {}


# ─── Ingestor: spec ───────────────────────────────────────────────────────────


async def _ingest_spec(settings) -> IngestResponse:
    """Ingesta nós :Spec lendo arquivos .md do filesystem local (/specs)."""
    from app.ingestor.spec_parser import parse_all_specs
    from app.ingestor.graph_builder import ingest_nodes, ingest_edges

    cfg = _load_config()
    specs_dir = Path(cfg.get("specs_dir", "/specs"))

    if not specs_dir.exists():
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Diretório de specs não encontrado: {specs_dir}",
        )

    nodes_list, edges_list = parse_all_specs(specs_dir)

    driver = get_driver()
    nodes_ok = await ingest_nodes(driver, nodes_list)
    edges_ok = await ingest_edges(driver, edges_list)

    return IngestResponse(
        dim_key="spec",
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=f"Ingestão de specs concluída: {nodes_ok} specs, {edges_ok} arestas",
    )


# ─── Ingestor: service ────────────────────────────────────────────────────────


async def _fetch_raw_github(
    client: httpx.AsyncClient,
    owner: str,
    repo_name: str,
    branch: str,
    path: str,
    github_token: str = "",
) -> str | None:
    """Busca arquivo via GitHub raw content API."""
    url = f"https://raw.githubusercontent.com/{owner}/{repo_name}/{branch}/{path}"
    headers = {"User-Agent": "cortex-ingestor/2.0"}
    if github_token:
        headers["Authorization"] = f"token {github_token}"
    try:
        resp = await client.get(url, headers=headers, follow_redirects=True, timeout=15.0)
        if resp.status_code == 200:
            logger.info("✅ Fetched %s/%s@%s/%s", owner, repo_name, branch, path)
            return resp.text
        logger.warning("⚠️  GitHub fetch %s/%s@%s/%s → HTTP %d", owner, repo_name, branch, path, resp.status_code)
    except httpx.RequestError as e:
        logger.error("❌ Erro ao buscar %s/%s: %s", repo_name, path, e)
    return None


async def _ingest_service(settings) -> IngestResponse:
    """Ingesta nós :Service buscando AGENTS.md de cada repo via GitHub API."""
    from app.core.parsers.agents_md_parser import AgentsMdParser
    from app.core.graph_builder import ingest_nodes, ingest_edges
    from app.core.parsers.base import NodeData
    from app.ingestor.relationship_builder import build_affects_edges

    cfg = _load_config()
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

            if ingest_strategy == "github":
                content = await _fetch_raw_github(
                    http_client, github_owner, repo_name, branch, agents_path,
                    settings.github_token,
                )

            # Fallback: volume local
            if content is None:
                local_path = repos_mount_path / repo_name / agents_path
                if local_path.exists():
                    content = local_path.read_text(encoding="utf-8")
                    logger.info("📂 Local fallback: %s", local_path)

            if content is None:
                logger.warning("⚠️  Sem conteúdo para %s/%s — ignorado", repo_name, agents_path)
                repos_failed += 1
                continue

            with tempfile.NamedTemporaryFile(
                mode="w", suffix=f"_{agents_path}", encoding="utf-8", delete=False
            ) as tmp:
                tmp.write(content)
                tmp_path = Path(tmp.name)

            try:
                if parser.can_parse(tmp_path):
                    parse_result = parser.parse(tmp_path)
                    service_nodes.extend(parse_result.nodes)
                repos_fetched += 1
            except Exception as e:
                logger.error("Erro ao parsear %s: %s", repo_name, e)
                repos_failed += 1
            finally:
                tmp_path.unlink(missing_ok=True)

    nodes_ok = await ingest_nodes(driver, service_nodes)

    # Reconstrói AFFECTS a partir dos Spec nodes já no grafo
    async with driver.session() as session:
        r = await session.run("MATCH (s:Spec) RETURN s {.*} AS props")
        spec_records = await r.data()

    spec_nodes = [
        NodeData(node_label="Spec", node_id=rr["props"]["id"], properties=rr["props"])
        for rr in spec_records
        if rr.get("props") and rr["props"].get("id")
    ]

    affects_edges = build_affects_edges(spec_nodes, service_nodes)
    edges_ok = await ingest_edges(driver, affects_edges)

    return IngestResponse(
        dim_key="service",
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=(
            f"Ingestão de services concluída: {nodes_ok} serviços, {edges_ok} AFFECTS edges"
        ),
        details={"repos_fetched": repos_fetched, "repos_failed": repos_failed},
    )


# ─── Ingestor: temporal_workflow ─────────────────────────────────────────────


async def _ingest_temporal_workflow(settings) -> IngestResponse:
    """Ingesta nós :TemporalWorkflow buscando temporal-workflows.yaml via GitHub API."""
    from app.core.parsers.temporal_workflow_yaml_parser import TemporalWorkflowYamlParser
    from app.core.graph_builder import ingest_nodes, ingest_edges

    cfg = _load_config()
    github_owner = cfg.get("github_owner", "rodrigoroldan")
    github_default_branch = cfg.get("github_default_branch", "develop")

    wf_cfg = cfg.get("temporal_workflow_manifest", {})
    repo_name = wf_cfg.get("repo", "backend-role-organizado")
    branch = wf_cfg.get("branch", github_default_branch)
    manifest_path = wf_cfg.get("path", "temporal-workflows.yaml")

    async with httpx.AsyncClient() as http_client:
        content = await _fetch_raw_github(
            http_client, github_owner, repo_name, branch, manifest_path,
            settings.github_token,
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
        mode="w", suffix="_temporal-workflows.yaml", encoding="utf-8", delete=False
    ) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        parse_result = TemporalWorkflowYamlParser().parse(tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    if not parse_result.nodes:
        return IngestResponse(
            dim_key="temporal_workflow",
            nodes_upserted=0,
            edges_upserted=0,
            message="Nenhum workflow encontrado no manifesto.",
        )

    driver = get_driver()
    nodes_ok = await ingest_nodes(driver, parse_result.nodes)
    edges_ok = await ingest_edges(driver, parse_result.edges)

    return IngestResponse(
        dim_key="temporal_workflow",
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=(
            f"Ingestão de temporal_workflow concluída: {nodes_ok} workflows, {edges_ok} arestas, "
            f"fonte: {github_owner}/{repo_name}@{branch}/{manifest_path}"
        ),
    )


# ─── Dispatch table ───────────────────────────────────────────────────────────
# Para adicionar uma nova dimensão: implemente _ingest_<dim_key>() e registre aqui.

_INGEST_DISPATCH: dict[str, Any] = {
    "spec": _ingest_spec,
    "service": _ingest_service,
    "temporal_workflow": _ingest_temporal_workflow,
}


# ─── Route ────────────────────────────────────────────────────────────────────


@router.post("/ingest/{dim_key}", response_model=IngestResponse)
async def ingest_dimension(
    dim_key: str,
    settings=Depends(get_settings),
    _token: str = Depends(_verify_token),
) -> IngestResponse:
    """
    Dispara a ingestão de uma dimensão específica pelo seu dim_key.

    dim_key deve corresponder a uma das dimensões ativas em cortex.config.yaml.
    A lógica de fetch e parse é determinada pelo dim_key — cada dimensão
    sabe onde buscar seus dados (filesystem, GitHub API, etc).

    Exemplos:
      POST /api/v1/ingest/spec
      POST /api/v1/ingest/service
      POST /api/v1/ingest/temporal_workflow
    """
    ingestor_fn = _INGEST_DISPATCH.get(dim_key)
    if ingestor_fn is None:
        cfg = _load_config()
        active = cfg.get("active_dimensions", [])
        supported = list(_INGEST_DISPATCH.keys())
        raise HTTPException(
            status_code=404,
            detail=(
                f"Dimensão '{dim_key}' não suportada para ingestão. "
                f"Suportadas: {supported}. "
                f"Ativas no config: {active}."
            ),
        )

    return await ingestor_fn(settings)
