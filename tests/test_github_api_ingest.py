"""
tests/test_github_api_ingest.py — Cobertura para ingest_strategy: github (issue #9).

Garante que dimensões `builtin.agents_manifest` (ex: "service") ingerem via
GitHub Contents API quando `cortex.config.yaml` declara `ingest_strategy: github`
e `repo_sources`, em vez de sempre usar o filesystem hardcoded (`/repos`).
"""
from __future__ import annotations

import base64
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi import BackgroundTasks

from app.core.dimension_loader import DimensionConfig
from app.routes.ingest import _fetch_github_file, _run_ingest_pipeline


def _service_dim_config() -> DimensionConfig:
    return DimensionConfig(
        dimension="service",
        node_label="Service",
        pillar="System",
        parser="builtin.agents_manifest",
        source_type="filesystem",
        source_path="/repos",
        source_patterns=["**/AGENTS.md", "**/agents.md"],
    )


class _FakeResponse:
    def __init__(self, status_code: int, json_data: dict):
        self.status_code = status_code
        self._json_data = json_data

    def json(self) -> dict:
        return self._json_data


class _FakeAsyncClient:
    def __init__(self, response_by_repo: dict[str, _FakeResponse], **kwargs):
        self._response_by_repo = response_by_repo

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str, headers=None, params=None):
        for repo_name, response in self._response_by_repo.items():
            if f"/{repo_name}/" in url:
                return response
        return _FakeResponse(404, {})


def _b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@pytest.mark.asyncio
async def test_fetch_github_file_success():
    fake_client = _FakeAsyncClient({"my-backend": _FakeResponse(200, {"content": _b64("# My Backend\n")})})
    with patch("app.routes.ingest.httpx.AsyncClient", return_value=fake_client):
        content = await _fetch_github_file("myorg", "my-backend", "AGENTS.md", "main", "")
    assert content == "# My Backend\n"


@pytest.mark.asyncio
async def test_fetch_github_file_404_returns_none():
    fake_client = _FakeAsyncClient({"my-backend": _FakeResponse(404, {})})
    with patch("app.routes.ingest.httpx.AsyncClient", return_value=fake_client):
        content = await _fetch_github_file("myorg", "my-backend", "AGENTS.md", "main", "")
    assert content is None


@pytest.mark.asyncio
async def test_fetch_github_file_network_error_returns_none():
    with patch("app.routes.ingest.httpx.AsyncClient", side_effect=httpx.ConnectError("boom")):
        content = await _fetch_github_file("myorg", "my-backend", "AGENTS.md", "main", "")
    assert content is None


@pytest.mark.asyncio
async def test_run_ingest_pipeline_uses_github_api_when_ingest_strategy_github():
    """service.yaml declara source_type: filesystem, mas ingest_strategy: github
    no cortex.config.yaml deve forçar a busca via GitHub API (issue #9)."""
    cfg = {
        "ingest_strategy": "github",
        "github": {"owner": "myorg", "default_branch": "main"},
        "repo_sources": [
            {"name": "backend", "service_id": "service-backend", "agents_path": "AGENTS.md", "branch": "main"},
            {"name": "frontend", "service_id": "service-frontend", "agents_path": "agents.md", "branch": "main"},
        ],
    }

    async def fake_fetch(owner, repo, path, branch, token):
        content_by_repo = {
            "backend": "# Backend Service\n\n## Capabilities\n- Handles orders\n",
            "frontend": "# Frontend App\n\n## Capabilities\n- Renders UI\n",
        }
        return content_by_repo.get(repo)

    captured_nodes = {}

    async def fake_ingest_nodes(driver, nodes, **kwargs):
        captured_nodes["nodes"] = nodes
        return len(nodes)

    with patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest._fetch_github_file", side_effect=fake_fetch), \
         patch("app.routes.ingest.ingest_nodes", side_effect=fake_ingest_nodes), \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock, return_value=0):

        res = await _run_ingest_pipeline(
            dim_config=_service_dim_config(),
            plugins_dir=None,
            background_tasks=BackgroundTasks(),
            domain_id="default",
            cfg=cfg,
        )

    assert res.details["source_type"] == "github_api"
    assert res.details["files_parsed"] == 2
    assert res.details["files_failed"] == 0

    node_ids = sorted(n.node_id for n in captured_nodes["nodes"])
    assert node_ids == ["service-backend", "service-frontend"]


@pytest.mark.asyncio
async def test_run_ingest_pipeline_github_api_skips_repo_on_fetch_failure():
    cfg = {
        "ingest_strategy": "github",
        "github": {"owner": "myorg", "default_branch": "main"},
        "repo_sources": [
            {"name": "backend", "service_id": "service-backend", "agents_path": "AGENTS.md"},
            {"name": "private-repo-no-token", "service_id": "service-private", "agents_path": "AGENTS.md"},
        ],
    }

    async def fake_fetch(owner, repo, path, branch, token):
        return "# Backend Service\n" if repo == "backend" else None

    with patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest._fetch_github_file", side_effect=fake_fetch), \
         patch("app.routes.ingest.ingest_nodes", new_callable=AsyncMock, return_value=1), \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock, return_value=0):

        res = await _run_ingest_pipeline(
            dim_config=_service_dim_config(),
            plugins_dir=None,
            background_tasks=BackgroundTasks(),
            domain_id="default",
            cfg=cfg,
        )

    assert res.details["files_parsed"] == 1
    assert res.details["files_failed"] == 0  # fetch failure is skipped, not a parse failure


@pytest.mark.asyncio
async def test_run_ingest_pipeline_defaults_to_filesystem_without_ingest_strategy_github(tmp_path):
    """Sem ingest_strategy: github, o comportamento de source_type: filesystem é preservado."""
    (tmp_path / "backend").mkdir()
    (tmp_path / "backend" / "AGENTS.md").write_text("# Backend Service\n")

    dim_cfg = _service_dim_config()
    dim_cfg.source_path = str(tmp_path)
    dim_cfg.source_patterns = ["**/AGENTS.md"]  # evita duplicar em fs case-insensitive (macOS)

    with patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest._fetch_github_file") as mock_fetch, \
         patch("app.routes.ingest.ingest_nodes", new_callable=AsyncMock, return_value=1), \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock, return_value=0):

        res = await _run_ingest_pipeline(
            dim_config=dim_cfg,
            plugins_dir=None,
            background_tasks=BackgroundTasks(),
            domain_id="default",
            cfg={},
        )

    mock_fetch.assert_not_called()
    assert res.details["source_type"] == "filesystem"
    assert res.details["files_parsed"] == 1
