# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.2.x   | ✅        |
| 0.1.x   | ❌        |

## Reporting a Vulnerability

**Please do not report security vulnerabilities through public GitHub issues.**

Report vulnerabilities via [GitHub Security Advisories](https://github.com/rodrigoroldan/cortex-context/security/advisories/new). You will receive a response within 72 hours.

## Scope

Security concerns relevant to this project:

- **Neo4j query injection** — Cypher queries built from user input
- **File system access** — Ingestor plugins reading local files
- **Network requests** — HTTP calls to embedding providers (OpenAI, etc.)
- **Docker image** — Base image vulnerabilities, exposed ports
- **Environment variables** — Secrets in `.env` / `cortex.config.yaml`

## Out of Scope

- Vulnerabilities in Neo4j, FastAPI, or other upstream dependencies (report to them directly)
- Issues in development/test configurations not used in production
