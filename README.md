# Cortex — Product Knowledge Graph

> GraphRAG + Vector RAG para contextualização de produto usando Neo4j + FastAPI  
> **v0.2.0 — Cortex Context v3** | Ontologia I.S.I.R | Plugin Pipeline Genérico

## Overview

O Cortex é um serviço de Knowledge Graph **produto-agnóstico** que ingere specs, documentação e manifestos de serviço em um grafo Neo4j, expondo uma API FastAPI para consultas FTS e semânticas (Vector RAG).

Usado pelo MCP Server local para fornecer contexto rico ao GitHub Copilot sobre features implementadas, arquitetura e decisões de produto.

### Ontologia I.S.I.R

A v3 introduz 4 pilares universais que estruturam todos os nós do grafo:

| Pilar | Responde a | Exemplos |
|-------|-----------|---------|
| `Intent` | O "Por quê" | Specs, Requisitos, Épicos, User Stories |
| `System` | O "Como" (alto nível) | Serviços, ADRs, APIs, Bancos de dados |
| `Implementation` | O "O quê" (concreto) | Workflows, Módulos, Componentes |
| `Runtime` | O "Mundo real" | Alertas, Deployments, Incidentes |

Cada nó recebe **multi-labels** no Neo4j:
```
(:Spec:Intent)          ← spec-070 "Pagamentos"
(:Service:System)       ← service-backend
(:Workflow:Implementation) ← workflow-payment-expiration
(:ADR:System)           ← adr-0001-use-temporal
(:DocumentChunk:Intent) ← chunk para Vector RAG
```

---

## Stack

- **Graph DB**: Neo4j 5.x (APOC)
- **API**: FastAPI + Uvicorn
- **Ingestão**: Plugin pipeline genérico (builtin + customizável)
- **Vector RAG**: Embeddings opcionais (none / local / openai)
- **Versão**: `0.2.0`

---

## API Endpoints

### Ingestão

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/v1/ingest/{dim_key}` | Ingesta uma dimensão específica (ex: `spec`, `service`, `adr`) |
| `POST` | `/api/v1/ingest` | Ingesta todas as dimensões ativas em `cortex.config.yaml` |

### Consulta FTS (Full-Text Search)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/query` | Busca FTS cross-dimension com expansão 1-hop no grafo |

**Parâmetros de query**: `keywords` (obrigatório), `limit` (default 8), `hops` (default 1), `pillar` (filtro: `Intent\|System\|Implementation\|Runtime`), `dimension` (filtro: `spec\|service\|...`)

### Consulta Semântica (Vector RAG)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `POST` | `/api/v1/query/semantic` | Hybrid GraphRAG: embedding → ANN → expansão no grafo |

**Requer** `CORTEX_EMBEDDING_PROVIDER != none`. Retorna `503` quando embedder desabilitado.

**Body**:
```json
{
  "query": "como funciona o rateio de eventos?",
  "top_k": 8,
  "hops": 1,
  "pillar": "Intent"
}
```

### Nós (Dimension-Agnostic)

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/api/v1/nodes` | Lista todas as dimensões ativas com contagem de nós |
| `GET` | `/api/v1/nodes/{dim_key}` | Lista nós de uma dimensão específica |
| `GET` | `/api/v1/nodes/{dim_key}/{node_id}` | Detalhe de um nó + vizinhos 1-hop |

### Health

| Método | Endpoint | Descrição |
|--------|----------|-----------|
| `GET` | `/health` | Health check (Neo4j + embedder status) |

---

## Dimensões Ativas

| Dimensão | Pilar | Parser | Fonte |
|----------|-------|--------|-------|
| `spec` | Intent | `builtin.markdown_frontmatter` | `specs/**/plan.md` (filesystem) |
| `service` | System | `builtin.agents_manifest` | `AGENTS.md` / `agents.md` (github_api ou filesystem) |
| `workflow` | Implementation | `builtin.markdown_frontmatter` | `**/temporal/**/*.yaml`, `**/workflows/**/*.md` |
| `adr` | System | `builtin.markdown_frontmatter` | `**/docs/arquitetura/**/*.md`, `**/docs/adr/**/*.md` |

Configure as dimensões ativas em `cortex.config.yaml`:
```yaml
active_dimensions:
  - spec
  - service
  - workflow
  - adr
```

---

## Plugin System

A v3 introduz um pipeline de parser genérico. Para adicionar um extractor customizado:

1. Crie `plugins/meu_parser.py` herdando de `BaseCortexExtractor`:
   ```python
   from app.core.parsers.base import BaseCortexExtractor, NodeData, ParseResult

   class MeuParser(BaseCortexExtractor):
       extractor_key = "meu.parser"

       def can_parse(self, file_path):
           return file_path.suffix == ".json"

       def parse(self, file_path, dimension_config):
           ...
   ```

2. Referencie no dimension YAML: `parser: meu.parser`

3. O Cortex descobre automaticamente via `discover_plugins()` — sem necessidade de registro manual.

Consulte [docs/DIMENSION-SCHEMA.md](docs/DIMENSION-SCHEMA.md) para o schema completo dos dimension YAMLs.

---

## Vector RAG (Embeddings)

Por padrão o embedder está **desabilitado** (`none`). O sistema funciona em modo FTS-only sem nenhuma dependência extra.

Para ativar:

```bash
# Modo local (offline, 384 dims — sentence-transformers)
pip install "cortex-role-organizado[embeddings-local]"
# No .env:  CORTEX_EMBEDDING_PROVIDER=local

# Modo OpenAI (1536 dims)
pip install "cortex-role-organizado[embeddings-openai]"
# No .env:  CORTEX_EMBEDDING_PROVIDER=openai
#           OPENAI_API_KEY=sk-...
```

Quando habilitado, a ingestão fragmenta os documentos em `DocumentChunk` nodes (via `chunker.py`) e cria um índice vetorial no Neo4j. O endpoint `/api/v1/query/semantic` fica disponível.

---

## Desenvolvimento

```bash
# Instalar dependências base
pip install -e ".[dev]"

# Com embeddings locais (opcional)
pip install -e ".[dev,embeddings-local]"

# Rodar testes
pytest

# Rodar localmente
uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload
```

---

## Deploy

Deploy automático via Terraform no LXC 200 (10.11.12.200) usando Docker Compose.

```bash
docker compose --env-file .env up -d
```

---

## Configuração

### Variáveis obrigatórias

| Variável | Descrição | Default |
|----------|-----------|---------|
| `NEO4J_URI` | URI do Neo4j | `bolt://localhost:7687` |
| `NEO4J_USER` | Usuário Neo4j | `neo4j` |
| `NEO4J_PASSWORD` | Senha Neo4j | `cortex-secret` |
| `CORTEX_API_TOKEN` | Token de autenticação da API | `dev-token` |

### Variáveis de filesystem

| Variável | Descrição | Default |
|----------|-----------|---------|
| `WORKSPACE_ROOT` | Raiz montada no container (volume bind) | `/workspace` |
| `SPECS_DIR` | Diretório das specs | `/specs` |
| `REPOS_MOUNT_PATH` | Repos montados localmente (fallback) | `/repos` |
| `GITHUB_TOKEN` | Token para source_type: github_api (repos privados) | — |

### Variáveis de Vector RAG (opcionais)

| Variável | Descrição | Default |
|----------|-----------|---------|
| `CORTEX_EMBEDDING_PROVIDER` | `none` \| `local` \| `openai` | `none` |
| `CORTEX_EMBEDDING_MODEL` | Modelo para provider `local` | `all-MiniLM-L6-v2` |
| `OPENAI_API_KEY` | Chave OpenAI (apenas provider `openai`) | — |
