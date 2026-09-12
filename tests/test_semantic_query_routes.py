"""
tests/test_semantic_query_routes.py — Regression tests for app/routes/semantic.py and
app/routes/query.py graph-expansion Cypher.
"""
from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import HTTPException

from app.routes.query import query_context
from app.routes.semantic import SemanticSearchRequest, semantic_search


class _FakeNeo4jNode(dict):
    """Minimal stand-in for a neo4j.graph.Node: dict-like + a `.labels` attribute."""

    def __init__(self, props: dict, labels: list[str]):
        super().__init__(props)
        self.labels = labels


def _make_mock_session(run_results: list[list[dict]]):
    """Builds a mocked AsyncSession whose `.run(...)` returns each record set in order."""
    results = []
    for records in run_results:
        mock_result = MagicMock()
        mock_result.data = AsyncMock(return_value=records)
        results.append(mock_result)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=results)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


@pytest.mark.asyncio
async def test_semantic_search_hops_zero_does_not_drop_seed_nodes():
    """
    Regression for #26: with hops=0, `all_rels` is a plain empty list. The old Cypher did
    `UNWIND all_rels AS rel_list` right before RETURN — UNWIND over an empty list produces
    *zero rows*, silently wiping out the already-collected `nodes` and making the whole
    response `nodes: []` even though `chunk_count`/`parent_count` were > 0 (exactly the repro
    in the issue: two matched parent chunks, zero relationships since hops=0).
    """
    seed_node = _FakeNeo4jNode(
        {"id": "spec-1", "title": "Pagamento PIX", "pillar": "Intent", "status": "active"},
        labels=["Spec", "Intent"],
    )
    driver, mock_session = _make_mock_session([[{"nodes": [seed_node], "edges": []}]])

    with patch("app.routes.semantic.is_embedder_enabled", return_value=True), patch(
        "app.routes.semantic.embed_texts", new=AsyncMock(return_value=[[0.1, 0.2, 0.3]])
    ), patch(
        "app.routes.semantic.vector_search",
        new=AsyncMock(return_value=[{"parent_id": "spec-1", "score": 0.9}, {"parent_id": "spec-2", "score": 0.7}]),
    ), patch("app.routes.semantic.get_driver", return_value=driver):
        payload = SemanticSearchRequest(query="pagamento pix", top_k=5, hops=0)
        response = await semantic_search(payload, domain_id="default", branch="main", _token="")

    assert response.query_meta["chunk_count"] == 2
    assert response.query_meta["parent_count"] == 2
    assert len(response.nodes) == 1, "seed nodes must survive even when there are zero relationships"
    assert response.nodes[0].id == "spec-1"
    assert response.edges == []

    cypher = mock_session.run.call_args.args[0]
    assert "UNWIND all_rels" not in cypher, "empty all_rels must not be UNWIND'd (drops the whole row)"
    assert "reduce(" in cypher


@pytest.mark.asyncio
async def test_query_context_expand_with_no_relationships_keeps_seed_nodes():
    """Same root cause as above, mirrored in query.py's /query (query_product_context)."""
    fts_record = {
        "props": {"id": "spec-1", "title": "Pagamento PIX", "pillar": "Intent"},
        "labels": ["Spec", "Intent"],
        "score": 1.0,
    }
    seed_node = _FakeNeo4jNode(
        {"id": "spec-1", "title": "Pagamento PIX", "pillar": "Intent"}, labels=["Spec", "Intent"]
    )
    driver, mock_session = _make_mock_session(
        [
            [fts_record],  # spec_fulltext
            [],  # service_fulltext
            [],  # workflow_fulltext
            [{"nodes": [seed_node], "edges": []}],  # expand
        ]
    )

    with patch("app.routes.query.get_driver", return_value=driver):
        response = await query_context(
            keywords="pagamento", limit=8, hops=1, pillar=None, dimension=None,
            domain_id="default", branch="main", _token="",
        )

    assert len(response.nodes) == 1
    assert response.nodes[0].id == "spec-1"
    assert response.edges == []

    expand_cypher = str(mock_session.run.call_args_list[-1].args[0])
    assert "UNWIND all_rels" not in expand_cypher
    assert "reduce(" in expand_cypher


@pytest.mark.asyncio
async def test_query_context_bounds_response_time_when_driver_hangs():
    """
    Regression for #33/#34/#35: a server-side Neo4j transaction timeout was observed
    (`SHOW TRANSACTIONS` -> "Terminated with reason: TransactionTimedOutClientConfiguration")
    without the async driver ever surfacing that failure back to the awaiting coroutine —
    the `await session.run(...).data()` call itself just hung. The Neo4j-side timeout
    (Query(..., timeout=X)) is defense in depth, but the route MUST still bound its own
    response time regardless of whether the driver ever notices the server-side abort.
    """
    fts_record = {
        "props": {"id": "spec-1", "title": "Pagamento PIX", "pillar": "Intent"},
        "labels": ["Spec", "Intent"],
        "score": 1.0,
    }

    call_count = 0

    async def _run_side_effect(*_args, **_kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            result = MagicMock()
            result.data = AsyncMock(return_value=[fts_record])
            return result
        if call_count in (2, 3):
            result = MagicMock()
            result.data = AsyncMock(return_value=[])
            return result
        # 4th call = the expand query: simulates the driver hanging even though
        # the Neo4j server already terminated the transaction on its own timeout.
        await asyncio.sleep(3600)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(side_effect=_run_side_effect)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)

    with patch("app.routes.query.get_driver", return_value=driver), patch(
        "app.routes.query._ROUTE_TIMEOUT_S", 0.05
    ):
        start = time.monotonic()
        with pytest.raises(HTTPException) as exc_info:
            await query_context(
                keywords="pagamento", limit=8, hops=1, pillar=None, dimension=None,
                domain_id="default", branch="main", _token="",
            )
        elapsed = time.monotonic() - start

    assert exc_info.value.status_code == 504
    assert elapsed < 1.0, f"route must bound response time even if the driver hangs, took {elapsed}s"
