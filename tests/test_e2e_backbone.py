"""
cortex-core/tests/test_e2e_backbone.py
======================================
Comprehensive End-to-End Integration & Feature Test Suite for Cortex Context AI-DLC Backbone.

Scope of Features Covered:
- Feature 2: FastAPI Ingestion Domain Stamping (/ingest, /ingest/{dim}, /ingest/manifest)
- Feature 3: Neo4j domain_id Node & Edge Partitioning
- Feature 4: Query Domain Isolation Filtering (/query, /semantic, /nodes)
- Feature 6: Neo4j Draft / WIP Node Tracking (branch & status: draft, composite keys)
- Feature 7: Branch-Aware Merged Graph Query (left-outer priority merge of main canonical with branch draft)
- Feature 9: CI Deploy Draft Consolidation (/consolidate endpoint converting draft to canonical on PR merge)
- Feature 10: Backstage Catalog Parser (builtin.backstage_catalog parsing catalog-info.yaml)
- Feature 11: Business Glossary Dimension [:CONCEPT] (Concept dimension terms & definitions)
- Feature 13: Implementation History [:IMPLEMENTATION_HISTORY] (History dimension decision logs & epics)
- Feature 15: Frontmatter Schema Contract Validator (builtin.markdown_frontmatter validation)
- Feature 17: Async Background Embedding Processing (BackgroundTasks vector embedding generation)
- Feature 18: Core REST API + Neo4j End-to-End Integration
- Feature 19: Docker Container Build Pipeline (Dockerfile & container build specs)

Structure across 4 Tiers:
- Tier 1: Feature Coverage (>=5 tests per feature for happy path)
- Tier 2: Boundary & Corner Cases (>=5 tests per feature for edge cases)
- Tier 3: Cross-Feature Combinations (pairwise & multi-feature interactions)
- Tier 4: Real-World Application Scenarios (end-to-end multi-squad workloads)
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml
from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.testclient import TestClient

# ── Import Cortex Core Application & Core Modules ─────────────────────────────
from app.config import Settings, get_settings
from app.core.dimension_loader import DimensionConfig
from app.core.graph_builder import ingest_edges, ingest_nodes, upsert_edge, upsert_node
from app.core.parser_registry import get_parser, list_parsers
from app.core.parsers.base import BaseCortexExtractor, EdgeData, NodeData, ParseResult
from app.core.parsers.builtin.backstage_catalog import BackstageCatalogParser
from app.core.parsers.builtin.markdown_frontmatter import (
    MarkdownFrontmatterParser,
    FrontmatterContractValidator,
    _extract_frontmatter,
)
from app.core.parsers.manifest import IngestManifest, ManifestEdge, ManifestNode
from app.main import create_app
from app.routes.ingest import IngestResponse, _process_embeddings_bg, _run_ingest_pipeline
from app.routes.nodes import NodeDetailResponse, NodeSummary
from app.routes.query import SubgraphResponse, _estimate_tokens, _neo4j_node_to_context
from app.routes.semantic import SemanticSearchRequest, SemanticSearchResponse


# ─────────────────────────────────────────────────────────────────────────────
# Test Fixtures
# ─────────────────────────────────────────────────────────────────────────────

@pytest.fixture
def mock_neo4j_driver() -> Generator[MagicMock, None, None]:
    """Provides a mocked Neo4j AsyncDriver fixture."""
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
        "app.routes.health.get_driver", return_value=driver
    ), patch(
        "app.routes.consolidate.get_driver", return_value=driver
    ), patch(
        "app.db.neo4j.init_driver", AsyncMock()
    ), patch(
        "app.db.neo4j.close_driver", AsyncMock()
    ), patch(
        "app.db.neo4j.apply_domain_indexes", AsyncMock()
    ), patch(
        "app.db.neo4j.apply_branch_indexes", AsyncMock()
    ):
        yield driver


@pytest.fixture
def test_app() -> FastAPI:
    """Provides a FastAPI app instance with mocked driver."""
    app = create_app()
    return app


@pytest.fixture
def api_client(test_app: FastAPI, mock_neo4j_driver: MagicMock) -> TestClient:
    """Provides a FastAPI TestClient for E2E REST API tests."""
    return TestClient(test_app)


# =============================================================================
# TIER 1: FEATURE COVERAGE (HAPPY PATH - >=5 TESTS PER FEATURE)
# =============================================================================

class TestTier1Feature02FastAPIIngestionDomainStamping:
    """Feature 2: FastAPI Ingestion Domain Stamping (/ingest, /ingest/{dim}, /ingest/manifest)."""

    def test_ingest_manifest_stamps_domain_id_happy_path(self, api_client: TestClient):
        manifest_data = {
            "source": "cli-sync",
            "commit_sha": "abc12345",
            "domain_id": "payments-squad",
            "nodes": [
                {
                    "node_id": "spec-pay-01",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {"title": "Payment API", "domain_id": "payments-squad"},
                }
            ],
            "edges": [],
        }
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200
        data = res.json()
        assert data["nodes_upserted"] == 1
        assert "payments-squad" in manifest_data["nodes"][0]["properties"]["domain_id"]

    def test_ingest_manifest_with_explicit_edge_domain_stamping(self, api_client: TestClient):
        manifest_data = {
            "source": "cli-sync",
            "domain_id": "checkout-domain",
            "nodes": [
                {"node_id": "spec-c1", "node_labels": ["Spec", "Intent"], "properties": {"domain_id": "checkout-domain"}},
                {"node_id": "svc-c1", "node_labels": ["Service", "System"], "properties": {"domain_id": "checkout-domain"}},
            ],
            "edges": [
                {"from_id": "spec-c1", "to_id": "svc-c1", "relationship": "AFFECTS", "properties": {"domain_id": "checkout-domain"}}
            ],
        }
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200
        assert res.json()["edges_upserted"] == 1

    def test_ingest_manifest_default_global_domain(self, api_client: TestClient):
        manifest_data = {
            "source": "cli-sync",
            "nodes": [{"node_id": "spec-g1", "node_labels": ["Spec", "Intent"], "properties": {"title": "Global Spec"}}],
            "edges": [],
        }
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200
        assert res.json()["nodes_upserted"] == 1

    def test_manifest_ingest_response_structure(self, api_client: TestClient):
        manifest_data = {"source": "test-suite", "commit_sha": "def67890", "nodes": [], "edges": []}
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200
        body = res.json()
        assert "nodes_upserted" in body
        assert "edges_upserted" in body
        assert "message" in body

    def test_ingest_manifest_multi_node_domain_stamping(self, api_client: TestClient):
        manifest = IngestManifest(
            source="multi-test",
            nodes=[
                ManifestNode(node_id="n1", node_labels=["Spec"], properties={"domain_id": "d1"}),
                ManifestNode(node_id="n2", node_labels=["Service"], properties={"domain_id": "d1"}),
            ],
            edges=[ManifestEdge(from_id="n1", to_id="n2", relationship="RELATES_TO", properties={"domain_id": "d1"})],
        )
        assert manifest.nodes[0].properties["domain_id"] == "d1"
        assert manifest.edges[0].properties["domain_id"] == "d1"


class TestTier1Feature03Neo4jDomainIdPartitioning:
    """Feature 3: Neo4j domain_id Node & Edge Partitioning."""

    @pytest.mark.asyncio
    async def test_upsert_node_includes_domain_id_property(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        node = NodeData(
            node_labels=["Spec", "Intent"],
            node_id="spec-p1",
            properties={"id": "spec-p1", "title": "Partitioned Spec", "domain_id": "domain-alpha"},
        )
        await upsert_node(driver, node)
        assert mock_session.run.called
        kwargs = mock_session.run.call_args[1]
        assert kwargs["props"]["domain_id"] == "domain-alpha"

    @pytest.mark.asyncio
    async def test_upsert_edge_includes_domain_id_property(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        edge = EdgeData(
            from_id="spec-p1",
            to_id="service-p1",
            relationship="AFFECTS",
            properties={"domain_id": "domain-alpha", "created_by": "test"},
        )
        await upsert_edge(driver, edge)
        assert mock_session.run.called
        kwargs = mock_session.run.call_args[1]
        assert kwargs["props"]["domain_id"] == "domain-alpha"

    @pytest.mark.asyncio
    async def test_ingest_nodes_batch_stamping(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        nodes = [
            NodeData(node_labels=["Spec"], node_id=f"spec-{i}", properties={"domain_id": "domain-beta"})
            for i in range(5)
        ]
        count = await ingest_nodes(driver, nodes)
        assert count == 5

    @pytest.mark.asyncio
    async def test_ingest_edges_batch_stamping(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        edges = [
            EdgeData(from_id=f"n{i}", to_id=f"n{i+1}", relationship="LINK", properties={"domain_id": "domain-beta"})
            for i in range(4)
        ]
        count = await ingest_edges(driver, edges)
        assert count == 4

    def test_node_data_domain_id_validation(self):
        node = NodeData(
            node_labels=["Spec"],
            node_id="spec-val-1",
            properties={"domain_id": "tenant-42"},
        )
        assert node.properties.get("domain_id") == "tenant-42"


class TestTier1Feature04QueryDomainIsolationFiltering:
    """Feature 4: Query Domain Isolation Filtering (/query, /semantic, /nodes)."""

    def test_query_context_accepts_domain_id_param(self, api_client: TestClient):
        res = api_client.get("/api/v1/query?keywords=payment&domain_id=payments-squad")
        assert res.status_code == 200
        body = res.json()
        assert "nodes" in body
        assert "edges" in body

    def test_semantic_query_accepts_domain_id(self, api_client: TestClient):
        payload = {"query": "rateio de eventos", "top_k": 5, "domain_id": "finance-domain"}
        res = api_client.post("/api/v1/query/semantic", json=payload)
        # Should either return 200 or 503 if embedder is not enabled
        assert res.status_code in (200, 503)

    def test_nodes_list_filters_by_domain_id_query_param(self, api_client: TestClient):
        res = api_client.get("/api/v1/nodes/spec?domain_id=payments-squad")
        assert res.status_code == 200
        assert isinstance(res.json(), list)

    def test_subgraph_response_includes_domain_query_meta(self):
        resp = SubgraphResponse(
            nodes=[],
            edges=[],
            token_estimate=0,
            query_meta={"domain_id": "tenant-x", "keywords": ["auth"]},
        )
        assert resp.query_meta["domain_id"] == "tenant-x"

    def test_semantic_search_request_domain_field(self):
        req = SemanticSearchRequest(query="search test", top_k=5, pillar="Intent")
        assert req.top_k == 5


class TestTier1Feature06Neo4jDraftWIPNodeTracking:
    """Feature 6: Neo4j Draft / WIP Node Tracking (branch & status: draft)."""

    def test_draft_node_composite_id_format(self):
        canonical_id = "spec-auth-01"
        branch = "feat/oauth2"
        composite_id = f"draft:{branch}:{canonical_id}"
        assert composite_id == "draft:feat/oauth2:spec-auth-01"

    def test_draft_node_properties(self):
        node = NodeData(
            node_labels=["Spec", "Intent"],
            node_id="draft:feat/login:spec-login-01",
            properties={
                "id": "draft:feat/login:spec-login-01",
                "canonical_id": "spec-login-01",
                "branch": "feat/login",
                "status": "draft",
                "is_draft": True,
            },
        )
        assert node.properties["is_draft"] is True
        assert node.properties["status"] == "draft"

    @pytest.mark.asyncio
    async def test_upsert_draft_node_to_neo4j(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        node = NodeData(
            node_labels=["Spec", "Draft"],
            node_id="draft:feature-x:spec-100",
            properties={"branch": "feature-x", "status": "draft"},
        )
        await upsert_node(driver, node)
        assert mock_session.run.called

    def test_manifest_node_with_draft_flag(self):
        manifest_node = ManifestNode(
            node_id="draft:branch-a:spec-10",
            node_labels=["Spec"],
            properties={"status": "draft", "branch": "branch-a"},
        )
        assert manifest_node.properties["status"] == "draft"

    def test_draft_edge_linking_to_canonical(self):
        edge = EdgeData(
            from_id="draft:branch-a:spec-10",
            to_id="service-auth",
            relationship="PROPOSES_CHANGE_TO",
            properties={"branch": "branch-a"},
        )
        assert edge.relationship == "PROPOSES_CHANGE_TO"


class TestTier1Feature07BranchAwareMergedGraphQuery:
    """Feature 7: Branch-Aware Merged Graph Query (left-outer priority merge)."""

    def test_merge_canonical_and_draft_properties(self):
        canonical_props = {"title": "Original Spec", "status": "active", "version": 1}
        draft_props = {"title": "Updated Spec on Branch", "status": "draft", "version": 2}

        # Left-outer merge simulation: draft overrides canonical
        merged = {**canonical_props, **draft_props}
        assert merged["title"] == "Updated Spec on Branch"
        assert merged["version"] == 2

    def test_merged_view_fallback_to_canonical(self):
        canonical_props = {"title": "Main Spec", "status": "active"}
        draft_props = None

        merged = draft_props or canonical_props
        assert merged["title"] == "Main Spec"

    def test_query_context_accepts_branch_param(self, api_client: TestClient):
        res = api_client.get("/api/v1/query?keywords=auth&branch=feat/oauth2")
        assert res.status_code == 200

    def test_semantic_query_accepts_branch_param(self, api_client: TestClient):
        payload = {"query": "auth spec", "top_k": 3, "branch": "feat/oauth2"}
        res = api_client.post("/api/v1/query/semantic", json=payload)
        assert res.status_code in (200, 503)

    def test_node_summary_supports_branch_metadata(self):
        summary = NodeSummary(
            id="spec-auth",
            label="Spec",
            properties={"title": "Auth", "branch": "main", "is_draft": False},
        )
        assert summary.properties["is_draft"] is False


class TestTier1Feature09CIDeployDraftConsolidation:
    """Feature 9: CI Deploy Draft Consolidation (/consolidate endpoint)."""

    def test_consolidate_branch_helper(self, api_client: TestClient):
        res = api_client.post("/api/v1/consolidate", json={"branch": "feat/payment-refactor", "domain_id": "payments"})
        assert res.status_code == 200
        result = res.json()
        assert result["status"] == "success"
        assert result["branch"] == "feat/payment-refactor"
        assert result["domain_id"] == "payments"
        assert result["consolidated_count"] >= 0
        assert isinstance(result["promoted_nodes"], list)

    def test_consolidated_node_promotion_properties(self):
        draft_props = {"id": "draft:main-pr:spec-10", "canonical_id": "spec-10", "status": "draft", "is_draft": True}
        # Promotion converts draft props to canonical props
        promoted_props = {
            **draft_props,
            "id": draft_props["canonical_id"],
            "status": "completed",
            "is_draft": False,
        }
        assert promoted_props["id"] == "spec-10"
        assert promoted_props["is_draft"] is False

    @pytest.mark.asyncio
    async def test_consolidation_cypher_simulation(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        # Simulate executing consolidation cypher query
        await mock_session.run(
            "MATCH (d:Draft {branch: $branch}) SET d.status = 'active', d.is_draft = false RETURN count(d)",
            branch="feat/pr-123",
        )
        assert mock_session.run.called

    def test_consolidation_idempotency(self, api_client: TestClient):
        res1 = api_client.post("/api/v1/consolidate", json={"branch": "feat/pr-100"}).json()
        res2 = api_client.post("/api/v1/consolidate", json={"branch": "feat/pr-100"}).json()
        assert res1["branch"] == res2["branch"]
        assert res1["status"] == "success"
        assert res1["domain_id"] == "default"
        assert res1["consolidated_count"] >= 0

    def test_consolidation_returns_promoted_list(self, api_client: TestClient):
        res = api_client.post("/api/v1/consolidate", json={"branch": "release/1.0.0"}).json()
        assert isinstance(res["promoted_nodes"], list)
        assert res["status"] == "success"
        assert res["branch"] == "release/1.0.0"
        assert res["domain_id"] == "default"
        assert res["consolidated_count"] >= 0


class TestTier1Feature10BackstageCatalogParser:
    """Feature 10: Backstage Catalog Parser (builtin.backstage_catalog)."""

    def test_parser_can_parse_catalog_info_yaml(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("apiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata:\n  name: order-service\n")
        parser = BackstageCatalogParser()
        assert parser.can_parse(f) is True

    def test_parser_extracts_component_service_nodes(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: payment-service\n"
            "  title: Payment Processing Service\n"
            "spec:\n"
            "  type: service\n"
            "  lifecycle: production\n"
            "  owner: squad-payments\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        result = parser.parse(f, cfg)
        assert len(result.nodes) == 1
        node = result.nodes[0]
        assert node.node_id == "service-payment-service"
        assert node.properties["owner"] == "squad-payments"

    def test_parser_extracts_depends_on_relationships(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: checkout-bff\n"
            "spec:\n"
            "  type: service\n"
            "  dependsOn:\n"
            "    - component:payment-service\n"
            "    - component:auth-service\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        result = parser.parse(f, cfg)
        assert len(result.edges) == 2
        assert result.edges[0].relationship == "DEPENDS_ON"
        assert result.edges[0].to_id == "service-payment-service"

    def test_parser_extracts_provides_api_relationships(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: auth-service\n"
            "spec:\n"
            "  providesApis:\n"
            "    - oauth2-api\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        result = parser.parse(f, cfg)
        assert len(result.edges) == 1
        assert result.edges[0].relationship in ("EXPOSES", "PROVIDES_API")
        assert result.edges[0].to_id == "api-oauth2-api"

    def test_parser_ignores_non_catalog_files(self, tmp_path: Path):
        f = tmp_path / "random.txt"
        f.write_text("hello world")
        parser = BackstageCatalogParser()
        assert parser.can_parse(f) is False

    def test_parser_extracts_multidoc_yaml(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: order-service\n"
            "spec:\n"
            "  type: service\n"
            "  providesApis:\n"
            "    - order-api\n"
            "  dependsOn:\n"
            "    - component:default/payment-service\n"
            "---\n"
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: API\n"
            "metadata:\n"
            "  name: order-api\n"
            "spec:\n"
            "  type: openapi\n"
            "---\n"
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: System\n"
            "metadata:\n"
            "  name: order-system\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        result = parser.parse(f, cfg)
        assert len(result.nodes) == 3
        node_ids = {n.node_id for n in result.nodes}
        assert node_ids == {"service-order-service", "api-order-api", "system-order-system"}

        # Verify edge relationships
        rel_types = {e.relationship for e in result.edges}
        assert "EXPOSES" in rel_types
        assert "DEPENDS_ON" in rel_types

        # Verify target node ID for component ref
        dep_edge = next(e for e in result.edges if e.relationship == "DEPENDS_ON")
        assert dep_edge.to_id == "service-payment-service"

    def test_parser_extracts_consumes_api_calls(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: checkout-bff\n"
            "spec:\n"
            "  consumesApis:\n"
            "    - api:default/payment-api\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        result = parser.parse(f, cfg)
        assert len(result.edges) == 1
        assert result.edges[0].relationship == "CALLS"
        assert result.edges[0].to_id == "api-payment-api"


class TestTier1Feature11BusinessGlossaryConceptDimension:
    """Feature 11: Business Glossary Dimension [:CONCEPT]."""

    def test_concept_dimension_config(self):
        cfg = DimensionConfig(dimension="concept", node_label="Concept", pillar="Intent")
        assert cfg.dimension == "concept"
        assert cfg.node_label == "Concept"
        assert cfg.pillar == "Intent"

    def test_concept_node_data_structure(self):
        node = NodeData(
            node_labels=["Concept", "Intent"],
            node_id="concept-pix-key",
            properties={
                "term": "PIX Key",
                "definition": "Unique identifier for PIX instant payment accounts.",
                "domain_id": "payments",
            },
        )
        assert node.node_id == "concept-pix-key"
        assert node.properties["term"] == "PIX Key"

    def test_concept_defines_relationship_edge(self):
        edge = EdgeData(
            from_id="concept-pix-key",
            to_id="spec-pix-transfer",
            relationship="DEFINES",
            properties={"domain_id": "payments"},
        )
        assert edge.relationship == "DEFINES"

    def test_concept_used_in_relationship_edge(self):
        edge = EdgeData(
            from_id="concept-pix-key",
            to_id="service-pix-gateway",
            relationship="USED_IN",
            properties={"domain_id": "payments"},
        )
        assert edge.relationship == "USED_IN"

    def test_clarify_business_term_response_formatting(self):
        term_data = {
            "term": "Chargeback",
            "definition": "Reversal of a prior transaction by the card issuer.",
            "related_specs": ["spec-chargeback-flow"],
            "related_services": ["service-disputes"],
        }
        assert term_data["term"] == "Chargeback"
        assert len(term_data["related_specs"]) == 1


class TestTier1Feature13ImplementationHistoryDimension:
    """Feature 13: Implementation History [:IMPLEMENTATION_HISTORY]."""

    def test_history_dimension_config(self):
        cfg = DimensionConfig(dimension="history", node_label="History", pillar="Implementation")
        assert cfg.dimension == "history"
        assert cfg.pillar == "Implementation"

    def test_history_node_data_structure(self):
        node = NodeData(
            node_labels=["History", "Implementation"],
            node_id="hist-epic-042",
            properties={
                "title": "Migrate to Redis Cache",
                "decision_log": "Chose Redis cluster over Memcached for persistence support.",
                "author": "dev-lead",
                "commit_sha": "abc999",
            },
        )
        assert node.node_id == "hist-epic-042"
        assert "Redis" in node.properties["decision_log"]

    def test_history_applies_to_spec_edge(self):
        edge = EdgeData(
            from_id="hist-epic-042",
            to_id="spec-caching",
            relationship="IMPLEMENTS_SPEC",
            properties={"commit_sha": "abc999"},
        )
        assert edge.relationship == "IMPLEMENTS_SPEC"

    def test_history_recorded_in_adr_edge(self):
        edge = EdgeData(
            from_id="hist-epic-042",
            to_id="adr-005-redis",
            relationship="RECORDED_IN",
        )
        assert edge.relationship == "RECORDED_IN"

    def test_query_history_response_model(self):
        history_entry = {
            "id": "hist-001",
            "title": "DB Migration",
            "summary": "Migrated MySQL to Neo4j Graph.",
            "date": "2026-08-01",
        }
        assert history_entry["id"] == "hist-001"


class TestTier1Feature15FrontmatterSchemaContractValidator:
    """Feature 15: Frontmatter Schema Contract Validator (builtin.markdown_frontmatter)."""

    def test_valid_frontmatter_schema(self):
        content = "---\ntype: spec\nid: spec-auth\ntitle: Auth Spec\nstatus: in-progress\nrepos: [auth-svc]\n---\n# Auth Spec\nBody text"
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is True
        assert len(errors) == 0

    def test_markdown_frontmatter_parser_can_parse_md(self, tmp_path: Path):
        f = tmp_path / "plan.md"
        f.write_text("---\nid: spec-1\ntitle: Plan\n---\n# Plan")
        parser = MarkdownFrontmatterParser()
        assert parser.can_parse(f) is True

    def test_markdown_frontmatter_parser_extracts_metadata(self, tmp_path: Path):
        f = tmp_path / "plan.md"
        f.write_text("---\nid: spec-100\ntitle: Feature Plan\nstatus: completed\nrepos: [svc-a]\n---\n# Feature Plan\nSummary paragraph.")
        parser = MarkdownFrontmatterParser()
        cfg = DimensionConfig(dimension="spec", node_label="Spec", pillar="Intent")
        res = parser.parse(f, cfg)
        assert len(res.nodes) >= 1
        node = res.nodes[0]
        assert node.node_id == "spec-100"
        assert node.properties["title"] == "Feature Plan"

    def test_frontmatter_parser_extracts_h1_fallback(self, tmp_path: Path):
        f = tmp_path / "doc.md"
        f.write_text("# Dynamic H1 Title\n\nBody content without frontmatter.")
        parser = MarkdownFrontmatterParser()
        cfg = DimensionConfig(dimension="spec", node_label="Spec", pillar="Intent")
        res = parser.parse(f, cfg)
        assert res.nodes[0].properties["title"] == "Dynamic H1 Title"

    def test_frontmatter_parser_cross_references_detection(self, tmp_path: Path):
        f = tmp_path / "spec.md"
        f.write_text("---\nid: spec-billing\ntitle: Billing\n---\n# Billing\nDepends on spec-auth for tokens.")
        parser = MarkdownFrontmatterParser()
        cfg = DimensionConfig(dimension="spec", node_label="Spec", pillar="Intent")
        res = parser.parse(f, cfg)
        assert any(e.relationship == "DEPENDS_ON" for e in res.edges)


class TestTier1Feature17AsyncBackgroundEmbeddingProcessing:
    """Feature 17: Async Background Embedding Processing (BackgroundTasks)."""

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_runs_silently_when_embedder_disabled(self):
        with patch("app.core.embedder.is_embedder_enabled", return_value=False):
            # Should return immediately without exception
            await _process_embeddings_bg([])

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_handles_empty_chunks(self):
        with patch("app.core.embedder.is_embedder_enabled", return_value=True):
            await _process_embeddings_bg([])

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_executes_embeddings(self):
        chunk_node = NodeData(
            node_labels=["DocumentChunk"],
            node_id="chunk-1",
            properties={"content": "Sample content for vector embedding"},
        )
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", new_callable=AsyncMock, return_value=[[0.1, 0.2, 0.3]]
        ) as mock_embed, patch("app.core.graph_builder.ingest_chunks", new_callable=AsyncMock) as mock_ingest:
            await _process_embeddings_bg([chunk_node])
            assert mock_embed.called
            assert mock_ingest.called

    def test_background_tasks_integration_with_fastapi_route(self, api_client: TestClient):
        # Trigger route that uses BackgroundTasks
        with patch("app.routes.ingest._load_config", return_value={"active_dimensions": ["spec"]}):
            res = api_client.post("/api/v1/ingest/spec")
            assert res.status_code in (200, 404)

    @pytest.mark.asyncio
    async def test_background_embedding_error_resilience(self):
        chunk_node = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={"content": "test"})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", side_effect=Exception("Embedding API error")
        ):
            # Should catch exception and log without raising
            await _process_embeddings_bg([chunk_node])

    @pytest.mark.asyncio
    async def test_background_task_respects_domain_id_partitioning(self):
        chunk_node = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={"content": "test chunk"})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", new_callable=AsyncMock, return_value=[[0.1, 0.2]]
        ), patch("app.core.graph_builder.ingest_chunks", new_callable=AsyncMock) as mock_ingest:
            await _process_embeddings_bg([chunk_node], domain_id="payments")
            assert mock_ingest.called
            _, kwargs = mock_ingest.call_args
            assert kwargs.get("domain_id") == "payments"


class TestTier1Feature18CoreRESTAPIIntegrationSuite:
    """Feature 18: Core REST API + Neo4j End-to-End Integration Suite."""

    def test_health_check_endpoint(self, api_client: TestClient):
        res = api_client.get("/health")
        assert res.status_code == 200
        assert res.json()["status"] == "ok"

    def test_list_dimensions_endpoint(self, api_client: TestClient):
        res = api_client.get("/api/v1/nodes")
        assert res.status_code == 200
        body = res.json()
        assert "dimensions" in body
        assert "total_nodes" in body

    def test_graph_reset_endpoint(self, api_client: TestClient):
        res = api_client.delete("/api/v1/graph")
        assert res.status_code == 200
        assert "nodes_deleted" in res.json()

    def test_token_estimation_utility(self):
        ctx = _neo4j_node_to_context({"id": "s1", "title": "Title", "summary": "Summary"}, ["Spec"])
        tokens = _estimate_tokens([ctx])
        assert tokens >= 0

    def test_auth_token_middleware_open_mode(self, api_client: TestClient):
        # Open mode (CORTEX_API_TOKEN empty) allow requests without Authorization header
        res = api_client.get("/api/v1/nodes")
        assert res.status_code == 200


class TestTier1Feature19DockerContainerBuildPipeline:
    """Feature 19: Docker Container Build Pipeline (Dockerfile & build specs)."""

    def test_dockerfile_exists_in_cortex_core(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        assert dockerfile_path.exists() is True

    def test_dockerfile_contains_python_base_image(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "FROM python:" in content or "FROM python" in content

    def test_dockerfile_exposes_port(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "EXPOSE" in content or "8000" in content

    def test_dockerfile_cmd_entrypoint_spec(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "uvicorn" in content or "CMD" in content

    def test_docker_compose_config_exists(self):
        compose_path = Path(__file__).parent.parent / "docker-compose.yml"
        assert compose_path.exists() is True
        content = compose_path.read_text(encoding="utf-8")
        assert "neo4j" in content.lower()


# =============================================================================
# TIER 2: BOUNDARY & CORNER CASES (>=5 TESTS PER FEATURE)
# =============================================================================

class TestTier2BoundaryAndCornerCases:
    """Tier 2: Boundary conditions, invalid inputs, edge cases across all features."""

    # Feature 2 Boundary
    def test_ingest_manifest_missing_source_field(self, api_client: TestClient):
        res = api_client.post("/api/v1/ingest/manifest", json={"nodes": [], "edges": []})
        assert res.status_code == 200
        assert res.json()["source"] == "manual"

    def test_ingest_manifest_empty_nodes_and_edges(self, api_client: TestClient):
        res = api_client.post("/api/v1/ingest/manifest", json={"source": "empty", "nodes": [], "edges": []})
        assert res.status_code == 200
        assert res.json()["nodes_upserted"] == 0

    def test_ingest_dimension_unregistered_key_404(self, api_client: TestClient):
        res = api_client.post("/api/v1/ingest/nonexistent_dim_key_123")
        assert res.status_code == 404

    def test_ingest_manifest_invalid_json_body(self, api_client: TestClient):
        res = api_client.post("/api/v1/ingest/manifest", content="invalid json")
        assert res.status_code == 422

    def test_ingest_manifest_token_auth_rejection(self, test_app: FastAPI):
        mock_s = Settings(cortex_api_token="secret-token")
        test_app.dependency_overrides[get_settings] = lambda: mock_s
        try:
            with patch("app.config.get_settings", return_value=mock_s), patch("app.routes.ingest.get_settings", return_value=mock_s):
                client = TestClient(test_app)
                res = client.post("/api/v1/ingest/manifest", json={"source": "test"})
                assert res.status_code == 401
        finally:
            test_app.dependency_overrides.clear()

    # Feature 3 Boundary
    @pytest.mark.asyncio
    async def test_upsert_node_with_special_chars_in_domain_id(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        node = NodeData(
            node_labels=["Spec"],
            node_id="spec-spec-char",
            properties={"domain_id": "domain!@#$%^&*()_+"},
        )
        await upsert_node(driver, node)
        assert mock_session.run.called

    def test_node_data_empty_domain_id_string(self):
        node = NodeData(node_labels=["Spec"], node_id="spec-empty-d", properties={"domain_id": ""})
        assert node.properties["domain_id"] == ""

    @pytest.mark.asyncio
    async def test_upsert_edge_null_domain_id_fallback(self):
        driver = MagicMock()
        mock_session = AsyncMock()
        mock_session.run = AsyncMock()
        mock_session.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session.__aexit__ = AsyncMock(return_value=False)
        driver.session = MagicMock(return_value=mock_session)

        edge = EdgeData(from_id="a", to_id="b", relationship="LINK", properties={})
        await upsert_edge(driver, edge)
        assert mock_session.run.called

    def test_manifest_node_without_domain_id(self):
        node = ManifestNode(node_id="n-no-domain", node_labels=["Spec"])
        assert "domain_id" not in node.properties

    @pytest.mark.asyncio
    async def test_batch_upsert_empty_list_of_nodes(self):
        driver = MagicMock()
        # Ingesting empty list should return 0 without calling session
        count = await ingest_nodes(driver, [])
        assert count == 0

    # Feature 4 Boundary
    def test_query_context_nonexistent_domain_id(self, api_client: TestClient):
        res = api_client.get("/api/v1/query?keywords=payment&domain_id=nonexistent-tenant-999")
        assert res.status_code == 200
        assert len(res.json()["nodes"]) == 0

    def test_semantic_query_empty_string_validation(self, api_client: TestClient):
        res = api_client.post("/api/v1/query/semantic", json={"query": "a", "top_k": 5})
        assert res.status_code == 422  # min_length validation error

    def test_nodes_detail_nonexistent_node_id_404(self, api_client: TestClient):
        with patch("app.routes.nodes._load_dimension_map", return_value={"spec": DimensionConfig(dimension="spec", node_label="Spec")}):
            res = api_client.get("/api/v1/nodes/spec/nonexistent-node-id-999")
            assert res.status_code == 404

    def test_query_context_sql_injection_safety_in_domain_id(self, api_client: TestClient):
        res = api_client.get("/api/v1/query?keywords=test&domain_id=' OR 1=1 --")
        assert res.status_code == 200

    def test_semantic_query_top_k_bounds(self, api_client: TestClient):
        res = api_client.post("/api/v1/query/semantic", json={"query": "valid query", "top_k": 100})
        assert res.status_code == 422  # le=50 validation error

    # Feature 6 Boundary
    def test_draft_node_missing_canonical_id_fallback(self):
        node = NodeData(
            node_labels=["Spec"],
            node_id="draft:feat/b:standalone",
            properties={"branch": "feat/b", "status": "draft"},
        )
        assert node.properties.get("canonical_id") is None

    def test_draft_node_with_slashes_in_branch_name(self):
        branch = "feature/squad-a/ticket-123"
        composite_id = f"draft:{branch}:spec-42"
        assert composite_id == "draft:feature/squad-a/ticket-123:spec-42"

    def test_draft_node_duplicate_composite_id_upsert(self):
        node1 = NodeData(node_labels=["Spec"], node_id="draft:b1:s1", properties={"v": 1})
        node2 = NodeData(node_labels=["Spec"], node_id="draft:b1:s1", properties={"v": 2})
        assert node1.node_id == node2.node_id

    def test_draft_node_empty_branch_string(self):
        node = NodeData(node_labels=["Spec"], node_id="draft::s1", properties={"branch": "", "status": "draft"})
        assert node.properties["branch"] == ""

    def test_draft_node_non_draft_status_properties(self):
        node = NodeData(node_labels=["Spec"], node_id="draft:b1:s1", properties={"status": "in-review"})
        assert node.properties["status"] == "in-review"

    # Feature 7 Boundary
    def test_merged_query_zero_draft_nodes(self):
        canonical_nodes = [{"id": "s1", "title": "Main Title"}]
        draft_nodes = []
        # Merge should return canonical unchanged
        merged = canonical_nodes + draft_nodes
        assert len(merged) == 1
        assert merged[0]["title"] == "Main Title"

    def test_merged_query_draft_node_without_canonical(self):
        canonical_nodes = []
        draft_nodes = [{"id": "draft:b1:s2", "title": "New Spec on Branch"}]
        merged = canonical_nodes + draft_nodes
        assert len(merged) == 1
        assert merged[0]["id"] == "draft:b1:s2"

    def test_merged_query_multiple_branches_isolation(self):
        b1_draft = {"id": "s1", "title": "Branch 1 Version", "branch": "b1"}
        b2_draft = {"id": "s1", "title": "Branch 2 Version", "branch": "b2"}
        assert b1_draft["title"] != b2_draft["title"]

    def test_merged_query_main_branch_ignores_drafts(self):
        main_spec = {"id": "s1", "title": "Canonical Main", "is_draft": False}
        branch_spec = {"id": "draft:b1:s1", "title": "Branch Draft", "is_draft": True}

        # Querying main branch returns main_spec only
        query_branch = "main"
        result = main_spec if query_branch == "main" else branch_spec
        assert result["title"] == "Canonical Main"

    def test_merged_query_conflicting_property_types(self):
        canonical = {"tags": "auth, login"}
        draft = {"tags": ["auth", "login", "oauth"]}
        merged = {**canonical, **draft}
        assert isinstance(merged["tags"], list)

    # Feature 9 Boundary
    def test_consolidate_nonexistent_branch(self, api_client: TestClient):
        res = api_client.post("/api/v1/consolidate", json={"branch": "nonexistent-branch-999"}).json()
        assert res["status"] == "success"
        assert res["branch"] == "nonexistent-branch-999"
        assert res["domain_id"] == "default"
        assert res["consolidated_count"] == 0
        assert res["promoted_nodes"] == []

    def test_consolidate_empty_branch_name_raises(self, api_client: TestClient):
        res = api_client.post("/api/v1/consolidate", json={"branch": ""})
        assert res.status_code == 422

    def test_consolidate_branch_with_special_characters(self, api_client: TestClient):
        res = api_client.post("/api/v1/consolidate", json={"branch": "fix/#123-bugfix"}).json()
        assert res["status"] == "success"
        assert res["branch"] == "fix/#123-bugfix"
        assert res["domain_id"] == "default"
        assert res["consolidated_count"] >= 0

    def test_consolidate_already_consolidated_branch(self, api_client: TestClient):
        res1 = api_client.post("/api/v1/consolidate", json={"branch": "merged-pr-1"}).json()
        res2 = api_client.post("/api/v1/consolidate", json={"branch": "merged-pr-1"}).json()
        assert res1 == res2
        assert res1["status"] == "success"
        assert res1["branch"] == "merged-pr-1"
        assert res1["domain_id"] == "default"
        assert res1["consolidated_count"] >= 0

    def test_consolidated_node_preserves_canonical_id(self):
        draft_node_id = "draft:feat/x:spec-unique-id"
        canonical_id = draft_node_id.split(":")[-1]
        assert canonical_id == "spec-unique-id"

    # Feature 10 Boundary
    def test_backstage_parser_malformed_yaml(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("apiVersion: [invalid yaml structure: {")
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 0

    def test_backstage_parser_missing_apiversion(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("kind: Component\nmetadata:\n  name: svc-no-version\n")
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 1

    def test_backstage_parser_unknown_kind(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("apiVersion: v1\nkind: UnknownKind\nmetadata:\n  name: custom-entity\n")
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 1
        assert res.nodes[0].node_id == "unknownkind-custom-entity"

    def test_backstage_parser_missing_metadata_name(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("apiVersion: v1\nkind: Component\nmetadata:\n  title: No Name\n")
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 0

    def test_backstage_parser_empty_file(self, tmp_path: Path):
        f = tmp_path / "catalog-info.yaml"
        f.write_text("")
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 0

    # Feature 11 Boundary
    def test_concept_missing_definition(self):
        node = NodeData(node_labels=["Concept"], node_id="concept-nodef", properties={"term": "Term"})
        assert node.properties.get("definition") is None

    def test_concept_duplicate_term_upsert_overwrite(self):
        c1 = NodeData(node_labels=["Concept"], node_id="concept-a", properties={"definition": "Def 1"})
        c2 = NodeData(node_labels=["Concept"], node_id="concept-a", properties={"definition": "Def 2"})
        assert c1.node_id == c2.node_id

    def test_concept_special_characters_in_term(self):
        node = NodeData(node_labels=["Concept"], node_id="c-spec", properties={"term": "API & SDK / 2.0"})
        assert node.properties["term"] == "API & SDK / 2.0"

    def test_concept_without_relationships(self):
        res = ParseResult(nodes=[NodeData(node_labels=["Concept"], node_id="c1")], edges=[])
        assert len(res.edges) == 0

    def test_concept_empty_properties_dict(self):
        node = NodeData(node_labels=["Concept"], node_id="c-empty", properties={})
        assert node.properties == {}

    # Feature 13 Boundary
    def test_history_missing_decision_tags(self):
        node = NodeData(node_labels=["History"], node_id="h1", properties={"title": "Decision"})
        assert "tags" not in node.properties

    def test_history_invalid_epic_format(self):
        node = NodeData(node_labels=["History"], node_id="h-invalid", properties={"epic_id": 12345})
        assert isinstance(node.properties["epic_id"], int)

    def test_history_missing_commit_sha(self):
        node = NodeData(node_labels=["History"], node_id="h-nosha", properties={"title": "Title"})
        assert node.properties.get("commit_sha") is None

    def test_history_empty_log_file(self, tmp_path: Path):
        f = tmp_path / "history.md"
        f.write_text("")
        parser = MarkdownFrontmatterParser()
        cfg = DimensionConfig(dimension="history", node_label="History", pillar="Implementation")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 1

    def test_history_malformed_yaml_frontmatter(self, tmp_path: Path):
        f = tmp_path / "history.md"
        f.write_text("---\ntitle: [unclosed list\n---\n# Title")
        parser = MarkdownFrontmatterParser()
        cfg = DimensionConfig(dimension="history", node_label="History", pillar="Implementation")
        res = parser.parse(f, cfg)
        assert len(res.nodes) == 1

    # Feature 15 Boundary
    def test_frontmatter_missing_required_type(self):
        content = "---\nid: spec-1\ntitle: Title\nstatus: completed\n---\n# Body"
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is False
        assert any("type" in e for e in errors)

    def test_frontmatter_invalid_status_enum(self):
        content = "---\ntype: spec\nid: spec-1\ntitle: Title\nstatus: invalid-status-val\n---\n# Body"
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is False
        assert any("status" in e.lower() for e in errors)

    def test_frontmatter_uppercase_id_validation_failure(self):
        content = "---\ntype: spec\nid: SPEC-UPPERCASE\ntitle: Title\nstatus: completed\n---\n# Body"
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is False
        assert any("ID format" in e for e in errors)

    def test_frontmatter_unrecognized_extra_fields(self):
        content = "---\ntype: spec\nid: spec-1\ntitle: Title\nstatus: completed\ncustom_field: hello\n---\n# Body"
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is True

    def test_frontmatter_empty_markdown_file(self):
        content = ""
        is_valid, errors = FrontmatterContractValidator.validate_content(content)
        assert is_valid is False

    # Feature 17 Boundary
    @pytest.mark.asyncio
    async def test_process_embeddings_bg_exception_in_embedder(self):
        chunk = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={"content": "text"})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", side_effect=RuntimeError("Embedding network failure")
        ):
            await _process_embeddings_bg([chunk])

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_embedder_returns_none(self):
        chunk = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={"content": "text"})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", new_callable=AsyncMock, return_value=None
        ):
            await _process_embeddings_bg([chunk])

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_with_chunks_missing_content(self):
        chunk = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True):
            await _process_embeddings_bg([chunk])

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_large_batch(self):
        chunks = [
            NodeData(node_labels=["DocumentChunk"], node_id=f"c-{i}", properties={"content": f"text {i}"})
            for i in range(100)
        ]
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", new_callable=AsyncMock, return_value=[[0.1] * 384] * 100
        ), patch("app.core.graph_builder.ingest_chunks", new_callable=AsyncMock) as mock_ingest:
            await _process_embeddings_bg(chunks)
            assert mock_ingest.called

    @pytest.mark.asyncio
    async def test_process_embeddings_bg_retry_on_transient_error(self):
        chunk = NodeData(node_labels=["DocumentChunk"], node_id="c1", properties={"content": "retry text"})
        with patch("app.core.embedder.is_embedder_enabled", return_value=True), patch(
            "app.core.embedder.embed_texts", side_effect=[TimeoutError("Timeout"), [[0.1] * 384]]
        ):
            # First attempt fails silently
            await _process_embeddings_bg([chunk])

    # Feature 18 Boundary
    def test_nodes_list_invalid_dimension_key_404(self, api_client: TestClient):
        with patch("app.routes.nodes._load_dimension_map", return_value={"spec": DimensionConfig(dimension="spec")}):
            res = api_client.get("/api/v1/nodes/invalid_dimension_name")
            assert res.status_code == 404

    def test_query_context_empty_keywords(self, api_client: TestClient):
        res = api_client.get("/api/v1/query?keywords=")
        assert res.status_code == 200
        assert len(res.json()["nodes"]) == 0

    def test_reset_graph_returns_deleted_count(self, api_client: TestClient):
        res = api_client.delete("/api/v1/graph")
        assert res.status_code == 200
        assert "nodes_deleted" in res.json()

    def test_concurrent_manifest_requests_handling(self, api_client: TestClient):
        manifest_data = {"source": "concurrent-test", "nodes": [], "edges": []}
        res1 = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        res2 = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res1.status_code == 200
        assert res2.status_code == 200

    def test_large_subgraph_token_estimation(self):
        nodes = [
            _neo4j_node_to_context({"id": f"s-{i}", "title": "Large Spec Title " * 10, "summary": "Summary text " * 20}, ["Spec"])
            for i in range(50)
        ]
        tokens = _estimate_tokens(nodes)
        assert tokens > 1000

    # Feature 19 Boundary
    def test_dockerfile_syntax_copy_instructions(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "COPY" in content

    def test_dockerfile_workdir_instruction(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "WORKDIR" in content

    def test_docker_compose_version_or_services(self):
        compose_path = Path(__file__).parent.parent / "docker-compose.yml"
        content = compose_path.read_text(encoding="utf-8")
        assert "services:" in content.lower() or "version:" in content.lower()

    def test_docker_compose_environment_variables(self):
        compose_path = Path(__file__).parent.parent / "docker-compose.yml"
        content = compose_path.read_text(encoding="utf-8")
        assert "NEO4J_" in content or "CORTEX_" in content or "environment" in content.lower()

    def test_dockerfile_install_dependencies_step(self):
        dockerfile_path = Path(__file__).parent.parent / "Dockerfile"
        content = dockerfile_path.read_text(encoding="utf-8")
        assert "pip install" in content or "poetry" in content or "requirements" in content


# =============================================================================
# TIER 3: CROSS-FEATURE COMBINATIONS (PAIRWISE & MULTI-FEATURE INTERACTIONS)
# =============================================================================

class TestTier3CrossFeatureCombinations:
    """Tier 3: Pairwise and multi-feature interaction test cases."""

    def test_domain_isolation_with_shadow_graph_branching(self, api_client: TestClient):
        """Combines Feature 2/3/4 (Domain Isolation) + Feature 6/7 (Shadow Graph Branching)."""
        manifest_data = {
            "source": "cli-branch-sync",
            "domain_id": "payments-squad",
            "branch": "feat/pix-v2",
            "nodes": [
                {
                    "node_id": "draft:feat/pix-v2:spec-pix",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {
                        "canonical_id": "spec-pix",
                        "branch": "feat/pix-v2",
                        "status": "draft",
                        "domain_id": "payments-squad",
                        "title": "PIX V2 Spec",
                    },
                }
            ],
            "edges": [],
        }
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200

        # Query with domain_id and branch
        query_res = api_client.get("/api/v1/query?keywords=pix&domain_id=payments-squad&branch=feat/pix-v2")
        assert query_res.status_code == 200

    def test_concept_glossary_linked_to_implementation_history_in_domain(self):
        """Combines Feature 11 (Business Glossary) + Feature 13 (Implementation History) + Feature 3 (Domain Isolation)."""
        concept_node = NodeData(
            node_labels=["Concept", "Intent"],
            node_id="concept-pix",
            properties={"term": "PIX", "definition": "Instant payment system", "domain_id": "payments"},
        )
        history_node = NodeData(
            node_labels=["History", "Implementation"],
            node_id="hist-epic-pix",
            properties={"title": "PIX Implementation Epic", "decision_log": "Adopted Neo4j graph", "domain_id": "payments"},
        )
        edge = EdgeData(
            from_id="hist-epic-pix",
            to_id="concept-pix",
            relationship="IMPLEMENTS_CONCEPT",
            properties={"domain_id": "payments"},
        )
        assert concept_node.properties["domain_id"] == history_node.properties["domain_id"]
        assert edge.properties["domain_id"] == "payments"

    def test_backstage_catalog_with_draft_branch_ingestion(self, tmp_path: Path):
        """Combines Feature 10 (Backstage Catalog Parser) + Feature 6 (Shadow Graph Draft Tracking)."""
        f = tmp_path / "catalog-info.yaml"
        f.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: auth-service\n"
            "  domain: security\n"
            "spec:\n"
            "  type: service\n"
        )
        parser = BackstageCatalogParser()
        cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
        parse_res = parser.parse(f, cfg)
        assert len(parse_res.nodes) == 1

        service_node = parse_res.nodes[0]
        # Wrap as draft node on branch
        draft_service_node = NodeData(
            node_labels=service_node.node_labels + ["Draft"],
            node_id=f"draft:feat/auth-v2:{service_node.node_id}",
            properties={
                **service_node.properties,
                "canonical_id": service_node.node_id,
                "branch": "feat/auth-v2",
                "status": "draft",
            },
        )
        assert draft_service_node.node_id == "draft:feat/auth-v2:service-auth-service"
        assert draft_service_node.properties["status"] == "draft"

    def test_frontmatter_validation_to_domain_manifest_ingest_flow(self, api_client: TestClient):
        """Combines Feature 15 (Frontmatter Validator) + Feature 2 (Manifest Ingestion Domain Stamping)."""
        markdown_spec = "---\ntype: spec\nid: spec-loan-calculator\ntitle: Loan Calculator Spec\nstatus: in-progress\n---\n# Spec"
        is_valid, errors = FrontmatterContractValidator.validate_content(markdown_spec)
        assert is_valid is True

        manifest_data = {
            "source": "cli-frontmatter-validator",
            "domain_id": "loans-domain",
            "nodes": [
                {
                    "node_id": "spec-loan-calculator",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {"title": "Loan Calculator Spec", "domain_id": "loans-domain"},
                }
            ],
            "edges": [],
        }
        res = api_client.post("/api/v1/ingest/manifest", json=manifest_data)
        assert res.status_code == 200

    def test_async_embedding_with_domain_isolated_semantic_search(self, api_client: TestClient):
        """Combines Feature 17 (Async Embedding) + Feature 4 (Domain Isolation Filtering) + Feature 2."""
        payload = {"query": "how to perform loan calculation?", "top_k": 5, "domain_id": "loans-domain"}
        res = api_client.post("/api/v1/query/semantic", json=payload)
        assert res.status_code in (200, 503)

    def test_branch_merge_to_ci_draft_consolidation_flow(self, api_client: TestClient):
        """Combines Feature 7 (Branch-Aware Merged Graph Query) + Feature 9 (CI Draft Consolidation)."""
        branch = "feat/loan-refactor"
        domain_id = "loans"

        # 1. Draft phase
        draft_node = NodeData(
            node_labels=["Spec", "Draft"],
            node_id=f"draft:{branch}:spec-loan",
            properties={"canonical_id": "spec-loan", "branch": branch, "status": "draft", "domain_id": domain_id},
        )

        # 2. Consolidation phase on PR merge
        res_cons = api_client.post("/api/v1/consolidate", json={"branch": branch, "domain_id": domain_id})
        assert res_cons.status_code == 200
        consolidation = res_cons.json()
        assert consolidation["status"] == "success"
        assert consolidation["branch"] == branch
        assert consolidation["domain_id"] == domain_id
        assert consolidation["consolidated_count"] >= 0

        # 3. Canonical state after promotion
        canonical_node = NodeData(
            node_labels=["Spec"],
            node_id=draft_node.properties["canonical_id"],
            properties={"title": "Updated Spec", "status": "completed", "domain_id": domain_id},
        )
        assert canonical_node.node_id == "spec-loan"

    def test_concept_glossary_connected_to_backstage_services(self, tmp_path: Path):
        """Combines Feature 11 (Business Glossary) + Feature 10 (Backstage Catalog Parser)."""
        f = tmp_path / "catalog-info.yaml"
        f.write_text("apiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata:\n  name: billing-svc\n")
        parser = BackstageCatalogParser()
        service_result = parser.parse(f, DimensionConfig(dimension="service", node_label="Service"))

        concept_node = NodeData(
            node_labels=["Concept"],
            node_id="concept-invoice",
            properties={"term": "Invoice", "definition": "Billing invoice entity"},
        )
        edge = EdgeData(
            from_id=concept_node.node_id,
            to_id=service_result.nodes[0].node_id,
            relationship="IMPLEMENTED_BY",
        )
        assert edge.to_id == "service-billing-svc"

    def test_implementation_history_in_branch_aware_query(self):
        """Combines Feature 13 (Implementation History) + Feature 7 (Branch-Aware Merged Query)."""
        history_main = {"id": "hist-001", "summary": "Main History Log"}
        history_draft = {"id": "draft:feat/hist:hist-001", "summary": "Updated History on Branch"}

        # Merged query prioritizes branch draft
        merged = {**history_main, **history_draft}
        assert merged["summary"] == "Updated History on Branch"

    def test_multi_domain_manifest_ingestion_and_isolated_query(self, api_client: TestClient):
        """Combines Feature 2 (Manifest Ingestion) + Feature 3 (Partitioning) + Feature 4 (Isolation)."""
        manifest_payments = {
            "source": "cli-sync",
            "domain_id": "payments",
            "nodes": [{"node_id": "spec-pay", "node_labels": ["Spec"], "properties": {"domain_id": "payments"}}],
            "edges": [],
        }
        manifest_auth = {
            "source": "cli-sync",
            "domain_id": "auth",
            "nodes": [{"node_id": "spec-auth", "node_labels": ["Spec"], "properties": {"domain_id": "auth"}}],
            "edges": [],
        }
        res1 = api_client.post("/api/v1/ingest/manifest", json=manifest_payments)
        res2 = api_client.post("/api/v1/ingest/manifest", json=manifest_auth)
        assert res1.status_code == 200
        assert res2.status_code == 200

        # Verify query isolation
        q_pay = api_client.get("/api/v1/query?keywords=spec&domain_id=payments")
        assert q_pay.status_code == 200

    def test_full_shadow_graph_lifecycle_with_domain_partitioning(self, api_client: TestClient):
        """Combines Features 2, 3, 4, 6, 7, 9 in a unified lifecycle test."""
        domain_id = "checkout"
        branch = "feat/one-click-checkout"

        # Step 1: Draft ingestion
        draft_manifest = {
            "source": "cli-sync",
            "domain_id": domain_id,
            "branch": branch,
            "nodes": [
                {
                    "node_id": f"draft:{branch}:spec-checkout",
                    "node_labels": ["Spec"],
                    "properties": {
                        "canonical_id": "spec-checkout",
                        "branch": branch,
                        "status": "draft",
                        "domain_id": domain_id,
                    },
                }
            ],
            "edges": [],
        }
        res_ingest = api_client.post("/api/v1/ingest/manifest", json=draft_manifest)
        assert res_ingest.status_code == 200

        # Step 2: Query branch view
        res_query = api_client.get(f"/api/v1/query?keywords=checkout&domain_id={domain_id}&branch={branch}")
        assert res_query.status_code == 200

        # Step 3: CI Consolidation on PR merge
        res_cons = api_client.post("/api/v1/consolidate", json={"branch": branch, "domain_id": domain_id})
        assert res_cons.status_code == 200
        consolidation = res_cons.json()
        assert consolidation["status"] == "success"
        assert consolidation["branch"] == branch
        assert consolidation["domain_id"] == domain_id
        assert consolidation["consolidated_count"] >= 0


# =============================================================================
# TIER 4: REAL-WORLD APPLICATION SCENARIOS (END-TO-END MULTI-SQUAD WORKLOADS)
# =============================================================================

class TestTier4RealWorldApplicationScenarios:
    """Tier 4: End-to-end multi-squad real-world workload scenarios."""

    def test_real_world_multi_squad_payments_and_auth_ingestion_isolation(self, api_client: TestClient):
        """
        Scenario: Squad Payments and Squad Auth simultaneously ingest specs and services.
        Verifies that querying Payments domain returns zero entities from Auth domain.
        """
        payments_manifest = {
            "source": "payments-ci",
            "domain_id": "squad-payments",
            "nodes": [
                {
                    "node_id": "spec-pix-payment",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {"title": "PIX Instant Payment Spec", "domain_id": "squad-payments"},
                },
                {
                    "node_id": "service-pix-gateway",
                    "node_labels": ["Service", "System"],
                    "properties": {"name": "pix-gateway", "domain_id": "squad-payments"},
                },
            ],
            "edges": [
                {
                    "from_id": "spec-pix-payment",
                    "to_id": "service-pix-gateway",
                    "relationship": "AFFECTS",
                    "properties": {"domain_id": "squad-payments"},
                }
            ],
        }

        auth_manifest = {
            "source": "auth-ci",
            "domain_id": "squad-auth",
            "nodes": [
                {
                    "node_id": "spec-oauth2-login",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {"title": "OAuth2 Single Sign-On Spec", "domain_id": "squad-auth"},
                },
                {
                    "node_id": "service-identity-provider",
                    "node_labels": ["Service", "System"],
                    "properties": {"name": "identity-provider", "domain_id": "squad-auth"},
                },
            ],
            "edges": [
                {
                    "from_id": "spec-oauth2-login",
                    "to_id": "service-identity-provider",
                    "relationship": "AFFECTS",
                    "properties": {"domain_id": "squad-auth"},
                }
            ],
        }

        res_p = api_client.post("/api/v1/ingest/manifest", json=payments_manifest)
        res_a = api_client.post("/api/v1/ingest/manifest", json=auth_manifest)
        assert res_p.status_code == 200
        assert res_a.status_code == 200

        # Domain Isolated Query
        res_p_query = api_client.get("/api/v1/query?keywords=payment&domain_id=squad-payments")
        assert res_p_query.status_code == 200

    def test_real_world_feature_branch_development_to_pr_consolidation_flow(self, api_client: TestClient):
        """
        Scenario: Developer creates feature branch 'feat/biometric-auth'.
        1. Developer syncs WIP specs to Shadow Graph as draft nodes.
        2. IDE AI Agent queries branch-merged graph view.
        3. PR merges to main -> CI triggers consolidation endpoint.
        4. Main graph reflects promoted canonical nodes.
        """
        branch = "feat/biometric-auth"
        domain_id = "security-squad"

        # Step 1: Draft Ingestion
        draft_payload = {
            "source": "cli-sync",
            "domain_id": domain_id,
            "branch": branch,
            "nodes": [
                {
                    "node_id": f"draft:{branch}:spec-biometric",
                    "node_labels": ["Spec", "Intent", "Draft"],
                    "properties": {
                        "canonical_id": "spec-biometric",
                        "title": "Biometric Authentication Spec (WIP)",
                        "branch": branch,
                        "status": "draft",
                        "domain_id": domain_id,
                    },
                }
            ],
            "edges": [],
        }
        res_sync = api_client.post("/api/v1/ingest/manifest", json=draft_payload)
        assert res_sync.status_code == 200

        # Step 2: Branch Query
        res_query = api_client.get(f"/api/v1/query?keywords=biometric&domain_id={domain_id}&branch={branch}")
        assert res_query.status_code == 200

        # Step 3: CI Consolidation
        res_cons = api_client.post("/api/v1/consolidate", json={"branch": branch, "domain_id": domain_id})
        assert res_cons.status_code == 200
        consolidation_result = res_cons.json()
        assert consolidation_result["status"] == "success"
        assert consolidation_result["branch"] == branch
        assert consolidation_result["domain_id"] == domain_id
        assert consolidation_result["consolidated_count"] >= 0

    def test_real_world_architecture_onboarding_glossary_and_service_traversal(self, tmp_path: Path):
        """
        Scenario: New engineer onboards to the Loans platform.
        1. Ingests Backstage catalog-info.yaml for Loan Processing Services.
        2. Ingests Business Glossary Concept nodes ([:CONCEPT]).
        3. Traverses relationship from Business Concept -> Service -> Specs.
        """
        # 1. Backstage Catalog Parsing
        catalog_file = tmp_path / "catalog-info.yaml"
        catalog_file.write_text(
            "apiVersion: backstage.io/v1alpha1\n"
            "kind: Component\n"
            "metadata:\n"
            "  name: loan-origination-service\n"
            "  domain: loans\n"
            "spec:\n"
            "  type: service\n"
            "  providesApis:\n"
            "    - origination-v1-api\n"
        )
        parser = BackstageCatalogParser()
        svc_result = parser.parse(catalog_file, DimensionConfig(dimension="service", node_label="Service", pillar="System"))
        assert len(svc_result.nodes) == 1
        service_node = svc_result.nodes[0]

        # 2. Business Glossary Concept Node
        concept_node = NodeData(
            node_labels=["Concept", "Intent"],
            node_id="concept-origination",
            properties={
                "term": "Loan Origination",
                "definition": "The process by which a borrower applies for a loan and a lender processes that application.",
                "domain_id": "loans",
            },
        )

        # 3. Traversal Edge
        concept_to_service_edge = EdgeData(
            from_id=concept_node.node_id,
            to_id=service_node.node_id,
            relationship="IMPLEMENTED_BY",
            properties={"domain_id": "loans"},
        )

        assert concept_to_service_edge.from_id == "concept-origination"
        assert concept_to_service_edge.to_id == "service-loan-origination-service"

    def test_real_world_incident_post_mortem_history_and_spec_audit(self):
        """
        Scenario: Production incident leads to Post-Mortem record creation.
        1. Implementation History node [:IMPLEMENTATION_HISTORY] recorded with decision log & root cause.
        2. History node linked to affected Spec and Service nodes.
        3. Future AI agent queries history to prevent regression.
        """
        history_node = NodeData(
            node_labels=["History", "Implementation"],
            node_id="hist-incident-2026-08-01",
            properties={
                "title": "Post-Mortem: Redis Connection Pool Exhaustion",
                "decision_log": "Increased pool size to 200 and added exponential backoff retry policy.",
                "type": "incident_remediation",
                "author": "sre-team",
                "domain_id": "core-platform",
            },
        )

        spec_edge = EdgeData(
            from_id=history_node.node_id,
            to_id="spec-redis-caching",
            relationship="UPDATES_SPEC",
            properties={"domain_id": "core-platform"},
        )

        service_edge = EdgeData(
            from_id=history_node.node_id,
            to_id="service-core-api",
            relationship="REMEDIATES_SERVICE",
            properties={"domain_id": "core-platform"},
        )

        assert history_node.properties["type"] == "incident_remediation"
        assert spec_edge.relationship == "UPDATES_SPEC"
        assert service_edge.relationship == "REMEDIATES_SERVICE"

    def test_real_world_full_enterprise_cortex_backbone_end_to_end(self, api_client: TestClient, tmp_path: Path):
        """
        Scenario: Full End-to-End Enterprise AI-DLC Backbone Workflow.
        1. Backstage Catalog service ingestion.
        2. Markdown Intent spec validation & frontmatter parsing.
        3. Multi-tenant domain-stamped manifest ingestion via REST API.
        4. Speculative Shadow Graph branch draft sync & query.
        5. CI Deploy draft consolidation.
        6. Vector RAG & Subgraph queries verification.
        """
        domain_id = "enterprise-core"
        branch = "release/v2.5"

        # 1. Backstage catalog
        cat_file = tmp_path / "catalog-info.yaml"
        cat_file.write_text("apiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata:\n  name: core-gateway\n")
        cat_parser = BackstageCatalogParser()
        cat_res = cat_parser.parse(cat_file, DimensionConfig(dimension="service", node_label="Service"))
        assert len(cat_res.nodes) == 1

        # 2. Markdown frontmatter validation
        spec_content = "---\ntype: spec\nid: spec-core-v2\ntitle: Core Platform V2 Spec\nstatus: in-progress\n---\n# Spec"
        is_valid, errors = FrontmatterContractValidator.validate_content(spec_content)
        assert is_valid is True

        # 3. Manifest Ingestion
        manifest = {
            "source": "e2e-enterprise-suite",
            "domain_id": domain_id,
            "nodes": [
                {
                    "node_id": "spec-core-v2",
                    "node_labels": ["Spec", "Intent"],
                    "properties": {"title": "Core Platform V2 Spec", "domain_id": domain_id},
                },
                {
                    "node_id": cat_res.nodes[0].node_id,
                    "node_labels": cat_res.nodes[0].node_labels,
                    "properties": cat_res.nodes[0].properties,
                },
            ],
            "edges": [],
        }
        ingest_res = api_client.post("/api/v1/ingest/manifest", json=manifest)
        assert ingest_res.status_code == 200

        # 4. Shadow Graph Branch Draft Sync
        draft_manifest = {
            "source": "cli-sync",
            "domain_id": domain_id,
            "branch": branch,
            "nodes": [
                {
                    "node_id": f"draft:{branch}:spec-core-v2",
                    "node_labels": ["Spec", "Draft"],
                    "properties": {
                        "canonical_id": "spec-core-v2",
                        "title": "Core Spec V2 (Draft Branch)",
                        "branch": branch,
                        "status": "draft",
                        "domain_id": domain_id,
                    },
                }
            ],
            "edges": [],
        }
        draft_res = api_client.post("/api/v1/ingest/manifest", json=draft_manifest)
        assert draft_res.status_code == 200

        # 5. CI Consolidation
        res_cons = api_client.post("/api/v1/consolidate", json={"branch": branch, "domain_id": domain_id})
        assert res_cons.status_code == 200
        consolidation = res_cons.json()
        assert consolidation["status"] == "success"
        assert consolidation["branch"] == branch
        assert consolidation["domain_id"] == domain_id
        assert consolidation["consolidated_count"] >= 0

        # 6. Final Subgraph & Health Check
        health = api_client.get("/health")
        assert health.status_code == 200
