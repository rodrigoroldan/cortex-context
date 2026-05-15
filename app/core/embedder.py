"""
core/embedder.py — Geração de embeddings para Vector RAG no Cortex.

Suporta 3 providers configuráveis via settings (CORTEX_EMBEDDING_PROVIDER):
  - "none"    → desabilitado (default). FTS-only mode, sistema funciona sem deps extras.
  - "openai"  → OpenAI text-embedding-3-small (requer OPENAI_API_KEY).
                Instalar: pip install openai
  - "local"   → sentence-transformers all-MiniLM-L6-v2 (sem GPU, 384 dims, offline).
                Instalar: pip install sentence-transformers

Dimensões dos vetores:
  - openai:   1536 (text-embedding-3-small) ou 3072 (text-embedding-3-large)
  - local:    384  (all-MiniLM-L6-v2)
  - none:     N/A (nunca chamado)

O embedder é um singleton lazy — inicializado apenas quando necessário.
Se o provider estiver configurado mas a dep não estiver instalada, lança EmbedderError
em tempo de inicialização (não em runtime), para fail-fast claro.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Dimensões padrão por provider
PROVIDER_DIMS: dict[str, int] = {
    "openai": 1536,
    "local": 384,
}


class EmbedderError(Exception):
    """Erro de inicialização ou uso do embedder."""


class _OpenAIEmbedder:
    """Wrapper para OpenAI Embeddings API."""

    def __init__(self, api_key: str, model: str = "text-embedding-3-small") -> None:
        try:
            from openai import AsyncOpenAI  # type: ignore[import-untyped]
        except ImportError as exc:
            raise EmbedderError(
                "OpenAI provider requer 'openai'. Instale: pip install openai"
            ) from exc
        self._client = AsyncOpenAI(api_key=api_key)
        self._model = model

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        response = await self._client.embeddings.create(
            input=texts,
            model=self._model,
        )
        return [item.embedding for item in response.data]


class _LocalEmbedder:
    """Wrapper para sentence-transformers (completamente offline)."""

    def __init__(self, model_name: str = "all-MiniLM-L6-v2") -> None:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore[import-untyped]
        except ImportError as exc:
            raise EmbedderError(
                "Local provider requer 'sentence-transformers'. "
                "Instale: pip install sentence-transformers"
            ) from exc
        logger.info("Carregando modelo local de embeddings: %s", model_name)
        self._model = SentenceTransformer(model_name)

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        import asyncio

        loop = asyncio.get_event_loop()
        # SentenceTransformer é síncrono — rodar em thread pool para não bloquear
        vectors = await loop.run_in_executor(
            None,
            lambda: self._model.encode(texts, convert_to_numpy=True).tolist(),
        )
        return vectors  # type: ignore[return-value]


# ── Singleton ──────────────────────────────────────────────────────────────────

_embedder_instance: "_OpenAIEmbedder | _LocalEmbedder | None" = None
_embedder_provider: str = "none"


def init_embedder(provider: str, **kwargs: object) -> None:
    """
    Inicializa o singleton do embedder.

    Chamado no startup da aplicação se CORTEX_EMBEDDING_PROVIDER != "none".

    Args:
        provider: "openai" | "local" | "none"
        **kwargs: api_key (openai), model_name (local)
    """
    global _embedder_instance, _embedder_provider
    _embedder_provider = provider

    if provider == "openai":
        api_key = str(kwargs.get("api_key", ""))
        if not api_key:
            raise EmbedderError("OPENAI_API_KEY é obrigatório para provider 'openai'")
        _embedder_instance = _OpenAIEmbedder(api_key=api_key)
        logger.info("Embedder inicializado: OpenAI (text-embedding-3-small)")
    elif provider == "local":
        model_name = str(kwargs.get("model_name", "all-MiniLM-L6-v2"))
        _embedder_instance = _LocalEmbedder(model_name=model_name)
        logger.info("Embedder inicializado: Local (%s)", model_name)
    elif provider == "none":
        _embedder_instance = None
        logger.info("Embedder desabilitado — modo FTS-only")
    else:
        raise EmbedderError(f"Provider desconhecido: {provider!r}. Use 'openai', 'local' ou 'none'")


def get_embedding_dimensions(provider: str | None = None) -> int:
    """Retorna o número de dimensões do provider ativo (ou informado)."""
    p = provider or _embedder_provider
    return PROVIDER_DIMS.get(p, 384)


def is_embedder_enabled() -> bool:
    """Verdadeiro se um embedder foi inicializado e está ativo."""
    return _embedder_instance is not None


async def embed_texts(texts: list[str]) -> list[list[float]] | None:
    """
    Gera embeddings para uma lista de textos.

    Retorna None se o embedder não estiver habilitado (provider="none").
    Lança EmbedderError em caso de falha na chamada ao provider.
    """
    if _embedder_instance is None:
        return None
    if not texts:
        return []
    try:
        return await _embedder_instance.embed_batch(texts)
    except Exception as exc:
        raise EmbedderError(f"Falha ao gerar embeddings: {exc}") from exc
