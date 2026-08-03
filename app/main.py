from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, Response
from fastapi.middleware.cors import CORSMiddleware

from app.config import get_settings
from app.core.embedder import init_embedder
from app.db.neo4j import apply_domain_indexes, apply_branch_indexes, apply_vector_index, close_driver, init_driver
from app.routes import code, consolidate, health, ingest, nodes, query, semantic


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    await init_driver(settings.neo4j_uri, settings.neo4j_user, settings.neo4j_password)
    await apply_domain_indexes()
    await apply_branch_indexes()

    # ── Inicializar embedder (Vector RAG) ──────────────────────────────────────
    # Se provider="none" (default), o embedder fica desabilitado e o sistema
    # funciona em modo FTS-only. Nenhuma dep extra é necessária.
    if settings.cortex_embedding_provider != "none":
        init_embedder(
            provider=settings.cortex_embedding_provider,
            api_key=settings.openai_api_key,
            model_name=settings.cortex_embedding_model,
        )
        # Criar índice vetorial no Neo4j com as dimensões corretas para o provider
        from app.core.embedder import get_embedding_dimensions
        dims = get_embedding_dimensions(settings.cortex_embedding_provider)
        await apply_vector_index(dimensions=dims)

    yield
    await close_driver()


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title="Cortex — Product Knowledge Graph",
        description="GraphRAG service para contextualização de produto — agnóstico ao domínio",
        version="0.2.0",
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
    app.include_router(semantic.router, prefix="/api/v1")
    app.include_router(nodes.router, prefix="/api/v1")
    app.include_router(ingest.router, prefix="/api/v1")
    app.include_router(consolidate.router, prefix="/api/v1")
    app.include_router(code.router, prefix="/api/v1")

    # ── Prometheus /metrics ────────────────────────────────────────────────────
    try:
        from prometheus_client import (
            CONTENT_TYPE_LATEST,
            CollectorRegistry,
            generate_latest,
            multiprocess,
        )

        @app.get("/metrics", include_in_schema=False)
        async def metrics():
            """Prometheus metrics endpoint. Sem autenticação (scrape interno apenas)."""
            import os
            if os.environ.get("PROMETHEUS_MULTIPROC_DIR"):
                registry = CollectorRegistry()
                multiprocess.MultiProcessCollector(registry)
                data = generate_latest(registry)
            else:
                data = generate_latest()
            return Response(content=data, media_type=CONTENT_TYPE_LATEST)

    except ImportError:
        pass

    return app


app = create_app()
