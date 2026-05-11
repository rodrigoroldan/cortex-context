"""
ingestor/ingest_pipeline.py — Pipeline de ingestão dimension-agnostic.

Orquestra:
  1. Carregamento de dimension configs (YAMLs)
  2. Glob de arquivos por dimension
  3. Parse via DimensionParser
  4. Upsert de nós no Neo4j
  5. Criação de constraints/índices declarados nos YAMLs

Arestas entre dimensões (ex: AFFECTS) são geradas pelo relationship_builder.py
em pós-processamento, após todos os nós serem upsertados.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from neo4j import AsyncDriver

from app.core.dimension_loader import DimensionConfig, load_dimensions
from app.core.graph_builder import create_constraint_if_not_exists, ingest_edges, ingest_nodes
from app.core.parser_registry import get_parser
from app.core.parsers.base import NodeData

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    nodes_upserted: int = 0
    edges_upserted: int = 0
    constraints_created: int = 0
    errors: list[str] = field(default_factory=list)


async def _apply_constraints(driver: AsyncDriver, config: DimensionConfig) -> int:
    """Cria constraints e índices declarados no dimension YAML."""
    count = 0
    for idx in config.indexes:
        if idx.cypher:
            await create_constraint_if_not_exists(driver, idx.cypher)
            count += 1
    return count


def _glob_files_for_dimension(config: DimensionConfig, base_dir: Path) -> list[Path]:
    """Encontra todos os arquivos que batem com os padrões do dimension config."""
    files: list[Path] = []
    for pattern in config.source_patterns:
        matched = list(base_dir.glob(pattern))
        files.extend(matched)
    # Deduplica preservando ordem
    seen: set[Path] = set()
    deduped: list[Path] = []
    for f in files:
        if f not in seen:
            seen.add(f)
            deduped.append(f)
    return deduped


async def run_dimension_pipeline(
    driver: AsyncDriver,
    config: DimensionConfig,
    base_dir: Path,
    dry_run: bool = False,
) -> tuple[list[NodeData], PipelineResult]:
    """
    Executa o pipeline para uma única dimensão.

    Retorna (lista_de_nodes, resultado) para uso pelo relationship_builder.
    """
    result = PipelineResult()
    parser = get_parser(config.parser)

    if parser is None:
        msg = f"Parser '{config.parser}' não encontrado no registry para dimensão '{config.dimension}'"
        logger.error(msg)
        result.errors.append(msg)
        return [], result

    # Cria constraints/índices antes da ingestão
    if not dry_run:
        result.constraints_created = await _apply_constraints(driver, config)

    # Glob + parse
    files = _glob_files_for_dimension(config, base_dir)
    logger.info("[%s] %d arquivo(s) encontrado(s)", config.dimension, len(files))

    all_nodes: list[NodeData] = []
    all_edges = []

    for file_path in files:
        if not parser.can_parse(file_path):
            logger.debug("[%s] Pulando %s (can_parse=False)", config.dimension, file_path)
            continue
        try:
            parse_result = parser.parse(file_path)
            all_nodes.extend(parse_result.nodes)
            all_edges.extend(parse_result.edges)
        except Exception as e:
            msg = f"[{config.dimension}] Erro ao parsear {file_path}: {e}"
            logger.error(msg)
            result.errors.append(msg)

    logger.info("[%s] Parseados: %d nós, %d arestas diretas", config.dimension, len(all_nodes), len(all_edges))

    if not dry_run:
        result.nodes_upserted = await ingest_nodes(driver, all_nodes)
        result.edges_upserted = await ingest_edges(driver, all_edges)

    return all_nodes, result


async def run_full_pipeline(
    driver: AsyncDriver,
    dimensions_dir: Path,
    active_dimensions: list[str],
    base_dirs_by_dimension: dict[str, Path],
    dry_run: bool = False,
) -> PipelineResult:
    """
    Executa o pipeline completo para todas as dimensões ativas.

    Args:
        driver: Neo4j async driver já inicializado
        dimensions_dir: Diretório contendo os YAMLs de dimension
        active_dimensions: Lista de nomes de dimensões (ex: ["spec", "service"])
        base_dirs_by_dimension: Diretório base de busca de arquivos por dimensão
            ex: {"spec": Path("/specs"), "service": Path("/repos")}
        dry_run: Se True, não grava no Neo4j

    Returns:
        PipelineResult agregado de todas as dimensões
    """
    configs = load_dimensions(dimensions_dir, active_dimensions)
    aggregated = PipelineResult()
    all_nodes_by_dimension: dict[str, list[NodeData]] = {}

    for config in configs:
        base_dir = base_dirs_by_dimension.get(config.dimension, Path("."))
        nodes, result = await run_dimension_pipeline(driver, config, base_dir, dry_run)
        all_nodes_by_dimension[config.dimension] = nodes
        aggregated.nodes_upserted += result.nodes_upserted
        aggregated.edges_upserted += result.edges_upserted
        aggregated.constraints_created += result.constraints_created
        aggregated.errors.extend(result.errors)

    # Pós-processamento: AFFECTS edges (Spec → Service)
    if not dry_run:
        from app.ingestor.relationship_builder import build_affects_edges
        spec_nodes = all_nodes_by_dimension.get("spec", [])
        service_nodes = all_nodes_by_dimension.get("service", [])
        if spec_nodes and service_nodes:
            affects_edges = build_affects_edges(spec_nodes, service_nodes)
            logger.info("Gerando %d AFFECTS edges (Spec → Service)", len(affects_edges))
            aggregated.edges_upserted += await ingest_edges(driver, affects_edges)

    return aggregated
