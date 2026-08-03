"""
tests/test_challenger_m3_stress.py — Empirical Stress Test Suite for Milestone M3.

Executed by Challenger 1 (challenger_m3_1) to stress-test:
1. Backstage catalog parsing: empty YAML docs, missing metadata, complex entity refs,
   node label mapping, edge extraction.
2. Frontmatter contract validator: invalid IDs (uppercase, spaces, special chars),
   invalid status strings, missing required fields, missing --- header.
3. Async background embedding & domain_id propagation: non-blocking background tasks,
   domain_id attachment to nodes, edges, and chunks.
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
import pytest
from fastapi import BackgroundTasks

from app.core.dimension_loader import DimensionConfig
from app.core.parsers.builtin.backstage_catalog import BackstageCatalogParser, _parse_entity_ref
from app.core.parsers.builtin.markdown_frontmatter import (
    MarkdownFrontmatterParser,
    FrontmatterContractValidator,
)
from app.routes.ingest import _run_ingest_pipeline, _process_embeddings_bg


# ─── 1. Backstage Catalog Parser Stress Tests ─────────────────────────────────

def test_backstage_multi_doc_with_empty_and_null_docs(tmp_path: Path):
    """Stress test multi-document YAML with empty documents (--- \\n ---), null values, and primitive items."""
    manifest = tmp_path / "catalog-info.yaml"
    manifest.write_text(
        "---\n"
        "---\n"  # Empty doc 1
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        "  name: order-service\n"
        "  namespace: e-commerce\n"
        "spec:\n"
        "  type: service\n"
        "  owner: checkout-team\n"
        "---\n"
        "just a plain string document\n"  # Primitive non-dict doc
        "---\n"
        "---\n"  # Empty doc 2
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: API\n"
        "metadata:\n"
        "  name: order-api\n"
        "  domain: checkout\n"
        "spec:\n"
        "  type: openapi\n"
        "---\n"
        "kind: Component\n"
        "# Missing metadata and name entirely\n"
        "spec:\n"
        "  type: service\n"
    )

    parser = BackstageCatalogParser()
    cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
    result = parser.parse(manifest, cfg)

    # Should safely extract only the 2 valid dict documents (order-service and order-api)
    assert len(result.nodes) == 2

    svc_node = next(n for n in result.nodes if n.node_id == "service-order-service")
    assert svc_node.properties["domain_id"] == "e-commerce"
    assert svc_node.properties["owner"] == "checkout-team"
    assert set(svc_node.node_labels) == {"Service", "System"}

    api_node = next(n for n in result.nodes if n.node_id == "api-order-api")
    assert api_node.properties["domain_id"] == "checkout"
    assert set(api_node.node_labels) == {"API", "System"}


def test_backstage_complex_entity_refs():
    """Stress test _parse_entity_ref with diverse entity reference formats."""
    # kind:namespace/name
    kind, name, node_id = _parse_entity_ref("component:default/auth-service")
    assert (kind, name, node_id) == ("service", "auth-service", "service-auth-service")

    kind, name, node_id = _parse_entity_ref("api:production/payment-api")
    assert (kind, name, node_id) == ("api", "payment-api", "api-payment-api")

    kind, name, node_id = _parse_entity_ref("system:enterprise/core-system")
    assert (kind, name, node_id) == ("system", "core-system", "system-core-system")

    kind, name, node_id = _parse_entity_ref("domain:finance/billing")
    assert (kind, name, node_id) == ("domain", "billing", "domain-billing")

    kind, name, node_id = _parse_entity_ref("resource:cloud/s3-bucket")
    assert (kind, name, node_id) == ("resource", "s3-bucket", "resource-s3-bucket")

    # kind:name
    kind, name, node_id = _parse_entity_ref("component:user-service")
    assert (kind, name, node_id) == ("service", "user-service", "service-user-service")

    # namespace/name with default_kind
    kind, name, node_id = _parse_entity_ref("default/user-service", default_kind="service")
    assert (kind, name, node_id) == ("service", "user-service", "service-user-service")

    # bare name with default_kind
    kind, name, node_id = _parse_entity_ref("user-service", default_kind="service")
    assert (kind, name, node_id) == ("service", "user-service", "service-user-service")

    kind, name, node_id = _parse_entity_ref("user-api", default_kind="api")
    assert (kind, name, node_id) == ("api", "user-api", "api-user-api")

    # Empty ref
    kind, name, node_id = _parse_entity_ref("")
    assert (kind, name, node_id) == ("service", "", "service-")


def test_backstage_edge_extraction_and_domain_propagation(tmp_path: Path):
    """Stress test EXPOSES, DEPENDS_ON, and CALLS relationships and domain propagation."""
    manifest = tmp_path / "catalog-info.yaml"
    manifest.write_text(
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        "  name: checkout-service\n"
        "  domain: payments\n"
        "spec:\n"
        "  type: service\n"
        "  providesApis:\n"
        "    - api:payments/checkout-api\n"
        "  consumesApis:\n"
        "    - api:auth/login-api\n"
        "  dependsOn:\n"
        "    - component:inventory/stock-service\n"
    )

    parser = BackstageCatalogParser()
    cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
    result = parser.parse(manifest, cfg)

    assert len(result.nodes) == 1
    assert len(result.edges) == 3

    exposes = next(e for e in result.edges if e.relationship == "EXPOSES")
    assert exposes.from_id == "service-checkout-service"
    assert exposes.to_id == "api-checkout-api"
    assert exposes.properties["domain_id"] == "payments"

    calls = next(e for e in result.edges if e.relationship == "CALLS")
    assert calls.from_id == "service-checkout-service"
    assert calls.to_id == "api-login-api"
    assert calls.properties["domain_id"] == "payments"

    depends = next(e for e in result.edges if e.relationship == "DEPENDS_ON")
    assert depends.from_id == "service-checkout-service"
    assert depends.to_id == "service-stock-service"
    assert depends.properties["domain_id"] == "payments"


# ─── 2. Frontmatter Contract Validator Stress Tests ───────────────────────────

def test_frontmatter_contract_validator_invalid_ids():
    """Stress test FrontmatterContractValidator with invalid IDs (uppercase, spaces, special chars)."""
    # Uppercase ID
    c1 = "---\ntype: spec\nid: SPEC-100\ntitle: Test\nstatus: planned\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c1)
    assert valid is False
    assert any("Invalid ID format 'SPEC-100'" in e for e in errors)

    # Spaces in ID
    c2 = "---\ntype: spec\nid: spec 100\ntitle: Test\nstatus: planned\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c2)
    assert valid is False
    assert any("Invalid ID format 'spec 100'" in e for e in errors)

    # Special characters in ID
    c3 = "---\ntype: spec\nid: spec_100!\ntitle: Test\nstatus: planned\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c3)
    assert valid is False
    assert any("Invalid ID format 'spec_100!'" in e for e in errors)

    # Valid hyphenated lowercase ID
    c4 = "---\ntype: spec\nid: spec-100-v2\ntitle: Test\nstatus: planned\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c4)
    assert valid is True
    assert errors == []


def test_frontmatter_contract_validator_invalid_statuses():
    """Stress test FrontmatterContractValidator with invalid status strings."""
    c1 = "---\ntype: spec\nid: spec-101\ntitle: Test\nstatus: in_review\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c1)
    assert valid is False
    assert any("Invalid status 'in_review'" in e for e in errors)

    # Test all valid statuses
    for s in ["planned", "in-progress", "completed", "done", "deprecated", "draft"]:
        c = f"---\ntype: spec\nid: spec-101\ntitle: Test\nstatus: {s}\n---\n# Content"
        valid, errors = FrontmatterContractValidator.validate_content(c)
        assert valid is True, f"Status '{s}' should be valid"


def test_frontmatter_contract_validator_missing_fields_and_header():
    """Stress test missing required fields and missing --- frontmatter block."""
    # Missing header
    c1 = "# Header without frontmatter"
    valid, errors = FrontmatterContractValidator.validate_content(c1)
    assert valid is False
    assert any("Missing YAML frontmatter block" in e for e in errors)

    # Missing type & title
    c2 = "---\nid: spec-102\nstatus: planned\n---\n# Content"
    valid, errors = FrontmatterContractValidator.validate_content(c2)
    assert valid is False
    assert any("Missing required frontmatter field: 'type'" in e for e in errors)
    assert any("Missing required frontmatter field: 'title'" in e for e in errors)


def test_parser_stamps_validation_properties(tmp_path: Path):
    """Verify MarkdownFrontmatterParser stamps validation properties onto NodeData."""
    doc = tmp_path / "001-invalid-spec" / "spec.md"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_text("---\ntype: spec\nid: INVALID_ID\ntitle: Test\nstatus: bad_status\n---\n# Body")

    parser = MarkdownFrontmatterParser()
    cfg = MagicMock()
    cfg.dimension = "spec"
    cfg.pillar = "Intent"
    cfg.node_labels = ["Spec", "Intent"]

    result = parser.parse(doc, cfg)
    assert len(result.nodes) == 1
    node = result.nodes[0]

    assert node.properties["frontmatter_valid"] is False
    assert node.properties["validation_status"] == "failed"
    assert len(node.properties["frontmatter_errors"]) >= 2


# ─── 3. Async Background Embedding & Domain ID Stress Tests ──────────────────

@pytest.mark.asyncio
async def test_async_background_embedding_and_domain_id_propagation():
    """Stress test non-blocking background task queuing and domain_id propagation."""
    bg_tasks = BackgroundTasks()

    # Mock dimension config
    dim_cfg = MagicMock()
    dim_cfg.dimension = "spec"
    dim_cfg.pillar = "Intent"
    dim_cfg.indexes = []
    dim_cfg.parser = "builtin.markdown_frontmatter"
    dim_cfg.source_type = "filesystem"
    dim_cfg.source_path = None
    dim_cfg.source_patterns = ["*.md"]

    with patch("app.routes.ingest.get_driver") as mock_get_driver, \
         patch("app.routes.ingest.ingest_nodes", new_callable=AsyncMock) as mock_ingest_nodes, \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock) as mock_ingest_edges:

        mock_ingest_nodes.return_value = 1
        mock_ingest_edges.return_value = 0

        res = await _run_ingest_pipeline(
            dim_config=dim_cfg,
            plugins_dir=None,
            background_tasks=bg_tasks,
            domain_id="tenant-alpha",
        )

        assert res.domain_id == "tenant-alpha"
        assert res.dim_key == "spec"
        assert res.nodes_upserted == 1


@pytest.mark.asyncio
async def test_process_embeddings_bg_domain_id():
    """Stress test _process_embeddings_bg attaching domain_id to chunk properties."""
    chunk1 = MagicMock()
    chunk1.properties = {"content": "Sample text chunk content"}

    with patch("app.core.embedder.is_embedder_enabled", return_value=True), \
         patch("app.core.embedder.embed_texts", new_callable=AsyncMock) as mock_embed, \
         patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest.ingest_chunks", new_callable=AsyncMock) as mock_ingest_chunks:

        mock_embed.return_value = [[0.1, 0.2, 0.3]]

        await _process_embeddings_bg([chunk1], domain_id="tenant-beta")

        assert chunk1.properties["embedding"] == [0.1, 0.2, 0.3]
        assert chunk1.properties["domain_id"] == "tenant-beta"
        mock_ingest_chunks.assert_called_once()
        args, kwargs = mock_ingest_chunks.call_args
        assert kwargs["domain_id"] == "tenant-beta"
