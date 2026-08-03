"""
core/graph_builder.py — Persiste nós e arestas no Neo4j com suporte a multi-labels,
versionamento bitemporal e isolamento multi-tenant por domain_id.

Estratégia de multi-labels (I.S.I.R):
  - MERGE na label primária + id + domain_id (ex: MERGE (n:Spec {id: $id, domain_id: $domain_id}))
  - SET adiciona o pilar como label extra (ex: SET n:Intent)
  - Qualquer número de labels extras é suportado

Versionamento Bitemporal (v3.0):
  - ingested_at:  datetime da última ingestão bem-sucedida
  - valid_from:   datetime da primeira vez que o nó foi ingerido (nunca sobrescrito)
  - commit_sha:   SHA do commit que originou o nó (opcional; propagado pelo CLI agent)
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from neo4j import AsyncDriver

from app.core.parsers.base import EdgeData, NodeData

logger = logging.getLogger(__name__)


def _now_utc() -> datetime:
    """Retorna datetime UTC atual (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


async def upsert_node(
    driver: AsyncDriver,
    node: NodeData,
    *,
    commit_sha: str | None = None,
    domain_id: str = "default",
) -> None:
    """
    Cria ou atualiza um nó no Neo4j com suporte a multi-labels, versionamento bitemporal,
    Shadow Graph metadata (canonical_id, branch, status, is_draft) e isolamento por domain_id.

    MERGE usa primary_label + id + domain_id como chave de upsert.
    """
    primary = node.primary_label
    extra_labels = [lbl for lbl in node.node_labels if lbl != primary]

    set_labels_clause = f"SET n:{':'.join(extra_labels)}" if extra_labels else ""
    effective_domain_id = domain_id if domain_id and domain_id != "default" else (node.properties.get("domain_id") or "default")
    node.properties["domain_id"] = effective_domain_id

    canonical_id = node.canonical_id
    branch = node.branch
    is_draft = node.is_draft
    status = node.properties.get("status", "draft" if is_draft else "canonical")

    now = _now_utc()

    async with driver.session() as session:
        await session.run(
            f"""
            MERGE (n:{primary} {{id: $id, domain_id: $domain_id}})
            {set_labels_clause}
            SET n += $props
            SET n.domain_id = $domain_id
            SET n.canonical_id = $canonical_id
            SET n.branch = $branch
            SET n.status = $status
            SET n.is_draft = $is_draft
            SET n.ingested_at = $ingested_at
            SET n.commit_sha = CASE WHEN $commit_sha IS NOT NULL THEN $commit_sha ELSE n.commit_sha END
            SET n.valid_from = COALESCE(n.valid_from, $valid_from)
            """,
            id=node.node_id,
            domain_id=effective_domain_id,
            canonical_id=canonical_id,
            branch=branch,
            status=status,
            is_draft=is_draft,
            props=node.properties,
            ingested_at=now,
            valid_from=now,
            commit_sha=commit_sha,
        )


async def upsert_edge(
    driver: AsyncDriver,
    edge: EdgeData,
    *,
    domain_id: str = "default",
) -> None:
    """
    Cria ou atualiza uma aresta entre dois nós restrita ao domain_id com metadata de branch.
    """
    effective_domain_id = domain_id if domain_id and domain_id != "default" else (edge.properties.get("domain_id") or "default")
    edge.properties["domain_id"] = effective_domain_id
    branch = edge.properties.get("branch", "main")
    is_draft = bool(edge.properties.get("is_draft", False))

    async with driver.session() as session:
        await session.run(
            f"""
            MATCH (a {{id: $from_id, domain_id: $domain_id}})
            MATCH (b {{id: $to_id, domain_id: $domain_id}})
            MERGE (a)-[r:{edge.relationship}]->(b)
            SET r += $props
            SET r.domain_id = $domain_id
            SET r.branch = $branch
            SET r.is_draft = $is_draft
            """,
            from_id=edge.from_id,
            to_id=edge.to_id,
            domain_id=effective_domain_id,
            branch=branch,
            is_draft=is_draft,
            props=edge.properties,
        )


async def ingest_nodes(
    driver: AsyncDriver,
    nodes: list[NodeData],
    *,
    commit_sha: str | None = None,
    domain_id: str = "default",
) -> int:
    """Upserta uma lista de NodeData. Retorna o número de nós processados com sucesso."""
    count = 0
    for node in nodes:
        try:
            await upsert_node(driver, node, commit_sha=commit_sha, domain_id=domain_id)
            count += 1
        except Exception as e:
            logger.error(
                "Erro ao upsert nó %s (%s): %s",
                node.node_id,
                ":".join(node.node_labels),
                e,
            )
    return count


async def ingest_edges(
    driver: AsyncDriver,
    edges: list[EdgeData],
    *,
    domain_id: str = "default",
) -> int:
    """Upserta uma lista de EdgeData. Retorna o número de arestas processadas com sucesso."""
    count = 0
    for edge in edges:
        try:
            await upsert_edge(driver, edge, domain_id=domain_id)
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


async def upsert_chunk(
    driver: AsyncDriver,
    chunk: NodeData,
    *,
    domain_id: str = "default",
) -> None:
    """
    Persiste um nó DocumentChunk com suporte a embeddings, metadata de branch e domain_id.
    """
    effective_domain_id = domain_id if domain_id and domain_id != "default" else (chunk.properties.get("domain_id") or "default")
    chunk.properties["domain_id"] = effective_domain_id
    canonical_id = chunk.canonical_id
    branch = chunk.branch
    is_draft = chunk.is_draft
    status = chunk.properties.get("status", "draft" if is_draft else "canonical")

    props_without_embedding = {
        k: v for k, v in chunk.properties.items() if k != "embedding"
    }
    has_embedding = "embedding" in chunk.properties

    extra_labels = [lbl for lbl in chunk.node_labels if lbl != "DocumentChunk"]
    set_labels_clause = f"SET n:{':'.join(extra_labels)}" if extra_labels else ""

    now = _now_utc()

    if has_embedding:
        cypher = f"""
        MERGE (n:DocumentChunk {{id: $id, domain_id: $domain_id}})
        {set_labels_clause}
        SET n += $props
        SET n.domain_id = $domain_id
        SET n.canonical_id = $canonical_id
        SET n.branch = $branch
        SET n.status = $status
        SET n.is_draft = $is_draft
        SET n.embedding = $embedding
        SET n.ingested_at = $ingested_at
        SET n.valid_from = COALESCE(n.valid_from, $valid_from)
        """
        async with driver.session() as session:
            await session.run(
                cypher,
                id=chunk.node_id,
                domain_id=effective_domain_id,
                canonical_id=canonical_id,
                branch=branch,
                status=status,
                is_draft=is_draft,
                props=props_without_embedding,
                embedding=chunk.properties["embedding"],
                ingested_at=now,
                valid_from=now,
            )
    else:
        cypher = f"""
        MERGE (n:DocumentChunk {{id: $id, domain_id: $domain_id}})
        {set_labels_clause}
        SET n += $props
        SET n.domain_id = $domain_id
        SET n.canonical_id = $canonical_id
        SET n.branch = $branch
        SET n.status = $status
        SET n.is_draft = $is_draft
        SET n.ingested_at = $ingested_at
        SET n.valid_from = COALESCE(n.valid_from, $valid_from)
        """
        async with driver.session() as session:
            await session.run(
                cypher,
                id=chunk.node_id,
                domain_id=effective_domain_id,
                canonical_id=canonical_id,
                branch=branch,
                status=status,
                is_draft=is_draft,
                props=props_without_embedding,
                ingested_at=now,
                valid_from=now,
            )


async def ingest_chunks(
    driver: AsyncDriver,
    chunks: list[NodeData],
    *,
    domain_id: str = "default",
) -> int:
    """Upserta uma lista de DocumentChunk nodes. Retorna o número processado com sucesso."""
    count = 0
    for chunk in chunks:
        try:
            await upsert_chunk(driver, chunk, domain_id=domain_id)
            count += 1
        except Exception as e:
            logger.error(
                "Erro ao upsert chunk %s: %s",
                chunk.node_id,
                e,
            )
    return count
