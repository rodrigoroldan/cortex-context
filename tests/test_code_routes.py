"""
tests/test_code_routes.py — Regression tests for app/routes/code.py Cypher templates.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.routes.code import trace_symbol


def _make_mock_driver(record: dict):
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=[record])

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


@pytest.mark.asyncio
async def test_trace_symbol_sends_valid_map_projection_cypher():
    """
    Regression for #19: trace_symbol's cypher is a plain string (not an f-string), so
    map projections must use single braces `{.*}`. Double braces `{{.*}}` are only valid
    inside f-strings (see get_call_hierarchy in the same file) and produce a
    neo4j.exceptions.CypherSyntaxError when sent literally.
    """
    record = {
        "target_props": {"id": "sym-1", "name": "do_thing", "domain_id": "default", "branch": "main"},
        "service_props": None,
        "specs": [],
        "apis": [],
        "adrs": [],
    }
    driver, mock_session = _make_mock_driver(record)

    from app.db import neo4j as neo4j_module
    old_driver = neo4j_module._driver
    neo4j_module._driver = driver
    try:
        response = await trace_symbol(symbol="do_thing", domain_id="default", branch="main", _token="")

        cypher = mock_session.run.call_args_list[0][0][0]
        assert "{{.*}}" not in cypher, "map projection must use single braces in a plain (non f-string) cypher template"
        assert "target {.*} AS target_props" in cypher

        assert response.symbol.id == "sym-1"
        assert response.symbol.name == "do_thing"
    finally:
        neo4j_module._driver = old_driver
