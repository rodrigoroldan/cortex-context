# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.2.0] - 2026-05-16

### Added

- Cortex Context v3 with I.S.I.R ontology (Intent, System, Implementation, Runtime)
- Multi-label Neo4j nodes for cross-pillar GraphRAG queries
- Generic ingestor plugin pipeline (builtin + customizable)
- Vector RAG with optional embeddings: none / local (sentence-transformers) / OpenAI
- Semantic search endpoint `POST /api/v1/query/semantic`
- `workflow` dimension plugin for Temporal.io workflows
- `adr` dimension plugin for Architecture Decision Records
- Bitemporal graph support (valid_time + transaction_time)
- Public landing page at `https://cortex-context.rolds.dev`
- Docker multi-stage build published to GHCR + Docker Hub
- CLI integration via `@cortex-context/cli` (`npx cortex-context init`)

### Changed

- Initial public release as `cortex-context`
- Upgraded to Neo4j 5.x driver + APOC
- FTS index rebuilt with multi-label support
- Config schema: `cortex.config.yaml` with per-dimension plugin overrides

### Fixed

- Race condition in `TemporalScheduleInitializer` on cold start
- Embedding re-indexing skipping existing vectors on partial ingest

## [0.1.0] - 2026-01-01

### Added

- Initial release: FastAPI + Neo4j product knowledge graph
- Spec and service ingestor plugins
- FTS cross-dimension query (`GET /api/v1/query`)
- MCP server integration for GitHub Copilot context
- Docker Compose for local development

[0.2.0]: https://github.com/rodrigoroldan/cortex-context/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rodrigoroldan/cortex-context/releases/tag/v0.1.0
