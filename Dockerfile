# ── Stage 1: Build ────────────────────────────────────────────────────────────
FROM python:3.11-slim AS builder

WORKDIR /build

# Instala hatchling para build
RUN pip install --no-cache-dir hatchling

COPY pyproject.toml .
COPY README.md .
COPY app/ app/

# Instala dependências em /install
RUN pip install --no-cache-dir --prefix=/install .

# ── Stage 2: Runtime (base — FTS only, ~250 MB) ───────────────────────────────
FROM python:3.11-slim AS runtime

WORKDIR /app

# Copia dependências instaladas
COPY --from=builder /install /usr/local

# Copia código
COPY app/ app/
COPY run.py .
COPY cortex.config.yaml .

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8082/health')" || exit 1

ENV PYTHONUNBUFFERED=1 \
  APP_ENV=production \
  LOG_LEVEL=INFO

EXPOSE 8082

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "2"]

# ── Stage 3: Embeddings (sentence-transformers + all-MiniLM-L6-v2, ~1.5 GB) ──
FROM runtime AS embeddings

# Instala sentence-transformers (puxa PyTorch CPU-only como dep transitiva)
RUN pip install --no-cache-dir "sentence-transformers>=2.7.0"

# Pré-baixa o modelo all-MiniLM-L6-v2 para dentro da imagem
# (evita download em runtime — funciona offline após o pull)
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('all-MiniLM-L6-v2'); print('Model cached successfully')"

ENV CORTEX_EMBEDDING_PROVIDER=local \
  CORTEX_EMBEDDING_MODEL=all-MiniLM-L6-v2
