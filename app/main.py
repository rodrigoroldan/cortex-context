from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.db.neo4j import close_driver, init_driver
from app.routes import health, query, services, workflows


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_driver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    yield
    await close_driver()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Cortex — Product Knowledge Graph",
        description="GraphRAG service para contextualização de specs do Rolê Organizado",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if settings.app_env != "production" else None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
    )

    app.include_router(health.router)
    app.include_router(query.router, prefix="/api/v1")
    app.include_router(services.router, prefix="/api/v1")
    app.include_router(workflows.router, prefix="/api/v1")

    return app


app = create_app()
