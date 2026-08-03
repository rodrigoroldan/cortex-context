"""
tests/test_domain_isolation.py — Tests for enterprise Multi-Tenancy & Domain Isolation.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from app.core.graph_builder import upsert_node, upsert_edge, upsert_chunk, ingest_nodes, ingest_edges, ingest_chunks
from app.core.parsers.base import NodeData, EdgeData
from app.core.parsers.manifest import IngestManifest, ManifestNode, ManifestIngestResponse
from app.db.neo4j import vector_search
from app.routes.dependencies import get_domain_id


def _make_driver():
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=MagicMock())
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


@pytest.mark.asyncio
async def test_upsert_node_stamps_domain_id():
    driver, mock_session = _make_driver()
    node = NodeData(node_labels=["Spec", "Intent"], node_id="spec-100", properties={"title": "Spec 100"})

    await upsert_node(driver, node, domain_id="financeiro")

    mock_session.run.assert_called_once()
    cypher = mock_session.run.call_args[0][0]
    kwargs = mock_session.run.call_args[1]

    assert "domain_id: $domain_id" in cypher
    assert kwargs.get("domain_id") == "financeiro"


@pytest.mark.asyncio
async def test_upsert_edge_stamps_domain_id():
    driver, mock_session = _make_driver()
    mock_result = AsyncMock()
    mock_result.single = AsyncMock(return_value={"edge_count": 1})
    mock_session.run = AsyncMock(return_value=mock_result)

    edge = EdgeData(from_id="spec-100", to_id="service-pay", relationship="AFFECTS", properties={})

    created = await upsert_edge(driver, edge, domain_id="cartoes")

    mock_session.run.assert_called_once()
    cypher = mock_session.run.call_args[0][0]
    kwargs = mock_session.run.call_args[1]

    assert "a.domain_id = $domain_id" in cypher
    assert "SET r.domain_id = $domain_id" in cypher
    assert kwargs.get("domain_id") == "cartoes"
    assert created is True


@pytest.mark.asyncio
async def test_upsert_edge_returns_false_when_nodes_not_found():
    """MERGE não roda quando o MATCH não casa nenhum nó — upsert_edge deve reportar
    isso como False em vez de mascarar como sucesso (issue #16)."""
    driver, mock_session = _make_driver()
    mock_result = AsyncMock()
    mock_result.single = AsyncMock(return_value={"edge_count": 0})
    mock_session.run = AsyncMock(return_value=mock_result)

    edge = EdgeData(from_id="spec-ghost", to_id="service-ghost", relationship="AFFECTS", properties={})

    created = await upsert_edge(driver, edge, domain_id="cartoes")

    assert created is False


@pytest.mark.asyncio
async def test_ingest_edges_reports_real_persisted_count():
    """ingest_edges deve retornar quantas arestas foram de fato persistidas, não
    quantas foram 'tentadas' — arestas cujo nó não foi encontrado não contam."""
    driver, mock_session = _make_driver()
    mock_result = AsyncMock()
    mock_result.data = AsyncMock(
        return_value=[
            {"from_id": "spec-1", "to_id": "service-1", "matched": True},
            {"from_id": "spec-2", "to_id": "service-missing", "matched": False},
        ]
    )
    mock_session.run = AsyncMock(return_value=mock_result)

    edges = [
        EdgeData(from_id="spec-1", to_id="service-1", relationship="AFFECTS", properties={}),
        EdgeData(from_id="spec-2", to_id="service-missing", relationship="AFFECTS", properties={}),
    ]
    count = await ingest_edges(driver, edges, domain_id="cartoes")

    assert count == 1


@pytest.mark.asyncio
async def test_upsert_chunk_stamps_domain_id():
    driver, mock_session = _make_driver()
    chunk = NodeData(
        node_labels=["DocumentChunk"],
        node_id="chunk-1",
        properties={"content": "payment spec content", "embedding": [0.1, 0.2]}
    )

    await upsert_chunk(driver, chunk, domain_id="emprestimos")

    mock_session.run.assert_called_once()
    cypher = mock_session.run.call_args[0][0]
    kwargs = mock_session.run.call_args[1]

    assert "domain_id: $domain_id" in cypher
    assert kwargs.get("domain_id") == "emprestimos"


@pytest.mark.asyncio
async def test_vector_search_filters_by_domain_id():
    driver, mock_session = _make_driver()
    mock_session.run.return_value.data = AsyncMock(return_value=[
        {"chunk_id": "c1", "parent_id": "p1", "content": "test", "pillar": "Intent", "domain_id": "cartoes", "score": 0.95}
    ])

    with pytest.helpers if hasattr(pytest, 'helpers') else MagicMock():
        from app.db import neo4j as neo4j_module
        old_driver = neo4j_module._driver
        neo4j_module._driver = driver
        try:
            results = await vector_search([0.1, 0.2], top_k=5, domain_id="cartoes")
            cypher = mock_session.run.call_args[0][0]
            kwargs = mock_session.run.call_args[1]

            assert "n.domain_id = $domain_id" in cypher
            assert kwargs.get("domain_id") == "cartoes"
            assert len(results) == 1
            assert results[0]["domain_id"] == "cartoes"
        finally:
            neo4j_module._driver = old_driver


def test_manifest_schema_domain_id():
    manifest = IngestManifest(
        source="git-diff",
        domain_id="cartoes",
        nodes=[
            ManifestNode(node_id="spec-1", node_labels=["Spec"], domain_id="cartoes"),
        ],
    )
    assert manifest.domain_id == "cartoes"
    assert manifest.nodes[0].domain_id == "cartoes"

    default_manifest = IngestManifest()
    assert default_manifest.domain_id == "default"


def test_get_domain_id_dependency_priority():
    # 1. Query param priority
    dom1 = get_domain_id(x_domain_id="hdr1", x_cortex_domain="hdr2", domain_id="query1")
    assert dom1 == "query1"

    # 2. X-Domain-ID header priority
    dom2 = get_domain_id(x_domain_id="hdr1", x_cortex_domain="hdr2", domain_id=None)
    assert dom2 == "hdr1"

    # 3. X-Cortex-Domain header fallback
    dom3 = get_domain_id(x_domain_id=None, x_cortex_domain="hdr2", domain_id=None)
    assert dom3 == "hdr2"

    # 4. Default fallback
    dom4 = get_domain_id(x_domain_id=None, x_cortex_domain=None, domain_id=None)
    assert dom4 == "default"


def test_get_branch_dependency_priority():
    from app.routes.dependencies import get_branch

    # 1. Query param priority
    b1 = get_branch(x_cortex_branch="c-hdr", x_branch_name="name-hdr", x_branch="b-hdr", branch="q-branch")
    assert b1 == "q-branch"

    # 2. X-Cortex-Branch header priority
    b2 = get_branch(x_cortex_branch="c-hdr", x_branch_name="name-hdr", x_branch="b-hdr", branch=None)
    assert b2 == "c-hdr"

    # 3. X-Branch-Name header priority
    b3 = get_branch(x_cortex_branch=None, x_branch_name="name-hdr", x_branch="b-hdr", branch=None)
    assert b3 == "name-hdr"

    # 4. X-Branch header fallback
    b4 = get_branch(x_cortex_branch=None, x_branch_name=None, x_branch="b-hdr", branch=None)
    assert b4 == "b-hdr"

    # 5. Default fallback 'main'
    b5 = get_branch(x_cortex_branch=None, x_branch_name=None, x_branch=None, branch=None)
    assert b5 == "main"


def test_nodedata_shadow_graph_properties():
    node_main = NodeData(node_labels=["Spec"], node_id="spec-100", properties={"branch": "main", "is_draft": False})
    assert node_main.canonical_id == "spec-100"
    assert node_main.is_draft is False
    assert node_main.branch == "main"

    node_draft = NodeData(
        node_labels=["Spec"],
        node_id="draft:feat/oauth:spec-100",
        properties={"branch": "feat/oauth", "is_draft": True, "canonical_id": "spec-100"},
    )
    assert node_draft.canonical_id == "spec-100"
    assert node_draft.is_draft is True
    assert node_draft.branch == "feat/oauth"
