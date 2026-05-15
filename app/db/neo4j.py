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
        "`vector.similarity_function`: 'cosine'}}}}"
    )
    await apply_index(cypher)
    logger.info("Vector index 'document_chunks' configurado (%d dims)", dimensions)


async def vector_search(
    query_embedding: list[float],
    top_k: int = 10,
    pillar_filter: str | None = None,
) -> list[dict]:
    """
    Busca por similaridade vetorial nos nós DocumentChunk.

    Args:
        query_embedding: Vetor da query (mesmo número de dimensões que o índice).
        top_k:           Número máximo de resultados.
        pillar_filter:   Filtra por pilar I.S.I.R (ex: "Intent"). Optional.

    Returns:
        Lista de dicts com {chunk_id, parent_id, content, score, pillar}.
    """
    driver = get_driver()

    where_clause = f"WHERE n.pillar = '{pillar_filter}'" if pillar_filter else ""
    cypher = f"""
        CALL db.index.vector.queryNodes('document_chunks', $top_k, $embedding)
        YIELD node AS n, score
        {where_clause}
        RETURN n.id AS chunk_id,
               n.parent_id AS parent_id,
               n.content AS content,
               n.pillar AS pillar,
               score
        ORDER BY score DESC
    """

    async with driver.session() as session:
        result = await session.run(cypher, embedding=query_embedding, top_k=top_k)
        return await result.data()
