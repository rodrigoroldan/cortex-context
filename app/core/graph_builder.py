"""
core/graph_builder.py — Persiste nós e arestas no Neo4j com suporte a multi-labels.

Estratégia de multi-labels (I.S.I.R):
  - MERGE na label primária + id (ex: MERGE (n:Spec {id: $id}))
  - SET adiciona o pilar como label extra (ex: SET n:Intent)
  - Qualquer número de labels extras é suportado

Isso preserva a semântica do MERGE (chave única) enquanto
adiciona a camada de pilares para queries cross-dimensional.
"""
from __future__ import annotations

import logging

from neo4j import AsyncDriver

from app.core.parsers.base import EdgeData, NodeData

logger = logging.getLogger(__name__)


async def upsert_node(driver: AsyncDriver, node: NodeData) -> None:
    """
    Cria ou atualiza um nó no Neo4j com suporte a multi-labels.

    MERGE usa a primary_label + id como chave de upsert.
    Labels adicionais (ex: pilar Intent/System) são aplicados via SET.
    """
    primary = node.primary_label
    extra_labels = [lbl for lbl in node.node_labels if lbl != primary]

    set_labels_clause = f"SET n:{':'.join(extra_labels)}" if extra_labels else ""

    async with driver.session() as session:
        await session.run(
            f"""
            MERGE (n:{primary} {{id: $id}})
            {set_labels_clause}
            SET n += $props
            """,
            id=node.node_id,
            props=node.properties,
        )


async def upsert_edge(driver: AsyncDriver, edge: EdgeData) -> None:
    """
    Cria ou atualiza uma aresta entre dois nós quaisquer.

    Localiza os nós pelo id (independente de labels), mantendo agnósticidade.
    """
    async with driver.session() as session:
        await session.run(
            f"""
            MATCH (a {{id: $from_id}})
            MATCH (b {{id: $to_id}})
            MERGE (a)-[r:{edge.relationship}]->(b)
            SET r += $props
            """,
            from_id=edge.from_id,
            to_id=edge.to_id,
            props=edge.properties,
        )


async def ingest_nodes(driver: AsyncDriver, nodes: list[NodeData]) -> int:
    """Upserta uma lista de NodeData. Retorna o número de nós processados com sucesso."""
    count = 0
    for node in nodes:
        try:
            await upsert_node(driver, node)
            count += 1
        except Exception as e:
            logger.error(
                "Erro ao upsert nó %s (%s): %s",
                node.node_id,
                ":".join(node.node_labels),
                e,
            )
    return count


async def ingest_edges(driver: AsyncDriver, edges: list[EdgeData]) -> int:
    """Upserta uma lista de EdgeData. Retorna o número de arestas processadas com sucesso."""
    count = 0
    for edge in edges:
        try:
            await upsert_edge(driver, edge)
            count += 1
        except Exception as e:
            logger.error(
                "Erro ao upsert aresta %s→%s [%s]: %s",
                edge.from_id,
                edge.to_id,
                edge.relationship,
                e,
            )
    return count


async def create_constraint_if_not_exists(driver: AsyncDriver, cypher: str) -> None:
    """Executa um Cypher de criação de constraint/índice (idempotente via IF NOT EXISTS)."""
    try:
        async with driver.session() as session:
            await session.run(cypher)
    except Exception as e:
        logger.warning("Erro ao criar constraint/índice: %s | Cypher: %s", e, cypher)

