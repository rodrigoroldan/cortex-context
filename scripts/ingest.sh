#!/usr/bin/env bash
# ingest.sh — Script de ingestão para uso manual e CI/CD
# Uso: ./scripts/ingest.sh /path/to/specs [--dry-run]
set -euo pipefail

SPECS_DIR="${1:-}"
DRY_RUN="${2:-}"

if [[ -z "$SPECS_DIR" ]]; then
  echo "❌ Usage: $0 <specs-dir> [--dry-run]" >&2
  exit 1
fi

if [[ ! -d "$SPECS_DIR" ]]; then
  echo "❌ Diretório não encontrado: $SPECS_DIR" >&2
  exit 1
fi

echo "🔍 Iniciando ingestão de specs em: $SPECS_DIR"

if [[ "$DRY_RUN" == "--dry-run" ]]; then
  echo "📋 Modo dry-run — sem gravação no Neo4j"
  python -m app.ingestor.cli ingest --specs-dir "$SPECS_DIR" --dry-run
else
  python -m app.ingestor.cli ingest --specs-dir "$SPECS_DIR"
fi
