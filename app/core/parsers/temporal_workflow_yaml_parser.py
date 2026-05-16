"""
parsers/temporal_workflow_yaml_parser.py — Parser para Temporal.io workflow manifests.

Lê arquivos `temporal-workflows.yaml` e produz:
  - Nós (:TemporalWorkflow:Implementation) para cada workflow definido
  - Arestas BELONGS_TO  (workflow → service)
  - Arestas HAS_WORKFLOW (service → workflow)
  - Arestas IMPLEMENTS   (workflow → spec) para cada spec_id listado
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from app.core.parsers.base import BaseCortexExtractor, EdgeData, NodeData, ParseResult

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig

_VALID_CATEGORIES = {"scheduled", "event-triggered", "sandbox"}
_VALID_STATUSES = {"active", "deprecated", "shadow"}
_DEFAULT_CATEGORY = "event-triggered"
_DEFAULT_STATUS = "active"
_FILENAME = "temporal-workflows.yaml"


class TemporalWorkflowYamlParser(BaseCortexExtractor):
    """
    Parser built-in para manifestos de Temporal.io workflows.

    Produz nós (:TemporalWorkflow:Implementation) a partir de arquivos YAML
    no formato esperado pelo Cortex Context.
    """

    extractor_key = "builtin.temporal_workflow_yaml"

    def can_parse(self, file_path: Path) -> bool:
        return file_path.name == _FILENAME

    def parse(self, file_path: Path, dimension_config: "DimensionConfig | None" = None) -> ParseResult:
        try:
            raw = yaml.safe_load(file_path.read_text(encoding="utf-8")) or {}
        except Exception:
            return ParseResult()

        service_id = raw.get("service_id")
        if not service_id:
            return ParseResult()

        workflows_raw = raw.get("workflows") or []
        nodes: list[NodeData] = []
        edges: list[EdgeData] = []

        for wf in workflows_raw:
            if not isinstance(wf, dict):
                continue

            wf_id = wf.get("id")
            if not wf_id:
                continue

            category = wf.get("category") or _DEFAULT_CATEGORY
            if category not in _VALID_CATEGORIES:
                category = _DEFAULT_CATEGORY

            status = wf.get("status") or _DEFAULT_STATUS
            if status not in _VALID_STATUSES:
                status = _DEFAULT_STATUS

            properties: dict = {
                "id": wf_id,
                "name": wf.get("name", wf_id),
                "category": category,
                "status": status,
                "service_id": service_id,
            }

            for optional in ("schedule", "mode_resolver", "replaces"):
                value = wf.get(optional)
                if value is not None:
                    properties[optional] = value

            spec_ids = wf.get("spec_ids") or []

            nodes.append(NodeData(
                node_labels=["TemporalWorkflow", "Implementation"],
                node_id=wf_id,
                properties=properties,
            ))
            edges.append(EdgeData(from_id=wf_id, to_id=service_id, relationship="BELONGS_TO"))
            edges.append(EdgeData(from_id=service_id, to_id=wf_id, relationship="HAS_WORKFLOW"))

            for spec_id in spec_ids:
                edges.append(EdgeData(from_id=wf_id, to_id=spec_id, relationship="IMPLEMENTS"))

        return ParseResult(nodes=nodes, edges=edges)
