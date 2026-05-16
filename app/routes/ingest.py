"""
routes/ingest.py — Router genérico para ingestão de qualquer dimensão.

Endpoints:
  POST /api/v1/ingest/{dim_key}   → dispara ingestão da dimensão indicada
  POST /api/v1/ingest             → dispara ingestão de TODAS as dimensões ativas
  GET  /api/v1/ingest/status      → retorna status do último ingest

A arquitetura é totalmente agnóstica ao produto:
  - Carrega a DimensionConfig do YAML da dimensão
  - Resolve o parser pelo extractor_key (built-in ou plugin)
  - Executa o parser contra os arquivos fonte
  - Upsert dos nós/arestas resultantes no Neo4j
  - Cria os índices/constraints declarados no YAML

Não existe mais lógica específica para "spec", "service", "temporal_workflow".
Tudo flui pelo mesmo pipeline genérico.
"""
from __future__ import annotations

import glob
import logging
from pathlib import Path
from typing import Any

import yaml
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from app.config import get_settings
from app.core.dimension_loader import DimensionConfig, load_dimensions
from app.core.graph_builder import ingest_edges, ingest_nodes
from app.core.parser_registry import get_parser
from app.core.parsers.manifest import IngestManifest, ManifestIngestResponse
from app.db.neo4j import apply_index, get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["ingest"])
_bearer = HTTPBearer(auto_error=False)

_CONFIG_PATH = Path(__file__).parent.parent.parent / "cortex.config.yaml"


# ─── Auth ─────────────────────────────────────────────────────────────────────


def _verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    settings=Depends(get_settings),
) -> str:
    """Valida o Bearer token quando CORTEX_API_TOKEN está configurado.
    Se CORTEX_API_TOKEN for vazio/não configurado, aceita qualquer requisição (modo aberto).
    """
    # Modo aberto: nenhum token configurado → sem autenticação
    if not settings.cortex_api_token:
        return ""
    # Token configurado mas não fornecido
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token obrigatório — configure CORTEX_API_TOKEN ou remova-o para modo aberto",
        )
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Config helpers ───────────────────────────────────────────────────────────


def _load_config() -> dict:
    if _CONFIG_PATH.exists():
        return yaml.safe_load(_CONFIG_PATH.read_text(encoding="utf-8")) or {}
    return {}


def _get_dimensions_dir(cfg: dict) -> Path:
    dimensions_dir = Path(cfg.get("dimensions_dir", "app/dimensions"))
    if not dimensions_dir.is_absolute():
        dimensions_dir = Path(__file__).parent.parent.parent / dimensions_dir
    return dimensions_dir


def _get_plugins_dir(cfg: dict) -> Path | None:
    plugins_dir_str = cfg.get("plugins_dir", "plugins")
    plugins_dir = Path(plugins_dir_str)
    if not plugins_dir.is_absolute():
        plugins_dir = Path(__file__).parent.parent.parent / plugins_dir
    return plugins_dir if plugins_dir.exists() else None


# ─── Response models ──────────────────────────────────────────────────────────


class IngestResponse(BaseModel):
    dim_key: str
    nodes_upserted: int
    edges_upserted: int
    indexes_applied: int
    message: str
    details: dict[str, Any] = {}


class BulkIngestResponse(BaseModel):
    dimensions_processed: int
    total_nodes: int
    total_edges: int
    results: list[IngestResponse]


# ─── Core ingest pipeline ─────────────────────────────────────────────────────


async def _run_ingest_pipeline(
    dim_config: DimensionConfig,
    plugins_dir: Path | None = None,
) -> IngestResponse:
    """
    Executa o pipeline completo de ingestão para uma única dimensão:
      1. Aplica constraints/índices declarados no YAML
      2. Resolve o parser
      3. Descobre arquivos fonte (filesystem)
      4. Executa o parser em cada arquivo
      5. Upsert nós + arestas no Neo4j
    """
    driver = get_driver()

    # 1. Aplicar índices/constraints declarados no dimension YAML
    indexes_applied = 0
    for idx in dim_config.indexes:
        if idx.cypher:
            await apply_index(idx.cypher)
            indexes_applied += 1

    # 2. Resolver parser
    parser = get_parser(dim_config.parser, plugins_dir)
    if parser is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Parser '{dim_config.parser}' não encontrado para dimensão '{dim_config.dimension}'",
        )

    # 3. Descobrir arquivos fonte
    source_path = Path(dim_config.source_path) if dim_config.source_path else None
    file_paths: list[Path] = []

    if dim_config.source_type == "filesystem" and source_path and source_path.exists():
        for pattern in dim_config.source_patterns:
            matched = [Path(p) for p in glob.glob(str(source_path / pattern), recursive=True)]
            file_paths.extend(matched)
        # Deduplicar mantendo ordem
        seen: set[Path] = set()
        file_paths = [p for p in file_paths if not (p in seen or seen.add(p))]  # type: ignore
    else:
        if dim_config.source_type == "filesystem" and source_path and not source_path.exists():
            logger.warning(
                "Diretório fonte não encontrado para dimensão '%s': %s",
                dim_config.dimension,
                source_path,
            )

    # 4. Parsear arquivos
    from app.core.parsers.base import NodeData, EdgeData

    all_nodes: list[NodeData] = []
    all_edges: list[EdgeData] = []
    files_parsed = 0
    files_failed = 0

    for file_path in file_paths:
        if not parser.can_parse(file_path):
            continue
        try:
            result = parser.parse(file_path, dim_config)
            all_nodes.extend(result.nodes)
            all_edges.extend(result.edges)
            files_parsed += 1
        except Exception as e:
            logger.error("Erro ao parsear %s: %s", file_path, e)
            files_failed += 1

    # 5. Upsert no Neo4j
    nodes_ok = await ingest_nodes(driver, all_nodes)
    edges_ok = await ingest_edges(driver, all_edges)

    return IngestResponse(
        dim_key=dim_config.dimension,
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        indexes_applied=indexes_applied,
        message=(
            f"Dimensão '{dim_config.dimension}' ingerida: "
            f"{nodes_ok} nós, {edges_ok} arestas, {indexes_applied} índices"
        ),
        details={
            "files_parsed": files_parsed,
            "files_failed": files_failed,
            "source_path": str(source_path) if source_path else None,
            "parser": dim_config.parser,
            "pillar": dim_config.pillar,
        },
    )


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.post(
    "/ingest/{dim_key}",
    response_model=IngestResponse,
    summary="Ingesta uma dimensão específica",
    description="Carrega a DimensionConfig do YAML, resolve o parser e executa o pipeline de ingestão.",
)
async def ingest_dimension(
    dim_key: str,
    _token: str = Depends(_verify_token),
):
    cfg = _load_config()
    dimensions_dir = _get_dimensions_dir(cfg)
    plugins_dir = _get_plugins_dir(cfg)

    active_dimensions = cfg.get("active_dimensions", [])
    if dim_key not in active_dimensions:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=(
                f"Dimensão '{dim_key}' não está em active_dimensions. "
                f"Disponíveis: {active_dimensions}"
            ),
        )

    dims = load_dimensions(dimensions_dir, [dim_key])
    if not dims:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Dimension YAML não encontrado ou inválido para '{dim_key}'",
        )

    return await _run_ingest_pipeline(dims[0], plugins_dir)


@router.post(
    "/ingest",
    response_model=BulkIngestResponse,
    summary="Ingesta todas as dimensões ativas",
    description="Executa o pipeline de ingestão para cada dimensão em active_dimensions.",
)
async def ingest_all(
    _token: str = Depends(_verify_token),
):
    cfg = _load_config()
    dimensions_dir = _get_dimensions_dir(cfg)
    plugins_dir = _get_plugins_dir(cfg)
    active_dimensions = cfg.get("active_dimensions", [])

    dims = load_dimensions(dimensions_dir, active_dimensions)
    if not dims:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Nenhuma dimensão carregada. Verifique cortex.config.yaml e app/dimensions/.",
        )

    results: list[IngestResponse] = []
    total_nodes = 0
    total_edges = 0

    for dim in dims:
        try:
            result = await _run_ingest_pipeline(dim, plugins_dir)
            results.append(result)
            total_nodes += result.nodes_upserted
            total_edges += result.edges_upserted
        except Exception as e:
            logger.error("Erro ao ingerir dimensão '%s': %s", dim.dimension, e)
            results.append(IngestResponse(
                dim_key=dim.dimension,
                nodes_upserted=0,
                edges_upserted=0,
                indexes_applied=0,
                message=f"Erro: {e}",
            ))

    # Pós-processamento: AFFECTS edges (Spec → Service) via Cypher
    affects_created = 0
    try:
        driver = get_driver()
        async with driver.session() as session:
            result_affects = await session.run(
                """
                MATCH (spec:Spec) WHERE spec.repos IS NOT NULL AND size(spec.repos) > 0
                UNWIND spec.repos AS repo
                MATCH (svc:Service) WHERE svc.repo = repo
                MERGE (spec)-[r:AFFECTS]->(svc)
                ON CREATE SET r.created_at = datetime()
                RETURN count(r) AS created
                """
            )
            record = await result_affects.single()
            affects_created = record["created"] if record else 0
            logger.info("AFFECTS edges (Spec→Service): %d criadas/verificadas", affects_created)
    except Exception as e:
        logger.error("Erro ao criar AFFECTS edges: %s", e)

    total_edges += affects_created

    return BulkIngestResponse(
        dimensions_processed=len(dims),
        total_nodes=total_nodes,
        total_edges=total_edges,
        results=results,
    )


# ─── Manifest endpoint ───────────────────────────────────────────────────────


@router.post(
    "/ingest/manifest",
    response_model=ManifestIngestResponse,
    summary="Ingesta via manifesto pré-computado (CLI agent)",
    description=(
        "Aceita um IngestManifest JSON com nós e arestas pré-computados pelo "
        "Cortex Ingestion Agent CLI (gerado via git diff). "
        "O servidor persiste os dados diretamente, sem necessidade de parsear arquivos. "
        "Propriedades bitemporais (ingested_at, valid_from, commit_sha) são adicionadas automaticamente."
    ),
)
async def ingest_manifest(
    manifest: IngestManifest,
    _token: str = Depends(_verify_token),
):
    from app.core.parsers.base import EdgeData, NodeData

    driver = get_driver()

    # Converter ManifestNodes → NodeData
    nodes = [
        NodeData(
            node_labels=mn.node_labels,
            node_id=mn.node_id,
            properties={"id": mn.node_id, **mn.properties},
        )
        for mn in manifest.nodes
    ]

    # Converter ManifestEdges → EdgeData
    edges = [
        EdgeData(
            from_id=me.from_id,
            to_id=me.to_id,
            relationship=me.relationship,
            properties=me.properties,
        )
        for me in manifest.edges
    ]

    nodes_ok = await ingest_nodes(driver, nodes, commit_sha=manifest.commit_sha)
    edges_ok = await ingest_edges(driver, edges)

    return ManifestIngestResponse(
        source=manifest.source,
        commit_sha=manifest.commit_sha,
        nodes_upserted=nodes_ok,
        edges_upserted=edges_ok,
        message=(
            f"Manifesto '{manifest.source}' ingerido: "
            f"{nodes_ok} nós, {edges_ok} arestas"
            + (f" (commit: {manifest.commit_sha[:8]})" if manifest.commit_sha else "")
        ),
    )


# ─── Reset endpoint ──────────────────────────────────────────────────────────


class GraphResetResponse(BaseModel):
    nodes_deleted: int
    message: str


@router.delete(
    "/graph",
    response_model=GraphResetResponse,
    summary="Limpa todos os nós e arestas do grafo",
    description=(
        "Remove todos os nós e relacionamentos do Neo4j (MATCH (n) DETACH DELETE n). "
        "Operação destrutiva e irreversível — requer token de autenticação. "
        "Útil para resetar o grafo antes de uma re-ingestão completa."
    ),
)
async def reset_graph(
    _token: str = Depends(_verify_token),
) -> GraphResetResponse:
    driver = get_driver()
    async with driver.session() as session:
        result = await session.run(
            "MATCH (n) DETACH DELETE n RETURN count(n) AS deleted"
        )
        record = await result.single()
        deleted = record["deleted"] if record else 0

    logger.warning("Graph reset: %d nodes deleted", deleted)
    return GraphResetResponse(
        nodes_deleted=deleted,
        message=f"Grafo limpo: {deleted} nós removidos",
    )

