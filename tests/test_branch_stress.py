"""
tests/test_branch_stress.py — Empirical stress testing harness for Shadow Graph & Branch Support.
Tests edge cases: missing branch context, draft priority join precedence, multi-tenant + branch filters,
main fallback, PR linter dry-run mode, and draft consolidation node ID retention.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.routes.query import _merge_nodes_priority, NodeContext
from app.routes.semantic import _merge_semantic_nodes_priority, SemanticNodeResult
from app.routes.nodes import _merge_node_summaries_priority, NodeSummary


@pytest.fixture
def mock_driver():
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
        yield driver, mock_session


@pytest.fixture
def client(mock_driver):
    app = create_app()
    return TestClient(app)


# ── Edge Case 1: Missing Branch Context (Default Fallback to 'main') ──────────────

def test_stress_missing_branch_context_fallback():
    from app.routes.dependencies import get_branch

    # No query param, no headers -> defaults to 'main'
    resolved = get_branch(x_cortex_branch=None, x_branch_name=None, x_branch=None, branch=None)
    assert resolved == "main"

    # Empty string query param -> defaults to 'main'
    resolved_empty = get_branch(x_cortex_branch=None, x_branch_name=None, x_branch=None, branch="   ")
    assert resolved_empty == "main"


# ── Edge Case 2: Priority Join Precedence (Draft overriding Main Node) ────────────

def test_stress_priority_join_draft_overrides_main():
    nodes = [
        NodeContext(
            id="spec-auth-01",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "Main Auth Spec (v1)", "branch": "main", "is_draft": False, "canonical_id": "spec-auth-01"},
        ),
        NodeContext(
            id="draft:feat/oauth:spec-auth-01",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "Draft OAuth Spec (v2)", "branch": "feat/oauth", "is_draft": True, "canonical_id": "spec-auth-01"},
        ),
    ]

    # Querying branch feat/oauth must return the draft version overriding main
    merged = _merge_nodes_priority(nodes, branch="feat/oauth")
    assert len(merged) == 1
    assert merged[0].id == "draft:feat/oauth:spec-auth-01"
    assert merged[0].properties["title"] == "Draft OAuth Spec (v2)"


# ── Edge Case 3: Fallback to Main when No Draft Exists ─────────────────────────────

def test_stress_priority_join_fallback_to_main_when_no_draft():
    nodes = [
        NodeContext(
            id="spec-payment-01",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "Main Payment Spec", "branch": "main", "is_draft": False, "canonical_id": "spec-payment-01"},
        )
    ]

    # Querying branch feat/unrelated with no draft node must fall back to returning main node
    merged = _merge_nodes_priority(nodes, branch="feat/unrelated")
    assert len(merged) == 1
    assert merged[0].id == "spec-payment-01"
    assert merged[0].properties["title"] == "Main Payment Spec"


# ── Edge Case 4: Branch Draft Only (No Main Node Exists) ──────────────────────────

def test_stress_priority_join_branch_draft_only():
    nodes = [
        NodeContext(
            id="draft:feat/new-feature:spec-new-01",
            labels=["Spec"],
            pillar="Intent",
            properties={"title": "New Spec Draft Only", "branch": "feat/new-feature", "is_draft": True, "canonical_id": "spec-new-01"},
        )
    ]

    merged = _merge_nodes_priority(nodes, branch="feat/new-feature")
    assert len(merged) == 1
    assert merged[0].id == "draft:feat/new-feature:spec-new-01"


# ── Edge Case 5: Multi-Tenant + Branch Combination Filters ────────────────────────

def test_stress_multitenant_branch_ingest_manifest(client):
    manifest_payload = {
        "source": "ci-diff",
        "domain_id": "tenant-enterprise-A",
        "branch": "feat/billing-v2",
        "draft": True,
        "nodes": [
            {
                "node_id": "spec-bill-01",
                "node_labels": ["Spec", "Intent"],
                "domain_id": "tenant-enterprise-A",
                "branch": "feat/billing-v2",
                "properties": {"title": "Tenant A Billing Spec"},
            }
        ],
        "edges": [],
    }

    res = client.post("/api/v1/ingest/manifest", json=manifest_payload)
    assert res.status_code == 200
    data = res.json()
    assert data["domain_id"] == "tenant-enterprise-A"
    assert data["nodes_upserted"] == 1


# ── Edge Case 6: PR Linter Dry-Run Mode ───────────────────────────────────────────

def test_stress_pr_linter_dry_run_validation(client):
    manifest_invalid = {
        "source": "ci-linter",
        "domain_id": "default",
        "branch": "feat/invalid",
        "dry_run": True,
        "nodes": [
            {
                "node_id": "",  # Missing node_id
                "node_labels": [""],  # Empty string label
                "properties": {},
            }
        ],
        "edges": [],
    }

    res = client.post("/api/v1/ingest/manifest?dry_run=true", json=manifest_invalid)
    assert res.status_code == 200
    data = res.json()
    assert data["dry_run"] is True
    assert data["linter_status"] == "failed"
    assert len(data["validation_errors"]) >= 2
    assert data["nodes_upserted"] == 0


# ── Edge Case 7: Draft Node Consolidation Cypher Verification ─────────────────────

def test_stress_draft_node_consolidation_cypher(client, mock_driver):
    driver, mock_session = mock_driver

    res = client.post(
        "/api/v1/graph/consolidate",
        json={"branch": "feat/payments", "domain_id": "cartoes"},
    )
    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["branch"] == "feat/payments"
    assert data["domain_id"] == "cartoes"

    # Verify Cypher query execution
    mock_session.run.assert_called_once()
    cypher = mock_session.run.call_args[0][0]
    kwargs = mock_session.run.call_args[1]

    assert "n.branch = $branch" in cypher
    assert "n.domain_id = $domain_id" in cypher
    assert "SET n.status = 'active', n.is_draft = false, n.branch = 'main'" in cypher
    assert kwargs.get("branch") == "feat/payments"
    assert kwargs.get("domain_id") == "cartoes"
