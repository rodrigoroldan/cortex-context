# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.0.1] - 2026-05-16

### Added

- Initial public release
- Cortex Context Server with I.S.I.R ontology (Intent, System, Implementation, Runtime)
- Multi-label Neo4j nodes for cross-pillar GraphRAG queries
- Generic ingestor plugin pipeline (builtin + customizable)
- Vector RAG with optional embeddings: none / local (sentence-transformers) / OpenAI
- Semantic search endpoint `POST /api/v1/query/semantic`
- `workflow` dimension plugin for Temporal.io workflows
- `adr` dimension plugin for Architecture Decision Records
- Bitemporal graph support (valid_time + transaction_time)
- Docker multi-stage build
- MCP stdio server for AI agent integration
- CLI integration via `@cortex-context/cli` (`npx cortex-context init`)

- Spec and service ingestor plugins
- FTS cross-dimension query (`GET /api/v1/query`)
- MCP server integration for GitHub Copilot context
- Docker Compose for local development

[0.2.0]: https://github.com/rodrigoroldan/cortex-context/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/rodrigoroldan/cortex-context/releases/tag/v0.1.0
