"""
tests/test_temporal_workflow_yaml_parser.py — Testes unitários do parser de TemporalWorkflow.
"""
from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from app.core.parsers.temporal_workflow_yaml_parser import TemporalWorkflowYamlParser


PARSER = TemporalWorkflowYamlParser()

# ── helpers ──────────────────────────────────────────────────────────────────

def _write_yaml(tmp_path: Path, content: str, filename: str = "temporal-workflows.yaml") -> Path:
    p = tmp_path / filename
    p.write_text(textwrap.dedent(content), encoding="utf-8")
    return p


# ── can_parse ─────────────────────────────────────────────────────────────────

def test_can_parse_correct_filename(tmp_path):
    p = tmp_path / "temporal-workflows.yaml"
    p.write_text("")
    assert PARSER.can_parse(p) is True


def test_can_parse_wrong_filename(tmp_path):
    p = tmp_path / "AGENTS.md"
    p.write_text("")
    assert PARSER.can_parse(p) is False


def test_can_parse_wrong_yaml_name(tmp_path):
    p = tmp_path / "workflows.yaml"
    p.write_text("")
    assert PARSER.can_parse(p) is False


# ── parse: arquivo mínimo válido ──────────────────────────────────────────────

def test_parse_single_workflow(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-sandbox
            name: SandboxWorkflow
            category: sandbox
            status: active
    """)
    result = PARSER.parse(p)

    assert len(result.nodes) == 1
    node = result.nodes[0]
    assert node.node_label == "TemporalWorkflow"
    assert node.node_id == "workflow-sandbox"
    assert node.properties["name"] == "SandboxWorkflow"
    assert node.properties["category"] == "sandbox"
    assert node.properties["status"] == "active"
    assert node.properties["service_id"] == "service-backend"


def test_parse_edges_belongs_to_and_has_workflow(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-sandbox
            name: SandboxWorkflow
            category: sandbox
            status: active
    """)
    result = PARSER.parse(p)

    rels = {(e.from_id, e.to_id, e.relationship) for e in result.edges}
    assert ("workflow-sandbox", "service-backend", "BELONGS_TO") in rels
    assert ("service-backend", "workflow-sandbox", "HAS_WORKFLOW") in rels


# ── parse: campos opcionais ───────────────────────────────────────────────────

def test_parse_scheduled_workflow_fields(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-finance-reconciliation
            name: FinanceReconciliationWorkflow
            category: scheduled
            schedule: "Daily 2:00 AM"
            status: active
            mode_resolver: FinanceReconciliationModeResolver
            replaces: ReconcileFinanceSummaryJob
            spec_ids: []
    """)
    result = PARSER.parse(p)

    assert len(result.nodes) == 1
    props = result.nodes[0].properties
    assert props["schedule"] == "Daily 2:00 AM"
    assert props["mode_resolver"] == "FinanceReconciliationModeResolver"
    assert props["replaces"] == "ReconcileFinanceSummaryJob"
    assert "spec_ids" not in props  # lista vazia não é armazenada


def test_parse_spec_ids_creates_implements_edges(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-outbound-execution
            name: OutboundExecutionWorkflow
            category: event-triggered
            status: active
            spec_ids:
              - spec-063
              - spec-070
    """)
    result = PARSER.parse(p)

    implements = [(e.from_id, e.to_id, e.relationship) for e in result.edges
                  if e.relationship == "IMPLEMENTS"]
    assert ("workflow-outbound-execution", "spec-063", "IMPLEMENTS") in implements
    assert ("workflow-outbound-execution", "spec-070", "IMPLEMENTS") in implements


def test_parse_null_optional_fields_not_stored(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-participant-lifecycle
            name: ParticipantLifecycleWorkflow
            category: event-triggered
            status: active
            schedule: null
            mode_resolver: null
            replaces: null
            spec_ids: []
    """)
    result = PARSER.parse(p)
    props = result.nodes[0].properties
    assert "schedule" not in props
    assert "mode_resolver" not in props
    assert "replaces" not in props


# ── parse: múltiplos workflows ────────────────────────────────────────────────

def test_parse_multiple_workflows(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-a
            name: WorkflowA
            category: scheduled
            status: active
          - id: workflow-b
            name: WorkflowB
            category: event-triggered
            status: deprecated
    """)
    result = PARSER.parse(p)
    assert len(result.nodes) == 2
    ids = {n.node_id for n in result.nodes}
    assert ids == {"workflow-a", "workflow-b"}


# ── parse: casos de erro / dados inválidos ────────────────────────────────────

def test_parse_missing_service_id_returns_empty(tmp_path):
    p = _write_yaml(tmp_path, """
        workflows:
          - id: workflow-x
            name: WorkflowX
            category: sandbox
            status: active
    """)
    result = PARSER.parse(p)
    assert result.nodes == []
    assert result.edges == []


def test_parse_invalid_category_defaults_to_event_triggered(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-x
            name: WorkflowX
            category: unknown-category
            status: active
    """)
    result = PARSER.parse(p)
    assert result.nodes[0].properties["category"] == "event-triggered"


def test_parse_invalid_status_defaults_to_active(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - id: workflow-x
            name: WorkflowX
            category: sandbox
            status: invalid-status
    """)
    result = PARSER.parse(p)
    assert result.nodes[0].properties["status"] == "active"


def test_parse_workflow_missing_id_skipped(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows:
          - name: WorkflowWithoutId
            category: sandbox
            status: active
          - id: workflow-valid
            name: WorkflowValid
            category: sandbox
            status: active
    """)
    result = PARSER.parse(p)
    assert len(result.nodes) == 1
    assert result.nodes[0].node_id == "workflow-valid"


def test_parse_empty_workflows_list(tmp_path):
    p = _write_yaml(tmp_path, """
        service_id: service-backend
        workflows: []
    """)
    result = PARSER.parse(p)
    assert result.nodes == []
    assert result.edges == []


def test_parse_invalid_yaml_returns_empty(tmp_path):
    p = tmp_path / "temporal-workflows.yaml"
    p.write_text(": : : invalid yaml {{{{", encoding="utf-8")
    result = PARSER.parse(p)
    assert result.nodes == []


def test_parse_full_manifest_15_workflows(tmp_path):
    """Smoke test: o manifesto real do backend deve produzir 15 nós."""
    manifest_path = Path(__file__).parent.parent.parent / ".." / \
        "worktrees" / "backend-role-organizado-146-temporal-workflow-dimension" / \
        "temporal-workflows.yaml"
    if not manifest_path.exists():
        pytest.skip("Manifesto do backend não encontrado no worktree")

    result = PARSER.parse(manifest_path)
    assert len(result.nodes) == 15, f"Esperado 15 workflows, obtido {len(result.nodes)}"
    # Cada workflow deve ter BELONGS_TO + HAS_WORKFLOW = 2 edges mínimas
    assert len(result.edges) >= 30
