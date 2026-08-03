from __future__ import annotations

import logging

from neo4j import AsyncDriver, AsyncGraphDatabase

logger = logging.getLogger(__name__)

_driver: AsyncDriver | None = None


async def init_driver(uri: str, user: str, password: str) -> None:
    global _driver
    _driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    await _driver.verify_connectivity()


async def close_driver() -> None:
    global _driver
    if _driver:
        await _driver.close()
        _driver = None


def get_driver() -> AsyncDriver:
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialized")
    return _driver


async def apply_index(cypher: str) -> None:
    """
    Executa um Cypher de criação de constraint/índice.
    Chamado durante a ingestão de cada dimension (via dimension YAML indexes[].cypher).
    """
    driver = get_driver()
    async with driver.session() as session:
        try:
            await session.run(cypher)
        except Exception as e:
            # Ignora erros de "already exists" — outros são logados
            if "already exists" not in str(e).lower():
                logger.warning("Erro ao aplicar index [%s]: %s", cypher[:80], e)


async def apply_domain_indexes() -> None:
    """
    Cria índices Cypher para (n.domain_id, n.id) em todas as labels de nós conhecidas.
    """
    labels = ["Spec", "Service", "ADR", "Workflow", "Concept", "History", "DocumentChunk", "CodeSymbol", "CodeFile"]
    for label in labels:
        cypher = (
            f"CREATE INDEX {label.lower()}_domain_id_idx IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.domain_id, n.id)"
        )
        await apply_index(cypher)
    logger.info("Índices Cypher de domínio (domain_id, id) configurados.")


async def apply_branch_indexes() -> None:
    """
    Cria índices Cypher para branch, status, is_draft e canonical_id.
    """
    labels = ["Spec", "Service", "ADR", "Workflow", "Concept", "History", "DocumentChunk", "CodeSymbol", "CodeFile"]
    for label in labels:
        cypher_branch = (
            f"CREATE INDEX {label.lower()}_branch_canonical_idx IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.domain_id, n.branch, n.canonical_id)"
        )
        cypher_draft = (
            f"CREATE INDEX {label.lower()}_is_draft_idx IF NOT EXISTS "
            f"FOR (n:{label}) ON (n.domain_id, n.is_draft)"
        )
        await apply_index(cypher_branch)
        await apply_index(cypher_draft)
    logger.info("Índices Cypher de branch e draft configurados.")


async def apply_vector_index(dimensions: int = 384) -> None:
    """
    Cria o índice vetorial para nós DocumentChunk no Neo4j 5.x.

    Idempotente: o índice só é criado se não existir.
    É chamado no startup quando CORTEX_EMBEDDING_PROVIDER != "none".

    Args:
        dimensions: Número de dimensões do vetor (384 local / 1536 openai).
    """
    cypher = (
        "CREATE VECTOR INDEX document_chunks IF NOT EXISTS "
        "FOR (n:DocumentChunk) ON n.embedding "
        f"OPTIONS {{indexConfig: {{`vector.dimensions`: {dimensions}, "
        "`vector.similarity_function`: 'cosine'}}"
    )
    await apply_index(cypher)
    logger.info("Vector index 'document_chunks' configurado (%d dims)", dimensions)


async def vector_search(
    query_embedding: list[float],
    top_k: int = 10,
    pillar_filter: str | None = None,
    domain_id: str = "default",
) -> list[dict]:
    """
    Busca por similaridade vetorial nos nós DocumentChunk filtrando por domain_id e pilar.

    Args:
        query_embedding: Vetor da query (mesmo número de dimensões que o índice).
        top_k:           Número máximo de resultados.
        pillar_filter:   Filtra por pilar I.S.I.R (ex: "Intent"). Optional.
        domain_id:       Identificador de domínio multi-tenant para isolamento.

    Returns:
        Lista de dicts com {chunk_id, parent_id, content, score, pillar, domain_id}.
    """
    driver = get_driver()

    cypher = """
        CALL db.index.vector.queryNodes('document_chunks', $top_k, $embedding)
        YIELD node AS n, score
        WHERE (n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL))
          AND ($pillar IS NULL OR n.pillar = $pillar)
        RETURN n.id AS chunk_id,
               n.parent_id AS parent_id,
               n.content AS content,
               n.pillar AS pillar,
               n.domain_id AS domain_id,
               score
        ORDER BY score DESC
    """

    async with driver.session() as session:
        result = await session.run(
            cypher,
            embedding=query_embedding,
            top_k=top_k,
            domain_id=domain_id,
            pillar=pillar_filter,
        )
        return await result.data()
