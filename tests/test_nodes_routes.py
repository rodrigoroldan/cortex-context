"""
tests/test_nodes_routes.py — Regression tests for app/routes/nodes.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.datastructures import QueryParams
from starlette.requests import Request

from app.core.dimension_loader import DimensionConfig
from app.routes.nodes import list_nodes


def _make_mock_driver(records: list[dict]):
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=records)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


def _make_request(query_string: str) -> Request:
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/api/v1/nodes/history",
        "headers": [],
        "query_string": query_string.encode(),
    }
    request = Request(scope)
    # Request.query_params is normally derived lazily from scope; force it explicitly
    # so the test doesn't depend on Starlette's internal caching behavior.
    request._query_params = QueryParams(query_string)
    return request


@pytest.mark.asyncio
async def test_list_nodes_query_filter_does_not_collide_with_session_run_query_kwarg():
    """
    Regression for #24: `?query=...` used to be forwarded as **filters straight into
    AsyncSession.run(query, parameters=None, **kwargs) — colliding with the driver's own
    positional `query` param and raising `TypeError: run() got multiple values for
    argument 'query'` (surfaced to callers as a bare 500). Filters must be passed via the
    `parameters=` dict instead.
    """
    record = {"props": {"id": "hist-1", "query": "deploy", "domain_id": "default", "branch": "main"}}
    driver, mock_session = _make_mock_driver([record])

    dim_cfg = DimensionConfig(dimension="history", node_label="History")

    from app.db import neo4j as neo4j_module
    old_driver = neo4j_module._driver
    neo4j_module._driver = driver
    try:
        with patch("app.routes.nodes._load_dimension_map", return_value={"history": dim_cfg}):
            request = _make_request("query=deploy&limit=5")
            response = await list_nodes(
                dim_key="history", request=request, domain_id="default", branch="main", _token=""
            )

        assert len(response) == 1
        assert response[0].id == "hist-1"

        call_kwargs = mock_session.run.call_args.kwargs
        assert "query" not in call_kwargs, "filter values must not be forwarded as **kwargs to session.run"
        assert call_kwargs["parameters"]["query"] == "deploy"
    finally:
        neo4j_module._driver = old_driver


@pytest.mark.asyncio
async def test_list_nodes_applies_limit_to_cypher():
    """Regression for #24 (bonus finding): `limit` was accepted but never applied to the Cypher."""
    driver, mock_session = _make_mock_driver([])
    dim_cfg = DimensionConfig(dimension="history", node_label="History")

    from app.db import neo4j as neo4j_module
    old_driver = neo4j_module._driver
    neo4j_module._driver = driver
    try:
        with patch("app.routes.nodes._load_dimension_map", return_value={"history": dim_cfg}):
            request = _make_request("limit=5")
            await list_nodes(dim_key="history", request=request, domain_id="default", branch="main", _token="")

        cypher = mock_session.run.call_args.args[0]
        assert "LIMIT $limit" in cypher
        assert mock_session.run.call_args.kwargs["parameters"]["limit"] == 5
    finally:
        neo4j_module._driver = old_driver
