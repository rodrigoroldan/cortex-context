"""
tests/test_backstage_catalog_parser.py — Unit tests for Backstage Catalog Parser (Feature 10).
"""
from pathlib import Path
import pytest

from app.core.dimension_loader import DimensionConfig
from app.core.parsers.builtin.backstage_catalog import BackstageCatalogParser, _parse_entity_ref


def test_parse_entity_ref_formats():
    # Test kind:namespace/name
    kind, name, node_id = _parse_entity_ref("component:default/auth-service")
    assert kind == "service"
    assert name == "auth-service"
    assert node_id == "service-auth-service"

    # Test api:namespace/name
    kind, name, node_id = _parse_entity_ref("api:default/user-api")
    assert kind == "api"
    assert name == "user-api"
    assert node_id == "api-user-api"

    # Test system:namespace/name
    kind, name, node_id = _parse_entity_ref("system:default/payments")
    assert kind == "system"
    assert name == "payments"
    assert node_id == "system-payments"

    # Test kind:name
    kind, name, node_id = _parse_entity_ref("component:order-service")
    assert kind == "service"
    assert name == "order-service"
    assert node_id == "service-order-service"

    # Test bare name with default_kind
    kind, name, node_id = _parse_entity_ref("auth-service", default_kind="service")
    assert kind == "service"
    assert name == "auth-service"
    assert node_id == "service-auth-service"

    # Test empty ref
    kind, name, node_id = _parse_entity_ref("")
    assert kind == "service"
    assert name == ""


def test_can_parse_catalog_info(tmp_path: Path):
    parser = BackstageCatalogParser()

    f1 = tmp_path / "catalog-info.yaml"
    f1.write_text("apiVersion: backstage.io/v1alpha1\nkind: Component\nmetadata:\n  name: svc\n")
    assert parser.can_parse(f1) is True

    f2 = tmp_path / "catalog-info.yml"
    f2.write_text("apiVersion: backstage.io/v1alpha1\nkind: API\nmetadata:\n  name: api\n")
    assert parser.can_parse(f2) is True

    f3 = tmp_path / "custom-service.yaml"
    f3.write_text("apiVersion: backstage.io/v1alpha1\nkind: Service\nmetadata:\n  name: custom\n")
    assert parser.can_parse(f3) is True

    f4 = tmp_path / "readme.md"
    f4.write_text("# Hello World")
    assert parser.can_parse(f4) is False


def test_parse_multi_doc_yaml(tmp_path: Path):
    f = tmp_path / "catalog-info.yaml"
    f.write_text(
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: System\n"
        "metadata:\n"
        "  name: payment-system\n"
        "  title: Payment System\n"
        "spec:\n"
        "  owner: team-payments\n"
        "---\n"
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: Component\n"
        "metadata:\n"
        "  name: payment-service\n"
        "  domain: payments\n"
        "spec:\n"
        "  type: service\n"
        "  owner: squad-checkout\n"
        "  system: payment-system\n"
        "  providesApis:\n"
        "    - api:default/payment-api\n"
        "  consumesApis:\n"
        "    - api:default/auth-api\n"
        "  dependsOn:\n"
        "    - component:default/user-service\n"
        "---\n"
        "apiVersion: backstage.io/v1alpha1\n"
        "kind: API\n"
        "metadata:\n"
        "  name: payment-api\n"
        "spec:\n"
        "  type: openapi\n"
        "  owner: squad-checkout\n"
    )

    parser = BackstageCatalogParser()
    cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
    result = parser.parse(f, cfg)

    # Verify nodes
    assert len(result.nodes) == 3

    # System node
    sys_node = next(n for n in result.nodes if n.node_id == "system-payment-system")
    assert "System" in sys_node.node_labels
    assert sys_node.properties["name"] == "payment-system"

    # Service node
    svc_node = next(n for n in result.nodes if n.node_id == "service-payment-service")
    assert "Service" in svc_node.node_labels
    assert "System" in svc_node.node_labels
    assert svc_node.properties["domain_id"] == "payments"

    # API node
    api_node = next(n for n in result.nodes if n.node_id == "api-payment-api")
    assert "API" in api_node.node_labels
    assert "System" in api_node.node_labels

    # Verify edges
    assert len(result.edges) == 3

    exposes_edge = next(e for e in result.edges if e.relationship == "EXPOSES")
    assert exposes_edge.from_id == "service-payment-service"
    assert exposes_edge.to_id == "api-payment-api"

    calls_edge = next(e for e in result.edges if e.relationship == "CALLS")
    assert calls_edge.from_id == "service-payment-service"
    assert calls_edge.to_id == "api-auth-api"

    depends_edge = next(e for e in result.edges if e.relationship == "DEPENDS_ON")
    assert depends_edge.from_id == "service-payment-service"
    assert depends_edge.to_id == "service-user-service"


def test_parse_corrupt_yaml(tmp_path: Path):
    f = tmp_path / "catalog-info.yaml"
    f.write_text("apiVersion: [invalid yaml structure: {")

    parser = BackstageCatalogParser()
    cfg = DimensionConfig(dimension="service", node_label="Service", pillar="System")
    result = parser.parse(f, cfg)

    assert len(result.nodes) == 0
    assert len(result.edges) == 0
