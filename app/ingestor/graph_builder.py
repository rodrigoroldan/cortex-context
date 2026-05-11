"""
graph_builder.py — Persiste nodes e edges no Neo4j.
"""
from __future__ import annotations

import logging

from neo4j import AsyncDriver

from app.models.spec import SpecEdge, SpecNode

logger = logging.getLogger(__name__)


async def upsert_spec(driver: AsyncDriver, node: SpecNode) -> None:
    """Cria ou atualiza um nó Spec no Neo4j (MERGE por id)."""
    props = node.to_neo4j_props()
    async with driver.session() as session:
        await session.run(
            """
            MERGE (s:Spec {id: $id})
            SET s += $props
            """,
            id=node.id,
            props=props,
        )


async def upsert_edge(driver: AsyncDriver, edge: SpecEdge) -> None:
    """Cria ou atualiza uma aresta entre dois nós Spec."""
    async with driver.session() as session:
        await session.run(
            f"""
            MATCH (a:Spec {{id: $from_id}})
            MATCH (b:Spec {{id: $to_id}})
            MERGE (a)-[r:{edge.relationship}]->(b)
            SET r.weight = $weight
            """,
            from_id=edge.from_id,
            to_id=edge.to_id,
            weight=edge.weight,
        )


async def ingest_nodes(driver: AsyncDriver, nodes: list[SpecNode]) -> int:
    """Upserta uma lista de nós. Retorna o número processado."""
    count = 0
    for node in nodes:
        try:
            await upsert_spec(driver, node)
            count += 1
        except Exception as e:
            logger.error("Erro ao upsert spec %s: %s", node.id, e)
    return count


async def ingest_edges(driver: AsyncDriver, edges: list[SpecEdge]) -> int:
    """Upserta uma lista de arestas. Retorna o número processado."""
    count = 0
    for edge in edges:
        try:
            await upsert_edge(driver, edge)
            count += 1
        except Exception as e:
            logger.error("Erro ao upsert edge %s->%s: %s", edge.from_id, edge.to_id, e)
    return count
