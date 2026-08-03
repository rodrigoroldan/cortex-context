"""
tests/test_graph_builder_batching.py — Cobertura para batching UNWIND (issue #17)
e tolerância a domain_id NULL em arestas (issue #16).
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.graph_builder import ingest_edges, ingest_nodes, upsert_edge
from app.core.parsers.base import EdgeData, NodeData


def _make_driver(data_return=None, single_return=None):
    mock_result = MagicMock()
    mock_result.data = AsyncMock(return_value=data_return if data_return is not None else [])
    mock_result.single = AsyncMock(return_value=single_return)

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


# ── ingest_nodes — batching por grupo de labels ───────────────────────────────


@pytest.mark.asyncio
async def test_ingest_nodes_same_label_group_uses_single_session_run():
    """N nós com a mesma combinação de labels devem virar 1 chamada UNWIND, não N."""
    driver, mock_session = _make_driver()
    nodes = [NodeData(node_labels=["CodeSymbol", "Implementation"], node_id=f"sym-{i}", properties={}) for i in range(50)]

    count = await ingest_nodes(driver, nodes)

    mock_session.run.assert_called_once()
    rows = mock_session.run.call_args[1]["rows"]
    assert len(rows) == 50
    assert count == 50


@pytest.mark.asyncio
async def test_ingest_nodes_different_label_groups_use_separate_batches():
    """Combinações de labels distintas (ex: CodeFile vs CodeSymbol) não podem
    compartilhar a mesma cláusula UNWIND (SET n:Label não é parametrizável)."""
    driver, mock_session = _make_driver()
    nodes = [
        NodeData(node_labels=["CodeFile", "Implementation"], node_id="file-1", properties={}),
        NodeData(node_labels=["CodeSymbol", "Implementation", "Function"], node_id="sym-1", properties={}),
    ]

    count = await ingest_nodes(driver, nodes)

    assert mock_session.run.call_count == 2
    assert count == 2


@pytest.mark.asyncio
async def test_ingest_nodes_respects_batch_size_chunking():
    """Um grupo maior que batch_size deve virar múltiplas transações UNWIND."""
    driver, mock_session = _make_driver()
    nodes = [NodeData(node_labels=["CodeSymbol"], node_id=f"sym-{i}", properties={}) for i in range(1250)]

    count = await ingest_nodes(driver, nodes, batch_size=500)

    assert mock_session.run.call_count == 3  # ceil(1250 / 500)
    assert count == 1250


@pytest.mark.asyncio
async def test_ingest_nodes_empty_list_short_circuits():
    driver = MagicMock()
    count = await ingest_nodes(driver, [])
    assert count == 0
    driver.session.assert_not_called()


# ── ingest_edges — batching por relationship type + tolerância a NULL ────────


@pytest.mark.asyncio
async def test_ingest_edges_same_relationship_uses_single_session_run():
    driver, mock_session = _make_driver(
        data_return=[{"from_id": f"n{i}", "to_id": f"n{i + 1}", "matched": True} for i in range(10)]
    )
    edges = [EdgeData(from_id=f"n{i}", to_id=f"n{i + 1}", relationship="CALLS", properties={}) for i in range(10)]

    count = await ingest_edges(driver, edges)

    mock_session.run.assert_called_once()
    assert count == 10


@pytest.mark.asyncio
async def test_ingest_edges_different_relationships_use_separate_batches():
    driver, mock_session = _make_driver(data_return=[{"from_id": "a", "to_id": "b", "matched": True}])
    edges = [
        EdgeData(from_id="a", to_id="b", relationship="CALLS", properties={}),
        EdgeData(from_id="a", to_id="c", relationship="IMPLEMENTS_SPEC", properties={}),
    ]

    await ingest_edges(driver, edges)

    assert mock_session.run.call_count == 2


@pytest.mark.asyncio
async def test_ingest_edges_counts_only_matched_rows_not_input_length():
    """3 arestas enviadas, só 2 casam nós existentes — count deve ser 2, não 3."""
    driver, mock_session = _make_driver(
        data_return=[
            {"from_id": "spec-1", "to_id": "sym-1", "matched": True},
            {"from_id": "spec-2", "to_id": "sym-2", "matched": True},
            {"from_id": "spec-3", "to_id": "sym-missing", "matched": False},
        ]
    )
    edges = [
        EdgeData(from_id="spec-1", to_id="sym-1", relationship="IMPLEMENTS_SPEC", properties={}),
        EdgeData(from_id="spec-2", to_id="sym-2", relationship="IMPLEMENTS_SPEC", properties={}),
        EdgeData(from_id="spec-3", to_id="sym-missing", relationship="IMPLEMENTS_SPEC", properties={}),
    ]

    count = await ingest_edges(driver, edges)

    assert count == 2


@pytest.mark.asyncio
async def test_ingest_edges_empty_list_short_circuits():
    driver = MagicMock()
    count = await ingest_edges(driver, [])
    assert count == 0
    driver.session.assert_not_called()


@pytest.mark.asyncio
async def test_ingest_edges_cypher_tolerates_null_domain_id():
    """O MATCH usado pelo batch precisa tratar domain_id NULL como equivalente a
    'default' — nós legados sem essa propriedade nunca devem impedir o match
    silenciosamente (issue #16)."""
    driver, mock_session = _make_driver(data_return=[{"from_id": "a", "to_id": "b", "matched": True}])
    edge = EdgeData(from_id="a", to_id="b", relationship="AFFECTS", properties={})

    await ingest_edges(driver, [edge], domain_id="default")

    cypher = mock_session.run.call_args[0][0]
    assert "IS NULL" in cypher
    assert "row.domain_id = 'default'" in cypher


# ── upsert_edge — retorno booleano fiel ao resultado real ────────────────────


@pytest.mark.asyncio
async def test_upsert_edge_cypher_tolerates_null_domain_id():
    driver, mock_session = _make_driver(single_return={"edge_count": 1})
    edge = EdgeData(from_id="a", to_id="b", relationship="AFFECTS", properties={})

    created = await upsert_edge(driver, edge, domain_id="default")

    cypher = mock_session.run.call_args[0][0]
    assert "IS NULL" in cypher
    assert created is True
