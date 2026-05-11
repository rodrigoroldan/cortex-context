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

# ── Stage 2: Runtime ──────────────────────────────────────────────────────────
FROM python:3.11-slim

WORKDIR /app

# Copia dependências instaladas
COPY --from=builder /install /usr/local

# Copia código
COPY app/ app/
COPY run.py .

# Healthcheck
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8082/health')" || exit 1

ENV PYTHONUNBUFFERED=1 \
    APP_ENV=production \
    LOG_LEVEL=INFO

EXPOSE 8082

CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8082", "--workers", "2"]
