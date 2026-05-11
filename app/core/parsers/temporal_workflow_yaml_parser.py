"""
parsers/temporal_workflow_yaml_parser.py — Parser de dimension :TemporalWorkflow.

Extrai dados de temporal-workflows.yaml mantido no repositório de cada serviço.
Produz nós :TemporalWorkflow e arestas diretas:
  - (:TemporalWorkflow)-[:BELONGS_TO]->(:Service)
  - (:Service)-[:HAS_WORKFLOW]->(:TemporalWorkflow)
  - (:TemporalWorkflow)-[:IMPLEMENTS]->(:Spec)  [para cada spec_id declarado]
"""
from __future__ import annotations

import logging
from pathlib import Path

import yaml

from app.core.parsers.base import DimensionParser, EdgeData, NodeData, ParseResult

logger = logging.getLogger(__name__)

# Valores permitidos nos campos de categoria e status
_VALID_CATEGORIES = {"scheduled", "event-triggered", "sandbox"}
_VALID_STATUSES = {"active", "deprecated"}


class TemporalWorkflowYamlParser(DimensionParser):
    """
    Parser para temporal-workflows.yaml.

    Formato esperado do arquivo:
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
    """

    def can_parse(self, file_path: Path) -> bool:
        return file_path.name == "temporal-workflows.yaml"

    def parse(self, file_path: Path) -> ParseResult:
        result = ParseResult()

        try:
            content = file_path.read_text(encoding="utf-8")
            data = yaml.safe_load(content)
        except Exception as exc:
            logger.warning("Falha ao ler/parsear %s: %s", file_path, exc)
            return result

        if not isinstance(data, dict):
            logger.warning("%s: YAML raiz não é um dict", file_path)
            return result

        service_id: str = data.get("service_id", "").strip()
        if not service_id:
            logger.warning("%s: campo 'service_id' ausente ou vazio", file_path)
            return result

        workflows = data.get("workflows", [])
        if not isinstance(workflows, list):
            logger.warning("%s: campo 'workflows' não é uma lista", file_path)
            return result

        for entry in workflows:
            if not isinstance(entry, dict):
                continue

            workflow_id = (entry.get("id") or "").strip()
            name = (entry.get("name") or "").strip()
            category = (entry.get("category") or "").strip()
            status = (entry.get("status") or "active").strip()

            if not workflow_id or not name:
                logger.warning("%s: workflow sem 'id' ou 'name', pulando: %s", file_path, entry)
                continue

            if category not in _VALID_CATEGORIES:
                logger.warning("%s: categoria inválida '%s' no workflow %s", file_path, category, workflow_id)
                category = "event-triggered"

            if status not in _VALID_STATUSES:
                status = "active"

            schedule = entry.get("schedule") or None
            mode_resolver = entry.get("mode_resolver") or None
            replaces = entry.get("replaces") or None
            spec_ids: list[str] = [
                s for s in (entry.get("spec_ids") or []) if isinstance(s, str) and s.strip()
            ]

            props = {
                "id": workflow_id,
                "name": name,
                "category": category,
                "status": status,
                "service_id": service_id,
            }
            if schedule is not None:
                props["schedule"] = schedule
            if mode_resolver is not None:
                props["mode_resolver"] = mode_resolver
            if replaces is not None:
                props["replaces"] = replaces
            if spec_ids:
                props["spec_ids"] = spec_ids

            result.nodes.append(NodeData(
                node_label="TemporalWorkflow",
                node_id=workflow_id,
                properties=props,
            ))

            # Aresta BELONGS_TO workflow → service
            result.edges.append(EdgeData(
                from_id=workflow_id,
                to_id=service_id,
                relationship="BELONGS_TO",
            ))

            # Aresta HAS_WORKFLOW service → workflow
            result.edges.append(EdgeData(
                from_id=service_id,
                to_id=workflow_id,
                relationship="HAS_WORKFLOW",
            ))

            # Arestas IMPLEMENTS para cada spec declarada
            for spec_id in spec_ids:
                result.edges.append(EdgeData(
                    from_id=workflow_id,
                    to_id=spec_id.strip(),
                    relationship="IMPLEMENTS",
                ))

        logger.info(
            "temporal_workflow_yaml_parser: %d workflows extraídos de %s (service: %s)",
            len(result.nodes), file_path.name, service_id,
        )
        return result
