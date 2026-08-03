"""
tests/test_code_ingest.py — Cobertura para POST /api/v1/code/ingest:
limite de tamanho de payload (413) e contagem real de arestas persistidas
por categoria, em vez de ecoar len(payload.x) (issues #16 e #17).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def mock_neo4j_driver():
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[])
    mock_result.single = AsyncMock(return_value={"edge_count": 1})

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)

    with patch("app.db.neo4j.get_driver", return_value=driver), patch("app.routes.code.get_driver", return_value=driver):
        yield driver


@pytest.fixture
def api_client(mock_neo4j_driver):
    app = create_app()
    return TestClient(app)


def test_code_ingest_rejects_oversized_payload(api_client):
    payload = {"repo": "big-repo", "symbols": [{"id": f"sym-{i}", "name": f"fn{i}"} for i in range(10001)]}
    res = api_client.post("/api/v1/code/ingest", json=payload)
    assert res.status_code == 413
    assert "10001" in res.json()["detail"]


def test_code_ingest_within_limit_is_accepted(api_client, mock_neo4j_driver):
    mock_neo4j_driver.session.return_value.run.return_value.data = AsyncMock(
        return_value=[{"from_id": "sym-1", "to_id": "spec-1", "matched": True}]
    )
    payload = {
        "repo": "small-repo",
        "symbols": [{"id": "sym-1", "name": "fn1"}],
        "implements_specs": [{"symbol_id": "sym-1", "spec_id": "spec-1"}],
    }
    res = api_client.post("/api/v1/code/ingest", json=payload)
    assert res.status_code == 200


def test_code_ingest_specs_linked_reflects_real_matches_not_payload_length(api_client, mock_neo4j_driver):
    """2 implements_specs enviados, mas só 1 aresta de fato casa nós existentes —
    specs_linked deve refletir isso (1), não simplesmente ecoar len(payload.implements_specs) (2)."""
    mock_neo4j_driver.session.return_value.run.return_value.data = AsyncMock(
        return_value=[
            {"from_id": "sym-1", "to_id": "spec-1", "matched": True},
            {"from_id": "sym-2", "to_id": "spec-missing", "matched": False},
        ]
    )
    payload = {
        "repo": "small-repo",
        "symbols": [{"id": "sym-1", "name": "fn1"}, {"id": "sym-2", "name": "fn2"}],
        "implements_specs": [
            {"symbol_id": "sym-1", "spec_id": "spec-1"},
            {"symbol_id": "sym-2", "spec_id": "spec-missing"},
        ],
    }
    res = api_client.post("/api/v1/code/ingest", json=payload)
    assert res.status_code == 200
    data = res.json()
    assert data["specs_linked"] == 1
