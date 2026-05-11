# Cortex — Product Knowledge Graph

> GraphRAG para contextualização de produto do Rolê Organizado usando Neo4j + FastAPI

## Overview

O Cortex é um serviço de Knowledge Graph que ingere specs e documentação do Rolê Organizado em um grafo Neo4j, expondo uma API FastAPI para consultas semânticas e contextuais.

Usado pelo MCP Server local para fornecer contexto rico ao GitHub Copilot sobre features implementadas, arquitetura e decisões de produto.

## Stack

- **Graph DB**: Neo4j 5.x (APOC)
- **API**: FastAPI + Uvicorn
- **Ingestão**: Scripts Python via CLI ou GitHub Action

## Endpoints

| Endpoint | Descrição |
|----------|-----------|
| `GET /health` | Health check |
| `GET /api/v1/features` | Lista features ingeridas |
| `POST /api/v1/ingest` | Ingere specs do diretório configurado |
| `POST /api/v1/query` | Consulta contextual no grafo |
| `GET /api/v1/spec/{spec_id}` | Detalhe de uma spec específica |

## Desenvolvimento

```bash
# Instalar dependências
pip install -e ".[dev]"

# Rodar testes
pytest

# Rodar localmente
uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload
```

## Deploy

Deploy automático via Terraform no LXC 200 (10.11.12.200) usando Docker Compose.

```bash
docker compose --env-file .env up -d
```

## Configuração

| Variável | Descrição | Default |
|----------|-----------|---------|
| `NEO4J_URI` | URI do Neo4j | `bolt://localhost:7687` |
| `NEO4J_USER` | Usuário Neo4j | `neo4j` |
| `NEO4J_PASSWORD` | Senha Neo4j | `cortex-secret` |
| `CORTEX_API_TOKEN` | Token de autenticação da API | `dev-token` |
| `SPECS_DIR` | Diretório com specs para ingestão | `./specs-sample` |
