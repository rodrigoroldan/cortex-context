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
