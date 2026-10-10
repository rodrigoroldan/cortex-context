# Cortex Context

> **The Enterprise Context Store for AI Agents.** Transform your fragmented specs, architecture decisions, and code history into a real-time, deterministic Knowledge Graph. The ultimate backbone for the AI Development Life Cycle (AI-DLC).

[![CI](https://github.com/rodrigoroldan/cortex-context/actions/workflows/ci.yml/badge.svg)](https://github.com/rodrigoroldan/cortex-context/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/Python-3.11+-blue.svg)](https://www.python.org/)
[![Docker](https://img.shields.io/badge/Docker-ready-blue.svg)](https://hub.docker.com/r/rodrigoroldan/cortex-context)
[![Neo4j](https://img.shields.io/badge/neo4j-5.x-008CC1)](https://neo4j.com/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115%2B-009688)](https://fastapi.tiangolo.com/)
[![npm](https://img.shields.io/badge/cli-@cortex--context%2Fcli-red)](https://www.npmjs.com/package/@cortex-context/cli)

---

## What is Cortex Context?

In the era of AI-assisted coding, giving an AI Agent access to a git repository is not enough. AI Agents (like GitHub Copilot, Cline, or Antigravity) constantly hallucinate when they lack the tribal knowledge, business rules, and cross-repo architectural decisions that aren't explicitly written in the code they are currently reading.

**Cortex Context solves the Context Starvation problem.** It is a **Knowledge Feature Store (Context Store)** for LLMs. 

Just like a Data Engineer uses a Feature Store to serve pre-calculated data to Machine Learning models, an Engineering team uses Cortex Context to serve pre-calculated, deterministic context graphs to AI Agents via the [Model Context Protocol (MCP)](https://modelcontextprotocol.io/).

Instead of your AI assistant trying (and failing) to read 50 disjointed repositories to understand a business rule, it queries the live graph in milliseconds:

```
"Which services does the Tax 2026 Spec affect?"
  → Graph traversal: (:Spec:Intent)-[:AFFECTS]->(:Service:System)
  → Returns: billing-api, payment-gateway (with full architectural context)
```

## Why Cortex? (The AI-DLC Backbone)

Pure semantic search (Vector RAG) fails at deterministic traversal. Standard enterprise catalogs (like Spotify Backstage) are rigid and designed for humans to read on dashboards.

Cortex Context is built **API-first for AI Agents**:
1. **GraphRAG + Vector RAG:** Combines the exactness of Neo4j graph relationships (`[:AFFECTS]`, `[:IMPLEMENTS]`) with semantic embeddings for fuzzy business concepts.
2. **Zero Cognitive Tax (80/20 Rule):** 80% of the graph is built automatically by parsing your existing code (ASTs, Temporal workflows, OpenAPIs, Backstage `catalog-info.yaml`). Only 20% requires cognitive effort (writing Specs via Markdown), which your local AI Agent is guided to write for you.
3. **The Shadow Graph (Branch-Aware):** Cortex isn't just a mirror of your `main` branch. It understands Work-In-Progress (Drafts). When an Agent is coding on a feature branch, it queries Cortex and sees the "Canonical Truth" merged with the "Branch Speculative Truth", allowing it to code with a view of the future.

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
| `GITHUB_TOKEN`     | GitHub token for `source_type: github_api` and `spec_issues` (optional) | —                       |
| `CODE_INGEST_MAX_ITEMS` | Max combined files+symbols+calls+links per `POST /code/ingest` request — rejected with `413` above this (split into smaller chunks client-side) | `10000` |

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

> **Editing config after deploy**: the reference `docker-compose.yml` mounts
> `cortex.config.yaml`, `app/dimensions/` and `plugins/` as read-only volumes
> over the copies baked into the image, so `git pull` + `docker compose restart
> cortex` picks up config/dimension changes with **no rebuild** needed. If you
> run a custom image/compose file without those mounts, the container keeps
> using whatever was in the image at build time — silently, with no warning —
> so you must `docker compose build` (or re-pull a rebuilt image) for changes
> to take effect.

---

## API Reference

### Ingestion

| Method | Endpoint                   | Description                                          |
| ------ | -------------------------- | ---------------------------------------------------- |
| `POST` | `/api/v1/ingest/{dim_key}` | Ingest a specific dimension (e.g. `spec`, `service`). `spec` also accepts an optional JSON body for **incremental** ingestion — see [Specs as folders](#specs-as-folders-and-github-issues) |
| `POST` | `/api/v1/ingest`           | Ingest all active dimensions (a full `spec` ingestion, with pruning) |
| `POST` | `/api/v1/ingest/manifest`  | Bulk upsert of pre-computed `nodes`/`edges`, any dimension |
| `POST` | `/api/v1/code/ingest`      | Bulk upsert of a pre-computed AST/call-graph (files, symbols, calls) |

**Client-side parsing**: `/ingest/{dim_key}` and `/ingest` require the server to have
filesystem access to your sources (bind mount). If you'd rather parse on the client
(e.g. in CI, where the repo is already checked out) and just push the result, use
`/ingest/manifest` (generic — any `node_labels`/`domain_id`/`branch`) or `/code/ingest`
(purpose-built for AST/call-graph data: `files`, `symbols`, `calls`, `implements_specs`,
`complies_adrs`, `exposes_apis`). Both respect `domain_id`/`branch`/`draft` for
multi-tenancy and the Shadow Graph, and avoid ever mounting your repos into the
server — see [`app/routes/ingest.py`](app/routes/ingest.py) and
[`app/routes/code.py`](app/routes/code.py) for the request schemas.

Both endpoints upsert nodes/edges in batches (`UNWIND`, grouped by label combination /
relationship type) instead of one Neo4j session per node/edge, so large ASTs don't need
to be chunked for performance — `CODE_INGEST_MAX_ITEMS` (default `10000`) is only a hard
backstop against pathologically large single requests, not a tuning knob you need to hit.

#### Specs as folders and GitHub issues

The `spec` dimension (`group_by: numbered_folder`) treats a **folder** as the unit of a spec: every
`.md` file (recursively) inside a folder named `NNN-slug` (3+ digits) contributes to one `(:Spec)`
node `spec-NNN`. Files outside a numbered folder are ignored, and a nested `README.md` is just another
file of its spec, not a spec of its own.

- **Primary file** (title, summary, frontmatter, status, `repos`, cross-references): `spec.md` >
  `README.md` > `plan.md` (folder root) > first in alphabetical order.
- The node also carries `files`, `files_str`, `source_folder` and `has_folder: true`. Files larger than
  1 MB are skipped.
- **Repeated numbers**: several folders may share a number (`217-alpha-draft`, `217-gamma-register`).
  Among *all* folders on disk with the same number, in alphabetical order, the first keeps `spec-217` and
  the others become `spec-217-<rest of the folder name>`; chunks and cross-references of a renamed folder use
  its new id, references to `spec-217` go to the owner, and the GitHub issue `#217` enriches `spec-217`. The
  rule only looks at folder names, so full and incremental runs agree: touching one folder re-processes the
  sibling folders with the same number. If the owner folder is removed, the next folder is promoted to
  `spec-217` and the old `spec-217-<slug>` node is deleted (incremental) or pruned (full). A folder whose
  `frontmatter.id` differs from `spec-NNN` keeps it (and does not free `spec-NNN` for the next folder); two
  folders with the same explicit id collide — the second one is skipped and reported in
  `details.id_collisions`. Re-identified folders are listed in `details.duplicate_folders` as
  `{"folder": …, "id": …}` (a warning, not an error).
- Every file is split into `DocumentChunk` nodes (`spec-NNN__<file-slug>__chunk_<i>`, with
  `source_file` and `chunk_source: "folder"`) — **always**, with or without an embedder. They feed the
  `doc_chunk_fts` full-text index; embeddings stay optional and computed in the background.

**Incremental ingestion** — `POST /api/v1/ingest/spec` with an optional JSON body:

```json
{ "spec_paths": ["specs/861-example/RUNBOOK.md", "862-other/spec.md"], "issue_numbers": [861] }
```

Paths may be relative to the repo (`specs/…`) or to the specs folder; the closest `\d{3,}-…` segment
names the spec folder. Only the affected folders are re-parsed (their old folder chunks are deleted
first, so removed or shrunk files leave nothing behind); a folder that no longer exists on disk has its
chunks deleted and, unless it has issue data, its node too. Paths that do not map to a spec folder are
returned in `details.ignored_paths`. Incremental runs do not sweep for stale data: they only remove
what the request itself covers (listed folders that vanished, listed issues that lost the label).

With no body (or an empty one) the whole dimension is ingested, then **pruned**: Spec nodes of folder
origin that were not rewritten in this run (and have no issue data) are deleted with their chunks —
this also cleans up nodes created by the old one-node-per-file parser. Nothing is pruned if the specs
directory is missing or contains no spec folder (e.g. a volume that failed to mount).

At the end of every spec ingestion the `AFFECTS` edges (`Spec` → `Service`, from the spec's `repos`) are
recomputed — for all specs on a full run, only for the touched specs on an incremental one. The old
`AFFECTS` of those specs that this mechanism created are removed first, so a repo dropped from the
frontmatter leaves no stale edge (edges created through `/ingest/manifest` or by hand are kept).

Response `details`: `folders_parsed`, `folders_deleted`, `chunks_upserted`, `issues_parsed`,
`issues_error`, `ignored_paths`, `pruned_nodes` (plus `mode`, `affects_edges`, `files_parsed`, `warnings`, …).

**Specs from GitHub issues** (optional) — set the repo in `cortex.config.yaml` (empty = off) and put a
`GITHUB_TOKEN` in `.env` for private repos (read access to issues):

```yaml
spec_issues:
  repo: "acme/specs-repo"
  label: "spec"
```

Issues with the label (open or closed; PRs are ignored) become `(:Spec)` nodes. The spec number is the
`NNN` in a title like `Spec NNN — <title>` (falling back to the issue number, zero-padded to 3 digits).
The node gets `issue_number`, `issue_url`, `issue_state`, `issue_state_reason`, `issue_updated_at`,
`has_issue: true` and chunks of the issue body (`spec-NNN__issue__chunk_<i>`, `source_file: "issue#N"`).
If the spec has **no folder**, the issue also sets `title`, `summary` (first ~300 chars) and `status`
(`open` → `planned`, closed as completed → `done`, closed as not planned → `deprecated`); if it has a
folder, the folder wins. An issue that loses the label is pruned on the next full run. GitHub failures
(401, network) never abort the ingestion — they are reported in `details.issues_error`.

### Full-Text Search

| Method | Endpoint        | Description                                    |
| ------ | --------------- | ---------------------------------------------- |
| `GET`  | `/api/v1/query` | Cross-dimension FTS with 1-hop graph expansion |

**Query params**: `keywords` (required), `limit` (default `8`), `hops` (default `1`), `pillar` (`Intent|System|Implementation|Runtime`), `dimension` (`spec|service|...`)

Besides title/summary/labels, the query also searches the **body** of specs (`doc_chunk_fts`) — always,
not only when the title search leaves room: each matching chunk adds its parent spec as a seed (if it is not
one already), up to `max(2, limit // 3)` body specs **on top of** the title hits, so the total can exceed
`limit`. The default tokenizer splits `https://api-staging.example.dev/x` into `api` + `staging.example.dev`,
so a search for `api-staging` matches `api` in hundreds of chunks; the query therefore fetches more
candidates (`max(limit * 10, 50)`) and ranks chunks that contain a keyword **literally** (case-insensitive
substring) before the rest, then by score. Such nodes carry `match_snippet` (`"<source_file>:
…excerpt…"`, anchored on the literal occurrence when there is one), and the same excerpt is appended to the
`summary` **in the response only** (`summary + "\n> 🔎 <source_file>: …excerpt…"`) so clients that only
display `summary` still show why the spec matched. Nothing is written to the graph.

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
| `spec`     | Intent         | `builtin.markdown_frontmatter` | `**/*.md` in numbered folders `NNN-slug/` (filesystem, one node per folder) + optional GitHub issues |
| `service`  | System         | `builtin.agents_manifest`      | `AGENTS.md` / `agents.md` (github_api or filesystem) |
| `workflow` | Implementation | `builtin.markdown_frontmatter` | `**/temporal/**/*.yaml`, `**/workflows/**/*.md`      |
| `adr`      | System         | `builtin.markdown_frontmatter` | `**/docs/adr/**/*.md`                                |
| `concept`  | Intent         | `builtin.markdown_frontmatter` | `**/*.md` under `/concepts` (filesystem)             |
| `history`  | Implementation | `builtin.markdown_frontmatter` | `**/*.md` under `/history` (filesystem)              |
| `code`     | Implementation | `builtin.code_ast`             | `**/*.{ts,js,tsx,jsx,py,go,java}` — not in `active_dimensions` by default; fed via `POST /api/v1/code/ingest` bulk upsert instead of the filesystem glob (see [Client-side parsing](#ingestion)) |

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

A `repos:` field in the spec's primary file (`spec.md`, `README.md` or `plan.md`) frontmatter creates `(:Spec)-[:AFFECTS]->(:Service)` edges, connecting specs to the services they impact:

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
