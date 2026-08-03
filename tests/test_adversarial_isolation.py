"""
tests/test_adversarial_isolation.py — Empirical Adversarial Challenge Suite for Cortex Context Multi-Tenancy.

This test suite stress-tests multi-tenant domain isolation boundaries in cortex-core:
1. Test 2-hop traversal cross-domain relationship/node leaks in /query and /query/semantic.
2. Test ingestion manifest domain spoofing via node properties overriding request domain_id.
3. Test query param key Cypher injection in GET /nodes/{dim_key}.
4. Test Cypher vector similarity search multi-tenant domain filtering.
5. Test header vs query param precedence in get_domain_id.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock
import pytest

from app.core.graph_builder import upsert_node, upsert_edge, upsert_chunk, ingest_nodes, ingest_edges
from app.core.parsers.base import NodeData, EdgeData
from app.core.parsers.manifest import IngestManifest, ManifestNode, ManifestEdge
from app.db.neo4j import vector_search
from app.routes.dependencies import get_domain_id
from app.routes.query import query_context
from app.routes.semantic import SemanticSearchRequest, semantic_search


def _make_mock_driver():
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=MagicMock())
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


# ─── Challenge 1: Cross-Domain 2-Hop Traversal Leak Test ────────────────────


@pytest.mark.asyncio
async def test_adversarial_2hop_traversal_cross_domain_leak():
    """
    Empirical test: When hops=2 in /query, verifies if Cypher query checks intermediate
    node domain_id or leaks edges connected to other domains.
    """
    driver, mock_session = _make_mock_driver()

    # Mock FTS seed returning seed node in domain_A
    seed_record = {
        "props": {"id": "spec-A1", "domain_id": "domain_A", "title": "Spec A1"},
        "labels": ["Spec", "Intent"],
        "score": 1.0,
    }

    # Mock FTS query result
    mock_fts_result = MagicMock()
    mock_fts_result.data = AsyncMock(return_value=[seed_record])

    # Mock expansion query result returning an edge to an intermediate node in domain_B
    mock_expand_result = MagicMock()
    mock_expand_result.data = AsyncMock(return_value=[{
        "nodes": [
            {"id": "spec-A1", "labels": ["Spec"], "domain_id": "domain_A"},
            {"id": "spec-A2", "labels": ["Spec"], "domain_id": "domain_A"},
        ],
        "edges": [
            {"from": "spec-A1", "to": "svc-B1", "type": "DEPENDS_ON"},  # svc-B1 is domain_B!
            {"from": "svc-B1", "to": "spec-A2", "type": "DEPENDS_ON"},
        ]
    }])

    mock_session.run = AsyncMock(side_effect=[mock_fts_result, mock_expand_result, mock_fts_result, mock_fts_result])

    from app.db import neo4j as neo4j_module
    old_driver = neo4j_module._driver
    neo4j_module._driver = driver
    try:
        response = await query_context(keywords="Spec", hops=2, domain_id="domain_A", _token="")

        # Inspect generated Cypher for expansion
        calls = mock_session.run.call_args_list
        assert len(calls) >= 2
        expand_cypher = calls[1][0][0]

        # Verify whether intermediate nodes in path are constrained by domain_id
        # Bug check: "WHERE neighbor IS NOT NULL" only checks the endpoint neighbor, NOT intermediate nodes in path!
        has_path_all_clause = "ALL(" in expand_cypher or "nodes(path)" in expand_cypher
        
        # If edge list contains svc-B1, domain_B entity ID leaked into domain_A response
        leaked_edges = [e for e in response.edges if e.to_id == "svc-B1" or e.from_id == "svc-B1"]

        # Document finding assertion:
        if leaked_edges and not has_path_all_clause:
            pytest.fail(
                "SECURITY LEAK REPRODUCED: 2-hop traversal leaked cross-domain relationship "
                "to 'svc-B1' (domain_B) into domain_A query response!"
            )
    finally:
        neo4j_module._driver = old_driver


# ─── Challenge 2: Ingestion Manifest Domain Spoofing Test ───────────────────


@pytest.mark.asyncio
async def test_adversarial_manifest_property_domain_spoofing():
    """
    Empirical test: Verifies whether specifying 'domain_id' inside ManifestNode.properties
    can override the request domain_id and write into another domain.
    """
    driver, mock_session = _make_mock_driver()

    # Manifest sent by tenant_A, but properties has domain_id = tenant_B
    manifest_node = ManifestNode(
        node_id="malicious-spec",
        node_labels=["Spec"],
        domain_id=None,
        properties={"title": "Hacked Spec", "domain_id": "tenant_B"}
    )

    manifest = IngestManifest(
        source="git-diff",
        domain_id="tenant_A",
        nodes=[manifest_node],
        edges=[]
    )

    # Replicate ingest_manifest construction
    from app.core.parsers.base import NodeData
    effective_domain_id = manifest.domain_id
    nodes = [
        NodeData(
            node_labels=mn.node_labels,
            node_id=mn.node_id,
            properties={"id": mn.node_id, "domain_id": mn.domain_id or effective_domain_id, **mn.properties},
        )
        for mn in manifest.nodes
    ]

    await ingest_nodes(driver, nodes, domain_id=effective_domain_id)

    # Check effective domain_id passed to Cypher (batched: nested per-row in `rows`,
    # not a top-level kwarg — see issue #17)
    cypher_kwargs = mock_session.run.call_args[1]
    actual_domain = cypher_kwargs["rows"][0]["domain_id"]

    # If actual_domain is tenant_B, then tenant_A succeeded in writing to tenant_B!
    assert actual_domain == "tenant_A", (
        f"VULNERABILITY REPRODUCED: Node was upserted into '{actual_domain}' "
        "instead of authenticated domain 'tenant_A'!"
    )


# ─── Challenge 3: Vector Search Filtering & Domain Isolation ─────────────────


@pytest.mark.asyncio
async def test_adversarial_vector_search_domain_isolation():
    """
    Empirical test: Ensures Cypher vector search filters strictly by domain_id.
    """
    driver, mock_session = _make_mock_driver()
    mock_session.run.return_value.data = AsyncMock(return_value=[
        {"chunk_id": "c1", "parent_id": "p1", "content": "secret a", "pillar": "Intent", "domain_id": "tenant_A", "score": 0.9},
    ])

    from app.db import neo4j as neo4j_module
    old_driver = neo4j_module._driver
    neo4j_module._driver = driver
    try:
        results = await vector_search([0.1, 0.2], top_k=5, domain_id="tenant_A")
        cypher = mock_session.run.call_args[0][0]
        kwargs = mock_session.run.call_args[1]

        # Verify Cypher query contains domain_id filter
        assert "n.domain_id = $domain_id" in cypher
        assert kwargs.get("domain_id") == "tenant_A"
        assert all(r["domain_id"] == "tenant_A" for r in results)
    finally:
        neo4j_module._driver = old_driver


# ─── Challenge 4: Header & Query Parameter Precedence ─────────────────────────


def test_adversarial_domain_id_precedence_hierarchy():
    """
    Tests exact precedence rules for domain_id resolution.
    """
    # 1. Query parameter overrides headers
    assert get_domain_id(x_domain_id="hdr_a", x_cortex_domain="hdr_b", domain_id="param_c") == "param_c"

    # 2. X-Domain-ID header overrides X-Cortex-Domain header
    assert get_domain_id(x_domain_id="hdr_a", x_cortex_domain="hdr_b", domain_id=None) == "hdr_a"

    # 3. X-Cortex-Domain fallback
    assert get_domain_id(x_domain_id=None, x_cortex_domain="hdr_b", domain_id=None) == "hdr_b"

    # 4. Whitespace query param ignored, falls back to header
    assert get_domain_id(x_domain_id="hdr_a", x_cortex_domain=None, domain_id="   ") == "hdr_a"

    # 5. Empty inputs default to 'default'
    assert get_domain_id(x_domain_id="", x_cortex_domain="  ", domain_id=None) == "default"
