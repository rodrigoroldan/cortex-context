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
from collections import defaultdict
from datetime import datetime, timezone

from neo4j import AsyncDriver

from app.core.parsers.base import EdgeData, NodeData

logger = logging.getLogger(__name__)

# Tamanho de lote por transação UNWIND em ingest_nodes/ingest_edges (issue #17).
DEFAULT_BATCH_SIZE = 500


def _now_utc() -> datetime:
    """Retorna datetime UTC atual (timezone-aware)."""
    return datetime.now(tz=timezone.utc)


def _chunked(items: list, size: int) -> list[list]:
    """Divide `items` em fatias contíguas de até `size` elementos."""
    return [items[i : i + size] for i in range(0, len(items), size)]


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
) -> bool:
    """
    Cria ou atualiza uma aresta entre dois nós restrita ao domain_id com metadata de branch.

    O MATCH tolera nós com domain_id ausente (NULL): nós ingeridos antes da propriedade
    domain_id existir (ou por qualquer outro parser que não a tenha setado) nunca casam
    contra domain_id = 'default' por igualdade estrita, e MERGE então simplesmente não
    executa — sem lançar exceção. Sem essa tolerância a aresta nunca é criada, mas a
    chamada "parece" bem-sucedida (ver issue #16).

    Retorna True se a aresta foi de fato persistida (ambos os nós foram encontrados),
    False caso contrário — o chamador deve checar o retorno em vez de assumir sucesso.
    """
    effective_domain_id = domain_id if domain_id and domain_id != "default" else (edge.properties.get("domain_id") or "default")
    edge.properties["domain_id"] = effective_domain_id
    branch = edge.properties.get("branch", "main")
    is_draft = bool(edge.properties.get("is_draft", False))

    async with driver.session() as session:
        result = await session.run(
            f"""
            MATCH (a {{id: $from_id}})
            WHERE a.domain_id = $domain_id OR ($domain_id = 'default' AND a.domain_id IS NULL)
            MATCH (b {{id: $to_id}})
            WHERE b.domain_id = $domain_id OR ($domain_id = 'default' AND b.domain_id IS NULL)
            MERGE (a)-[r:{edge.relationship}]->(b)
            SET r += $props
            SET r.domain_id = $domain_id
            SET r.branch = $branch
            SET r.is_draft = $is_draft
            RETURN count(r) AS edge_count
            """,
            from_id=edge.from_id,
            to_id=edge.to_id,
            domain_id=effective_domain_id,
            branch=branch,
            is_draft=is_draft,
            props=edge.properties,
        )
        record = await result.single()

    created = bool(record and record["edge_count"] > 0)
    if not created:
        logger.warning(
            "Aresta %s-[%s]->%s não persistida: nó de origem e/ou destino não encontrado (domain_id=%s)",
            edge.from_id,
            edge.relationship,
            edge.to_id,
            effective_domain_id,
        )
    return created


async def ingest_nodes(
    driver: AsyncDriver,
    nodes: list[NodeData],
    *,
    commit_sha: str | None = None,
    domain_id: str = "default",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """
    Upserta uma lista de NodeData em lotes (UNWIND + MERGE), agrupados por combinação de
    labels — a label primária + labels extras formam a cláusula `SET n:...`, que não pode
    ser parametrizada, então cada combinação distinta vira seu próprio grupo/transação.

    Reduz de N sessões Neo4j (uma por nó) para ceil(N/batch_size) transações por grupo de
    labels. Sem isso, payloads de milhares de nós (ex: POST /code/ingest de um AST real)
    abrem milhares de sessões sequenciais dentro de uma única requisição HTTP síncrona,
    retendo todos os objetos do payload em memória durante o loop inteiro — o suficiente
    para esgotar a RAM em containers pequenos (ver issue #17).

    Retorna o número de nós processados com sucesso.
    """
    if not nodes:
        return 0

    now = _now_utc()
    groups: dict[tuple[str, tuple[str, ...]], list[dict]] = defaultdict(list)

    for node in nodes:
        primary = node.primary_label
        extra_labels = tuple(lbl for lbl in node.node_labels if lbl != primary)
        effective_domain_id = domain_id if domain_id and domain_id != "default" else (node.properties.get("domain_id") or "default")
        node.properties["domain_id"] = effective_domain_id
        status = node.properties.get("status", "draft" if node.is_draft else "canonical")

        groups[(primary, extra_labels)].append(
            {
                "id": node.node_id,
                "domain_id": effective_domain_id,
                "canonical_id": node.canonical_id,
                "branch": node.branch,
                "status": status,
                "is_draft": node.is_draft,
                "props": node.properties,
                "ingested_at": now,
                "valid_from": now,
                "commit_sha": commit_sha,
            }
        )

    count = 0
    async with driver.session() as session:
        for (primary, extra_labels), rows in groups.items():
            set_labels_clause = f"SET n:{':'.join(extra_labels)}" if extra_labels else ""
            cypher = f"""
                UNWIND $rows AS row
                MERGE (n:{primary} {{id: row.id, domain_id: row.domain_id}})
                {set_labels_clause}
                SET n += row.props
                SET n.domain_id = row.domain_id
                SET n.canonical_id = row.canonical_id
                SET n.branch = row.branch
                SET n.status = row.status
                SET n.is_draft = row.is_draft
                SET n.ingested_at = row.ingested_at
                SET n.commit_sha = CASE WHEN row.commit_sha IS NOT NULL THEN row.commit_sha ELSE n.commit_sha END
                SET n.valid_from = COALESCE(n.valid_from, row.valid_from)
            """
            for chunk in _chunked(rows, batch_size):
                try:
                    await session.run(cypher, rows=chunk)
                    count += len(chunk)
                except Exception as e:
                    logger.error(
                        "Erro ao upsert batch de %d nós (%s%s): %s",
                        len(chunk),
                        primary,
                        "".join(f":{lbl}" for lbl in extra_labels),
                        e,
                    )
    return count


async def ingest_edges(
    driver: AsyncDriver,
    edges: list[EdgeData],
    *,
    domain_id: str = "default",
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """
    Upserta uma lista de EdgeData em lotes (UNWIND), agrupados por tipo de relacionamento
    (o tipo de aresta é parte da sintaxe do padrão Cypher, não dá pra parametrizar).

    Usa OPTIONAL MATCH + FOREACH condicional em vez de MATCH direto para que nós ausentes
    não descartem a linha do UNWIND (o que impediria saber quais arestas falharam) — cada
    linha de entrada sempre retorna, indicando se casou ou não. Nós com domain_id NULL
    (legado, sem essa propriedade) são tratados como equivalentes a domain_id='default',
    mesmo padrão já usado em app/routes/code.py (get_call_hierarchy, get_blast_radius).

    Retorna o número de arestas efetivamente persistidas — arestas cujo nó de origem
    e/ou destino não foi encontrado são contadas como não-persistidas e logadas como
    warning, nunca mascaradas como sucesso (ver issue #16), sem interromper o restante
    do lote.
    """
    if not edges:
        return 0

    groups: dict[str, list[EdgeData]] = defaultdict(list)
    for edge in edges:
        groups[edge.relationship].append(edge)

    total_created = 0
    async with driver.session() as session:
        for relationship, group_edges in groups.items():
            for chunk in _chunked(group_edges, batch_size):
                rows = []
                for edge in chunk:
                    effective_domain_id = domain_id if domain_id and domain_id != "default" else (edge.properties.get("domain_id") or "default")
                    edge.properties["domain_id"] = effective_domain_id
                    rows.append(
                        {
                            "from_id": edge.from_id,
                            "to_id": edge.to_id,
                            "domain_id": effective_domain_id,
                            "branch": edge.properties.get("branch", "main"),
                            "is_draft": bool(edge.properties.get("is_draft", False)),
                            "props": edge.properties,
                        }
                    )

                try:
                    result = await session.run(
                        f"""
                        UNWIND $rows AS row
                        OPTIONAL MATCH (a {{id: row.from_id}})
                        WHERE a.domain_id = row.domain_id OR (row.domain_id = 'default' AND a.domain_id IS NULL)
                        OPTIONAL MATCH (b {{id: row.to_id}})
                        WHERE b.domain_id = row.domain_id OR (row.domain_id = 'default' AND b.domain_id IS NULL)
                        FOREACH (_ IN CASE WHEN a IS NOT NULL AND b IS NOT NULL THEN [1] ELSE [] END |
                            MERGE (a)-[r:{relationship}]->(b)
                            SET r += row.props
                            SET r.domain_id = row.domain_id
                            SET r.branch = row.branch
                            SET r.is_draft = row.is_draft
                        )
                        RETURN row.from_id AS from_id, row.to_id AS to_id, (a IS NOT NULL AND b IS NOT NULL) AS matched
                        """,
                        rows=rows,
                    )
                    records = await result.data()
                except Exception as e:
                    logger.error("Erro ao upsert batch de %d arestas [%s]: %s", len(chunk), relationship, e)
                    continue

                for record in records:
                    if record.get("matched"):
                        total_created += 1
                    else:
                        logger.warning(
                            "Aresta %s-[%s]->%s não persistida: nó de origem e/ou destino não encontrado",
                            record.get("from_id"),
                            relationship,
                            record.get("to_id"),
                        )

    return total_created


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
