"""
tests/test_branch_support.py — Unit & Integration tests for Shadow Graph & Branch Support.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.core.parsers.base import NodeData
from app.core.parsers.manifest import IngestManifest, ManifestNode, ManifestEdge
from app.main import create_app
from app.routes.query import _merge_nodes_priority, NodeContext
from app.routes.nodes import _merge_node_summaries_priority, NodeSummary


@pytest.fixture
def mock_neo4j_driver():
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value={"deleted": 1, "cnt": 1, "created": 1})

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)

    with patch("app.db.neo4j.get_driver", return_value=driver), patch(
        "app.routes.ingest.get_driver", return_value=driver
    ), patch("app.routes.query.get_driver", return_value=driver), patch(
        "app.routes.semantic.get_driver", return_value=driver
    ), patch(
        "app.routes.nodes.get_driver", return_value=driver
    ), patch(
        "app.routes.consolidate.get_driver", return_value=driver
    ):
        yield driver


@pytest.fixture
def api_client(mock_neo4j_driver):
    app = create_app()
    return TestClient(app)


def test_ingest_manifest_dry_run_linter_mode(api_client):
    manifest_data = {
        "source": "ci-linter",
        "branch": "feat/oauth2",
        "draft": True,
        "dry_run": True,
        "nodes": [
            {
                "node_id": "spec-auth-01",
                "node_labels": ["Spec", "Intent"],
                "properties": {"title": "OAuth2 Auth Flow"},
            }
        ],
        "edges": [],
    }
    res = api_client.post("/api/v1/ingest/manifest?dry_run=true", json=manifest_data)
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is True
    assert data["linter_status"] == "passed"
    assert data["nodes_upserted"] == 0
    assert data["edges_upserted"] == 0
    assert "Dry-run linter complete" in data["message"]


def test_ingest_manifest_draft_composite_node_ids(api_client, mock_neo4j_driver):
    manifest_data = {
        "source": "cli-sync",
        "branch": "feat/payment",
        "draft": True,
        "nodes": [
            {
                "node_id": "spec-pay-01",
                "node_labels": ["Spec"],
                "properties": {"title": "Payment Draft"},
            }
        ],
        "edges": [
            {
                "from_id": "spec-pay-01",
                "to_id": "service-gateway",
                "relationship": "AFFECTS",
            }
        ],
    }
    res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is False
    assert data["nodes_upserted"] == 1
    assert data["edges_upserted"] == 1


def test_left_outer_priority_merge_deduplication():
    nodes = [
        NodeContext(
            id="spec-100",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "Main Canonical Spec", "branch": "main", "is_draft": False},
        ),
        NodeContext(
            id="draft:feat/oauth:spec-100",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "Branch Draft Spec", "branch": "feat/oauth", "is_draft": True, "canonical_id": "spec-100"},
        ),
    ]

    merged = _merge_nodes_priority(nodes, branch="feat/oauth")
    assert len(merged) == 1
    assert merged[0].properties["title"] == "Branch Draft Spec"
    assert merged[0].properties["branch"] == "feat/oauth"

    merged_main = _merge_nodes_priority(nodes, branch="main")
    assert len(merged_main) == 2


def test_merge_node_summaries_priority():
    items = [
        NodeSummary(id="spec-100", label="Spec", properties={"title": "Main Spec", "branch": "main", "is_draft": False}),
        NodeSummary(id="draft:feat/login:spec-100", label="Spec", properties={"title": "Draft Spec", "branch": "feat/login", "is_draft": True, "canonical_id": "spec-100"}),
    ]

    merged = _merge_node_summaries_priority(items, branch="feat/login")
    assert len(merged) == 1
    assert merged[0].properties["title"] == "Draft Spec"


def test_consolidate_draft_nodes_endpoint(api_client, mock_neo4j_driver):
    res = api_client.post("/api/v1/graph/consolidate", json={"branch": "feat/login", "domain_id": "default"})
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["branch"] == "feat/login"
    assert data["domain_id"] == "default"
    assert isinstance(data["promoted_nodes"], list)
