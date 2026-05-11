from __future__ import annotations

from neo4j import AsyncDriver, AsyncGraphDatabase

_driver: AsyncDriver | None = None


async def init_driver(uri: str, user: str, password: str) -> None:
    global _driver
    _driver = AsyncGraphDatabase.driver(uri, auth=(user, password))
    await _driver.verify_connectivity()
    await _ensure_constraints()


async def close_driver() -> None:
    global _driver
    if _driver:
        await _driver.close()
        _driver = None


def get_driver() -> AsyncDriver:
    if _driver is None:
        raise RuntimeError("Neo4j driver not initialized")
    return _driver


async def _ensure_constraints() -> None:
    """Cria constraints e índices de texto completo no Neo4j."""
    driver = get_driver()
    async with driver.session() as session:
        # Unique constraint no id do nó Spec
        await session.run(
            "CREATE CONSTRAINT spec_id IF NOT EXISTS "
            "FOR (s:Spec) REQUIRE s.id IS UNIQUE"
        )
        # Índice de texto completo para busca por keywords (Spec)
        await session.run(
            "CREATE FULLTEXT INDEX spec_fts IF NOT EXISTS "
            "FOR (s:Spec) ON EACH [s.title, s.summary, s.labels_str]"
        )
        # Unique constraint no id do nó Service
        await session.run(
            "CREATE CONSTRAINT service_id IF NOT EXISTS "
            "FOR (s:Service) REQUIRE s.id IS UNIQUE"
        )
        # Índice de texto completo para Service (name + capabilities)
        await session.run(
            "CREATE FULLTEXT INDEX service_fts IF NOT EXISTS "
            "FOR (s:Service) ON EACH [s.name, s.capabilities_str]"
        )
