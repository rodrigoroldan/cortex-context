"""
cli.py — CLI de ingestão para uso manual e GitHub Actions.

Uso:
  python -m cortex.ingestor.cli ingest --specs-dir /path/to/specs
  python -m cortex.ingestor.cli ingest --specs-dir /path/to/specs --dry-run
  python -m cortex.ingestor.cli ingest --specs-dir /path/to/specs --only 070,071,072
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Optional

import typer

from app.config import get_settings
from app.db.neo4j import close_driver, init_driver
from app.ingestor.graph_builder import ingest_edges, ingest_nodes
from app.ingestor.spec_parser import parse_all_specs, parse_spec_dir

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

app = typer.Typer(help="Cortex Ingestor CLI")


@app.command()
def ingest(
    specs_dir: Path = typer.Option(..., help="Caminho para a pasta specs/"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Simula sem gravar no Neo4j"),
    only: Optional[str] = typer.Option(
        None, help="Números de specs a processar (CSV, ex: 070,071,072)"
    ),
) -> None:
    """Parseia specs e ingesta no Neo4j."""
    asyncio.run(_ingest(specs_dir, dry_run, only))


async def _ingest(specs_dir: Path, dry_run: bool, only: Optional[str]) -> None:
    if not specs_dir.exists():
        typer.echo(f"❌ Diretório não encontrado: {specs_dir}", err=True)
        raise typer.Exit(1)

    settings = get_settings()

    # Parseia todos os specs
    logger.info("🔍 Parseando specs em %s ...", specs_dir)
    nodes, edges = parse_all_specs(specs_dir)

    # Filtra por número se --only foi passado
    if only:
        filter_nums = {int(n.strip()) for n in only.split(",")}
        nodes = [n for n in nodes if n.number in filter_nums]
        node_ids = {n.id for n in nodes}
        edges = [e for e in edges if e.from_id in node_ids or e.to_id in node_ids]

    logger.info("📊 Encontrados: %d nós, %d arestas", len(nodes), len(edges))

    if dry_run:
        typer.echo("\n=== DRY RUN — nenhuma gravação ===\n")
        for node in nodes:
            typer.echo(f"  [{node.status:12s}] {node.id}  {node.title[:60]}")
            if node.repos:
                typer.echo(f"               repos: {', '.join(node.repos)}")
        typer.echo(f"\n  Arestas:")
        for edge in edges:
            typer.echo(f"  {edge.from_id} --[{edge.relationship}]--> {edge.to_id}")
        typer.echo(f"\nTotal: {len(nodes)} nós, {len(edges)} arestas")
        return

    logger.info("🔌 Conectando ao Neo4j em %s ...", settings.neo4j_uri)
    await init_driver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)

    try:
        nodes_ok = await ingest_nodes(get_driver_from_context(), nodes)
        edges_ok = await ingest_edges(get_driver_from_context(), edges)
        logger.info("✅ Ingestão concluída: %d nós, %d arestas gravados", nodes_ok, edges_ok)
    finally:
        await close_driver()


def get_driver_from_context():
    from app.db.neo4j import get_driver
    return get_driver()


if __name__ == "__main__":
    app()
