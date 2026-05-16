# Contributing to Cortex Context

Thank you for your interest in contributing! Cortex is a product knowledge graph service built with FastAPI + Neo4j + GraphRAG. Every contribution helps make it better.

## Ways to Contribute

- 🐛 **Report bugs** — [open a bug report](https://github.com/rodrigoroldan/cortex-context/issues/new?template=bug_report.yml)
- 💡 **Request features** — [open a feature request](https://github.com/rodrigoroldan/cortex-context/issues/new?template=feature_request.yml)
- 💬 **Ask questions** — [start a Discussion](https://github.com/rodrigoroldan/cortex-context/discussions)
- 🔧 **Submit a PR** — see the workflow below

---

## Development Setup

**Requirements**: Python 3.11+, Docker, Neo4j 5.x

```bash
# 1. Fork and clone
git clone https://github.com/<your-username>/cortex-context.git
cd cortex-context

# 2. Create a virtual environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate

# 3. Install with dev extras
pip install -e ".[dev]"

# 4. Copy and fill environment variables
cp .env.example .env
# Edit .env with your Neo4j connection and other settings

# 5. Start Neo4j (via Docker)
docker compose up -d neo4j

# 6. Run tests
pytest tests/

# 7. Lint and format
ruff check .
ruff format .
```

---

## Branching & PR Workflow

1. **Fork** the repo and create a branch from `main`:
   ```bash
   git checkout -b feature/my-feature
   ```
2. **Make your changes** — keep commits small and focused.
3. **Add tests** for any new behavior.
4. **Run the CI checks** locally before opening a PR:
   ```bash
   ruff check .
   pytest tests/
   ```
5. **Open a Pull Request** targeting `main` and fill in the PR template.

---

## Commit Convention

We use [Conventional Commits](https://www.conventionalcommits.org/):

| Prefix      | Use for                           |
| ----------- | --------------------------------- |
| `feat:`     | New feature                       |
| `fix:`      | Bug fix                           |
| `docs:`     | Documentation only                |
| `refactor:` | Code change (no feature/fix)      |
| `test:`     | Adding or updating tests          |
| `chore:`    | Build process, dependency updates |
| `perf:`     | Performance improvement           |

Example: `feat(ingestor): add ADR plugin for MADR format`

---

## Plugin Development

Cortex uses a **generic plugin pipeline** for ingestors. To add a new dimension:

1. Create a new plugin in `app/ingestor/plugins/<dim>/`
2. Implement the `IngestorPlugin` interface
3. Register it in `cortex.config.yaml`
4. Add tests in `tests/test_<dim>_plugin.py`

See existing plugins (`spec`, `service`, `workflow`) for reference.

---

## Code Style

- **Formatter**: `ruff format` (line length 100)
- **Linter**: `ruff check` (target Python 3.11)
- **Type hints**: required for all public functions
- **Docstrings**: Google style for public classes and functions

---

## Versioning

Cortex follows [Semantic Versioning](https://semver.org/). Releases are tagged `vMAJOR.MINOR.PATCH` and trigger the Docker publish workflow automatically.

---

## Code of Conduct

Please read and follow our [Code of Conduct](CODE_OF_CONDUCT.md).
