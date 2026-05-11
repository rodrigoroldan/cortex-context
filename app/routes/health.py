"""
health.py — Endpoint de health check com verificação do Neo4j.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from app.db.neo4j import get_driver

router = APIRouter(tags=["health"])


class HealthResponse(BaseModel):
    status: str
    neo4j: str
    version: str = "0.1.0"


@router.get("/health", response_model=HealthResponse)
async def health() -> HealthResponse:
    neo4j_status = "ok"
    try:
        driver = get_driver()
        async with driver.session() as session:
            result = await session.run("RETURN 1 AS ping")
            await result.single()
    except Exception as e:
        neo4j_status = f"error: {e}"

    return HealthResponse(
        status="ok" if neo4j_status == "ok" else "degraded",
        neo4j=neo4j_status,
    )
