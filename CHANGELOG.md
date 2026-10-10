# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

- `spec` dimension is now **one node per folder** (`group_by: numbered_folder`): every `.md` inside a
  `NNN-slug/` folder contributes to a single `spec-NNN` node (primary file: `spec.md` > `README.md` >
  `plan.md` > alphabetical), with `files`, `source_folder` and `has_folder`. Per-file `DocumentChunk`s
  (`spec-NNN__<file>__chunk_<i>`) are always generated, with or without an embedder.
- Incremental `POST /api/v1/ingest/spec` with an optional JSON body
  (`{"spec_paths": [...], "issue_numbers": [...]}`); a full run (no body) now prunes folder-origin
  nodes and chunks that disappeared from the source. Response `details` report `folders_parsed`,
  `folders_deleted`, `chunks_upserted`, `issues_parsed`, `issues_error`, `ignored_paths`, `pruned_nodes`.
- Optional specs from GitHub issues (`spec_issues` in `cortex.config.yaml`; off by default).
- `GET /api/v1/query` also searches the body of specs (`doc_chunk_fts` full-text index) and returns
  `match_snippet` for body hits. The body search always runs and adds up to `max(2, limit // 3)` specs on
  top of the title hits; chunks containing the keyword literally rank first (the default tokenizer splits
  `api-staging` into `api` + `staging`).
- `AFFECTS` edges (`Spec` → `Service`) are recomputed at the end of every spec ingestion (all specs on a
  full run, only the touched ones on an incremental run), removing stale ones first. `POST /ingest`
  uses the same helper (`rebuild_spec_affects`).

### Fixed

- `spec.yaml` created a `spec_fulltext` index while `/query` reads `spec_fts`, so on a fresh install the
  title/summary search was always empty. The dimension now creates `spec_fts` (`IF NOT EXISTS`; a no-op where
  it already exists). If a graph has only `spec_fulltext`, run `DROP INDEX spec_fulltext` once and restart.
- A folder with both `spec.md` and `plan.md` no longer produces two nodes with the same id overwriting
  each other; nested `README.md` files no longer become junk nodes such as `spec-contracts`.

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
