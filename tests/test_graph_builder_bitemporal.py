"""
tests/test_graph_builder_bitemporal.py — Testes unitários do bitemporal versioning.

Testa que upsert_node/upsert_chunk:
  - Sempre define ingested_at
  - Preserva valid_from no re-ingest (COALESCE)
  - Propaga commit_sha quando fornecido
  - Omite commit_sha quando None
"""
from __future__ import annotations

import re
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from app.core.graph_builder import upsert_node
from app.core.parsers.base import NodeData


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_node(
    node_id: str = "spec-149",
    labels: list[str] | None = None,
    properties: dict | None = None,
) -> NodeData:
    labels = labels or ["Spec", "Intent"]
    properties = properties or {"id": node_id, "title": "Test Spec"}
    return NodeData(node_labels=labels, node_id=node_id, properties=properties)


def _make_driver(run_result=None) -> MagicMock:
    """Cria um AsyncDriver mock com session contextmanager."""
    mock_result = run_result or MagicMock()
    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    return driver, mock_session


# ── upsert_node — propriedades bitemporais ────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_node_sets_ingested_at():
    """ingested_at deve ser passado para o Cypher a cada chamada."""
    driver, mock_session = _make_driver()
    node = _make_node()

    await upsert_node(driver, node)

    mock_session.run.assert_called_once()
    cypher, *_ = mock_session.run.call_args[0]
    kwargs = mock_session.run.call_args[1] if mock_session.run.call_args[1] else {}

    # O parâmetro ingested_at deve estar presente
    assert "ingested_at" in kwargs or any(
        "ingested_at" in str(a) for a in mock_session.run.call_args[0]
    )


@pytest.mark.asyncio
async def test_upsert_node_cypher_contains_coalesce_valid_from():
    """O Cypher gerado deve usar COALESCE para valid_from."""
    driver, mock_session = _make_driver()
    node = _make_node()

    await upsert_node(driver, node)

    cypher = mock_session.run.call_args[0][0]
    assert "COALESCE(n.valid_from" in cypher, (
        "Cypher deve usar COALESCE para não sobrescrever valid_from existente"
    )


@pytest.mark.asyncio
async def test_upsert_node_with_commit_sha():
    """commit_sha deve ser propagado quando fornecido."""
    driver, mock_session = _make_driver()
    node = _make_node()

    await upsert_node(driver, node, commit_sha="abc1234")

    cypher = mock_session.run.call_args[0][0]
    kwargs = mock_session.run.call_args[1] if mock_session.run.call_args[1] else {}

    assert "commit_sha" in str(mock_session.run.call_args), (
        "commit_sha deve aparecer nos argumentos da chamada ao Cypher"
    )
    assert "CASE WHEN $commit_sha IS NOT NULL" in cypher, (
        "Cypher deve tratar commit_sha condicionalmente (não sobrescrever com NULL)"
    )


@pytest.mark.asyncio
async def test_upsert_node_without_commit_sha_preserves_existing():
    """Quando commit_sha=None, o Cypher não deve sobrescrever valor existente."""
    driver, mock_session = _make_driver()
    node = _make_node()

    await upsert_node(driver, node, commit_sha=None)

    cypher = mock_session.run.call_args[0][0]
    # Deve usar CASE WHEN para preservar o commit_sha existente
    assert "CASE WHEN $commit_sha IS NOT NULL" in cypher


# ── upsert_node — labels I.S.I.R. ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_upsert_node_applies_pillar_labels():
    """Nós com múltiplas labels devem ter todas aplicadas no Cypher."""
    driver, mock_session = _make_driver()
    node = _make_node(labels=["Spec", "Intent"])

    await upsert_node(driver, node)

    cypher = mock_session.run.call_args[0][0]
    # A label primária é usada no MERGE; labels extras em SET
    assert "Spec" in cypher
    assert "Intent" in cypher


# ── IngestManifest — schema validation ────────────────────────────────────────


def test_manifest_node_requires_labels():
    """ManifestNode deve exigir ao menos uma label."""
    from pydantic import ValidationError

    from app.core.parsers.manifest import ManifestNode

    with pytest.raises(ValidationError):
        ManifestNode(node_id="spec-149", node_labels=[])


def test_manifest_schema_roundtrip():
    """IngestManifest deve serializar/deserializar sem perda."""
    from app.core.parsers.manifest import IngestManifest, ManifestEdge, ManifestNode

    manifest = IngestManifest(
        source="git-diff",
        commit_sha="abc1234abcd5678",
        nodes=[
            ManifestNode(
                node_id="spec-149",
                node_labels=["Spec", "Intent"],
                properties={"title": "Cortex v3.0", "status": "in-progress"},
            ),
            ManifestNode(
                node_id="service-cortex",
                node_labels=["Service", "System"],
                properties={"name": "cortex-context"},
            ),
        ],
        edges=[
            ManifestEdge(
                from_id="spec-149",
                to_id="service-cortex",
                relationship="IMPLEMENTS",
                properties={"files_changed": 12, "via": "git-diff"},
            )
        ],
    )

    data = manifest.model_dump()
    restored = IngestManifest.model_validate(data)

    assert restored.source == "git-diff"
    assert restored.commit_sha == "abc1234abcd5678"
    assert len(restored.nodes) == 2
    assert len(restored.edges) == 1
    assert restored.edges[0].relationship == "IMPLEMENTS"
    assert restored.nodes[0].node_labels == ["Spec", "Intent"]


def test_manifest_defaults():
    """IngestManifest com payload mínimo deve ter defaults corretos."""
    from app.core.parsers.manifest import IngestManifest

    m = IngestManifest()
    assert m.source == "manual"
    assert m.commit_sha is None
    assert m.nodes == []
    assert m.edges == []


def test_manifest_edge_without_properties():
    """ManifestEdge sem properties deve usar dict vazio como default."""
    from app.core.parsers.manifest import ManifestEdge

    edge = ManifestEdge(from_id="a", to_id="b", relationship="RELATES_TO")
    assert edge.properties == {}
