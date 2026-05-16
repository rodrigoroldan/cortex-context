# Cortex Context

> **GraphRAG + Vector RAG for AI coding assistants** — turn your specs, docs and service manifests into a queryable knowledge graph that makes GitHub Copilot (and any MCP-compatible AI) context-aware about your product.

[![CI](https://github.com/rodrigoroldan/cortex-context/actions/workflows/ci.yml/badge.svg)](https://github.com/rodrigoroldan/cortex-context/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://hub.docker.com/r/rodrigoroldan/cortex-context)
[![Python](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![Neo4j](https://img.shields.io/badge/neo4j-5.x-008CC1)](https://neo4j.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![npm](https://img.shields.io/badge/cli-@cortex--context%2Fcli-red)](https://www.npmjs.com/package/@cortex-context/cli)

---

## What is Cortex Context?

Cortex Context is a **self-hosted knowledge graph server** that ingests your product documentation — specs, architecture decision records, service manifests, workflow definitions — and exposes a rich query API that AI coding assistants consume via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Instead of your AI assistant hallucinating about your domain, it queries the live graph:

```
"What specs affect the payments service?"
  → Graph traversal: (:Spec)-[:AFFECTS]->(:Service {id: "payments"})
  → Returns: spec-042, spec-070, spec-105 with full context
```

It works with any team following lightweight docs-as-code conventions. No proprietary format, no cloud lock-in.

---

## Quick Start

Get Cortex running locally in under 5 minutes.

**Prerequisites**: Docker, Node.js 18+ (for the CLI)

```bash
# 1. Install the CLI
npm install -g @cortex-context/cli

# 2. From your workspace root, run the guided setup
cortex-context init
```

`init` handles the full setup interactively:

- Starts Neo4j + Cortex API via Docker Compose
- Registers the MCP server in your editor's config
- Installs a git hook for automatic sync on commit

> **Manual setup?** See [Manual Installation](#manual-installation) below.

---

## How It Works

### I.S.I.R Ontology

Every node in the graph belongs to one of four universal pillars:

| Pillar           | Answers            | Examples                                 |
| ---------------- | ------------------ | ---------------------------------------- |
| `Intent`         | The **Why**        | Specs, Requirements, Epics, User Stories |
| `System`         | The **How** (high) | Services, ADRs, APIs, Databases          |
| `Implementation` | The **What**       | Workflows, Modules, Components           |
| `Runtime`        | The **Real world** | Alerts, Deployments, Incidents           |

Nodes receive **multi-labels** in Neo4j, enabling dimension-agnostic traversal:

```
(:Spec:Intent)               ← "spec-070: Payments"
(:Service:System)            ← "service-backend"
(:Workflow:Implementation)   ← "workflow-payment-expiration"
(:ADR:System)                ← "adr-0001-use-temporal"
(:DocumentChunk:Intent)      ← fragment for Vector RAG
```

### Graph Relationships

| Relationship    | Meaning                                            |
| --------------- | -------------------------------------------------- |
| `[:AFFECTS]`    | Spec impacts a service (from `repos:` frontmatter) |
| `[:IMPLEMENTS]` | Commit implements a spec (from branch convention)  |
| `[:RELATED_TO]` | Cross-dimension semantic link                      |
| `[:NEXT_HOP]`   | Graph expansion for context retrieval              |

---

## Installation

### Docker (recommended)

```bash
git clone https://github.com/rodrigoroldan/cortex-context.git
cd cortex-context

cp .env.example .env
# Edit .env — set NEO4J_PASSWORD, CORTEX_API_TOKEN, SPECS_DIR

docker compose up -d
```

The API is available at `http://localhost:8082`. Neo4j Browser at `http://localhost:7474`.

> **Note on networking**: The default `docker-compose.yml` uses `network_mode: host` for compatibility with unprivileged containers (e.g., Proxmox LXC). For standard Docker environments, see [Bridge Network Setup](#bridge-network-setup) below.

### Python (development)

```bash
git clone https://github.com/rodrigoroldan/cortex-context.git
cd cortex-context

python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

pip install -e ".[dev]"

cp .env.example .env
# Edit .env

# Start Neo4j separately (e.g., via Docker)
docker run -d --name neo4j -p 7474:7474 -p 7687:7687 \
  -e NEO4J_AUTH=neo4j/your-password \
  -e NEO4J_PLUGINS='["apoc"]' \
  neo4j:5.18-community

uvicorn app.main:app --host 0.0.0.0 --port 8082 --reload
```

### Bridge Network Setup

For standard Docker (non-LXC) environments, edit `docker-compose.yml`:

```yaml
# 1. Comment out all `network_mode: host` lines
# 2. Uncomment the `networks:` blocks
# 3. Change NEO4J_URI to bolt://neo4j:7687
# 4. Add ports: ["7474:7474", "8082:8082"] to each service
```

---

## Configuration

### Environment Variables

Copy `.env.example` to `.env` and set the required values:

| Variable           | Description                                           | Default                 |
| ------------------ | ----------------------------------------------------- | ----------------------- |
| `NEO4J_URI`        | Neo4j Bolt URI                                        | `bolt://localhost:7687` |
| `NEO4J_USER`       | Neo4j username                                        | `neo4j`                 |
| `NEO4J_PASSWORD`   | Neo4j password (**required**)                         | —                       |
| `CORTEX_API_TOKEN` | API bearer token (**required**)                       | —                       |
| `CORTEX_PORT`      | API port                                              | `8082`                  |
| `SPECS_DIR`        | Path to your specs directory (mounted into container) | `/specs`                |
| `REPOS_DIR`        | Path to repos with service manifests (optional)       | `/repos`                |
| `GITHUB_TOKEN`     | GitHub token for `source_type: github_api` (optional) | —                       |

**Vector RAG (optional):**

| Variable                    | Description                             | Default            |
| --------------------------- | --------------------------------------- | ------------------ |
| `CORTEX_EMBEDDING_PROVIDER` | `none` \| `local` \| `openai`           | `none`             |
| `CORTEX_EMBEDDING_MODEL`    | Model name for `local` provider         | `all-MiniLM-L6-v2` |
| `OPENAI_API_KEY`            | OpenAI key (only for `openai` provider) | —                  |

### cortex.config.yaml

The main configuration file. Controls which dimensions are active and how sources are discovered. Fully documented inline:

```yaml
cortex_version: "3.0.0"
dimensions_dir: "app/dimensions"
plugins_dir: "plugins"

active_dimensions:
  - spec # Intent:Spec  — product specifications
  - service # System:Service — microservices
  - workflow # Implementation:Workflow — async flows
  - adr # System:ADR — architecture decisions

filesystem:
  workspace_root: "/workspace"
  specs_dir: "/specs"

# For github_api source type, fill in your org:
github:
  owner: "your-org"
  default_branch: "main"
```

See [`cortex.config.yaml`](cortex.config.yaml) for the full annotated config.

---

## API Reference

### Ingestion

| Method | Endpoint                   | Description                                          |
| ------ | -------------------------- | ---------------------------------------------------- |
| `POST` | `/api/v1/ingest/{dim_key}` | Ingest a specific dimension (e.g. `spec`, `service`) |
| `POST` | `/api/v1/ingest`           | Ingest all active dimensions                         |

### Full-Text Search

| Method | Endpoint        | Description                                    |
| ------ | --------------- | ---------------------------------------------- |
| `GET`  | `/api/v1/query` | Cross-dimension FTS with 1-hop graph expansion |

**Query params**: `keywords` (required), `limit` (default `8`), `hops` (default `1`), `pillar` (`Intent|System|Implementation|Runtime`), `dimension` (`spec|service|...`)

### Semantic Search (Vector RAG)

| Method | Endpoint                 | Description                                        |
| ------ | ------------------------ | -------------------------------------------------- |
| `POST` | `/api/v1/query/semantic` | Hybrid GraphRAG: embedding → ANN → graph expansion |

Returns `503` when `CORTEX_EMBEDDING_PROVIDER=none`.

```json
{
  "query": "how does the payments reconciliation work?",
  "top_k": 8,
  "hops": 1,
  "pillar": "Intent"
}
```

### Nodes

| Method | Endpoint                            | Description                                |
| ------ | ----------------------------------- | ------------------------------------------ |
| `GET`  | `/api/v1/nodes`                     | List all active dimensions with node count |
| `GET`  | `/api/v1/nodes/{dim_key}`           | List nodes for a dimension                 |
| `GET`  | `/api/v1/nodes/{dim_key}/{node_id}` | Node detail + 1-hop neighbors              |

### Health

| Method | Endpoint  | Description                            |
| ------ | --------- | -------------------------------------- |
| `GET`  | `/health` | Health check (Neo4j + embedder status) |

---

## Built-in Dimensions

| Dimension  | Pillar         | Parser                         | Default source                                       |
| ---------- | -------------- | ------------------------------ | ---------------------------------------------------- |
| `spec`     | Intent         | `builtin.markdown_frontmatter` | `specs/**/plan.md` (filesystem)                      |
| `service`  | System         | `builtin.agents_manifest`      | `AGENTS.md` / `agents.md` (github_api or filesystem) |
| `workflow` | Implementation | `builtin.markdown_frontmatter` | `**/temporal/**/*.yaml`, `**/workflows/**/*.md`      |
| `adr`      | System         | `builtin.markdown_frontmatter` | `**/docs/adr/**/*.md`                                |

Dimensions are fully configurable via individual YAML files in `app/dimensions/`. See [docs/DIMENSION-SCHEMA.md](docs/DIMENSION-SCHEMA.md) for the full schema.

---

## Plugin System

Add custom extractors without touching core code.

1. Create `plugins/my_extractor.py` extending `BaseCortexExtractor`:

   ```python
   from app.core.parsers.base import BaseCortexExtractor, NodeData, ParseResult

   class MyExtractor(BaseCortexExtractor):
       extractor_key = "my.extractor"

       def can_parse(self, file_path):
           return file_path.suffix == ".json"

       def parse(self, file_path, dimension_config):
           # return ParseResult(nodes=[...], edges=[...])
           ...
   ```

2. Reference it in your dimension YAML: `parser: my.extractor`

3. Cortex auto-discovers it via `discover_plugins()` — no manual registration needed.

---

## Vector RAG (Embeddings)

The embedder is **disabled by default** (`none`). Cortex runs as a pure FTS graph with no extra dependencies.

To enable:

```bash
# Local (offline, 384 dims — no GPU required)
pip install "cortex-context[embeddings-local]"
# .env: CORTEX_EMBEDDING_PROVIDER=local

# OpenAI (1536 dims)
pip install "cortex-context[embeddings-openai]"
# .env: CORTEX_EMBEDDING_PROVIDER=openai
#       OPENAI_API_KEY=sk-...
```

When enabled, ingestion splits documents into `DocumentChunk` nodes and builds a vector index in Neo4j. The `/api/v1/query/semantic` endpoint becomes available.

---

## CLI — `@cortex-context/cli`

A standalone TypeScript CLI for managing Cortex from your terminal. [Published on npm.](https://www.npmjs.com/package/@cortex-context/cli)

```bash
npm install -g @cortex-context/cli
# or: npx @cortex-context/cli@latest <command>
```

| Command                         | Description                                                                |
| ------------------------------- | -------------------------------------------------------------------------- |
| `cortex-context init`           | 3-phase setup: Docker + MCP server + git hook. Run once per workspace.     |
| `cortex-context sync`           | Reads `git diff HEAD~1`, detects `spec_ref` from branch, ingests the diff. |
| `cortex-context sync --dry-run` | Preview diff and detected `spec_ref` without sending to server.            |
| `cortex-context update`         | Update Skills and MCP Server to the latest bundle version.                 |
| `cortex-context doctor`         | Verify connectivity with Cortex and validate local installation.           |

### Automatic Branch-to-Spec Linking

`sync` extracts `spec_ref` from your branch name convention automatically:

```
branch: feature/151-payments-refactor
  → spec_ref: "spec-151"
  → Cortex: (:Commit)-[:IMPLEMENTS]->(:Spec {id: "spec-151"})
```

Zero extra discipline: just follow the `feature/<NNN>-slug` convention. The CLI infers the context for free.

### Cross-Repo Spec Tracking

A `repos:` field in `plan.md` frontmatter creates `(:Spec)-[:AFFECTS]->(:Service)` edges, connecting specs to the services they impact:

```yaml
# specs/070-payments/plan.md
---
id: spec-070
title: Payments
repos: [backend, bff, mobile]
---
```

This powers impact analysis queries: "which specs affect the backend service?"

---

## MCP Server

Cortex ships with an [MCP (Model Context Protocol)](https://modelcontextprotocol.io/) server that exposes the graph to AI coding assistants like GitHub Copilot.

After running `cortex-context init`, the MCP server is automatically registered in your editor. Available tools:

| Tool                    | Description                                     |
| ----------------------- | ----------------------------------------------- |
| `query_product_context` | Semantic keyword search across all dimensions   |
| `get_spec_context`      | Spec detail + 1-hop neighbors (impact analysis) |
| `get_service_context`   | Service stack + specs that affect it            |
| `list_features`         | List features by status                         |
| `list_dimensions`       | List active dimensions with node counts         |
| `get_node_context`      | Any node + 1-hop neighbors                      |

---

## Development

```bash
pip install -e ".[dev]"

# With local embeddings (optional)
pip install -e ".[dev,embeddings-local]"

pytest tests/

# Lint
ruff check .
ruff format .
```

---

## Roadmap

### ✅ Shipped in v0.2.0

- **`@cortex-context/cli` v0.2.0** — TypeScript CLI with `init`, `sync`, `update`, `doctor`
- **Diff-aware ingest** — `sync` uses `git diff HEAD~1` for incremental ingestion (delta only)
- **Branch-to-spec linking** — `spec_ref` auto-extracted from `feature/<NNN>-slug` convention
- **Cross-repo tracking** — `repos:[]` frontmatter creates `(:Spec)-[:AFFECTS]->(:Service)` edges
- **Expanded MCP tools** — `list_dimensions` and `get_node_context` available in MCP server

### Coming next

- **`github-issue` dimension** — ingest GitHub issues and PRs as `Intent` nodes, linked to specs by label/title
- **`confluence` dimension** — parser for Confluence pages via API, mapped to `System` and `Intent`
- **Embedded graph UI** — visual graph explorer served directly by FastAPI
- **`get_impact_analysis` MCP tool** — explicit multi-hop traversal for spec/ADR impact analysis

---

## Contributing

Contributions are welcome! Please read [CONTRIBUTING.md](CONTRIBUTING.md) for the development setup and PR workflow.

---

## License

MIT — see [LICENSE](LICENSE).
