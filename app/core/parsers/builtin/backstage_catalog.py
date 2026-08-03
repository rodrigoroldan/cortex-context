"""
builtin/backstage_catalog.py — Built-in parser for Backstage catalog-info.yaml files.

Feature 10: Backstage Catalog Parser
Extracts services, systems, APIs, and relationships from Backstage entity manifests.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from app.core.parsers.base import BaseCortexExtractor, EdgeData, NodeData, ParseResult

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig


def _parse_entity_ref(ref: str, default_kind: str = "service") -> tuple[str, str, str]:
    """
    Parse Backstage entity reference string into (kind, name, node_id).
    Supports formats:
      - kind:namespace/name (e.g., component:default/auth-service, api:default/user-api)
      - kind:name (e.g., component:auth-service, api:user-api)
      - namespace/name (e.g., default/auth-service)
      - name (e.g., auth-service)
    """
    ref = str(ref).strip()
    if not ref:
        return (default_kind, "", f"{default_kind}-")

    if ":" in ref:
        parts = ref.split(":", 1)
        kind_part = parts[0].strip().lower()
        rest = parts[1].strip()
    else:
        kind_part = default_kind.lower()
        rest = ref

    if "/" in rest:
        name_part = rest.split("/", 1)[1].strip()
    else:
        name_part = rest.strip()

    if kind_part in ("component", "service"):
        norm_kind = "service"
        node_id = f"service-{name_part}"
    elif kind_part == "api":
        norm_kind = "api"
        node_id = f"api-{name_part}"
    elif kind_part == "system":
        norm_kind = "system"
        node_id = f"system-{name_part}"
    else:
        norm_kind = kind_part
        node_id = f"{kind_part}-{name_part}"

    return (norm_kind, name_part, node_id)


class BackstageCatalogParser(BaseCortexExtractor):
    """Built-in parser for Backstage catalog-info.yaml files (Feature 10)."""

    extractor_key = "builtin.backstage_catalog"

    def can_parse(self, file_path: Path) -> bool:
        return file_path.name in ("catalog-info.yaml", "catalog-info.yml") or (
            file_path.suffix in (".yaml", ".yml") and "apiVersion" in file_path.read_text(errors="ignore")
        )

    def parse(self, file_path: Path, dimension_config: DimensionConfig) -> ParseResult:
        try:
            content = file_path.read_text(encoding="utf-8")
            raw_docs = list(yaml.safe_load_all(content))
            docs = [d for d in raw_docs if isinstance(d, dict)]
        except Exception:
            return ParseResult()

        if not docs:
            return ParseResult()

        all_nodes: list[NodeData] = []
        all_edges: list[EdgeData] = []
        pillar = dimension_config.pillar or "System"

        for data in docs:
            kind = data.get("kind", "")
            metadata = data.get("metadata", {})
            spec = data.get("spec", {})

            if not isinstance(metadata, dict) or not isinstance(spec, dict):
                continue

            name = metadata.get("name", "")
            if not kind or not name:
                continue

            domain_id = metadata.get("domain", metadata.get("namespace", "global"))

            kind_lower = kind.lower()
            if kind_lower in ("component", "service"):
                primary_label = "Service"
                node_id = f"service-{name}"
            elif kind_lower == "api":
                primary_label = "API"
                node_id = f"api-{name}"
            elif kind_lower == "system":
                primary_label = "System"
                node_id = f"system-{name}"
            else:
                primary_label = kind.capitalize()
                node_id = f"{kind_lower}-{name}"

            node_props = {
                "id": node_id,
                "title": metadata.get("title", name),
                "name": name,
                "kind": kind,
                "domain_id": domain_id,
                "owner": spec.get("owner", metadata.get("owner", "unknown")),
                "lifecycle": spec.get("lifecycle", "production"),
                "system": spec.get("system", ""),
                "pillar": pillar,
                "file_path": str(file_path),
            }

            labels = list(dict.fromkeys([primary_label, pillar]))

            node = NodeData(
                node_labels=labels,
                node_id=node_id,
                properties=node_props,
            )
            all_nodes.append(node)

            # Parse dependencies: spec.dependsOn -> DEPENDS_ON
            depends_on = spec.get("dependsOn", [])
            if isinstance(depends_on, list):
                for dep in depends_on:
                    if not dep:
                        continue
                    _, _, target_node_id = _parse_entity_ref(dep, default_kind="service")
                    all_edges.append(
                        EdgeData(
                            from_id=node_id,
                            to_id=target_node_id,
                            relationship="DEPENDS_ON",
                            properties={"domain_id": domain_id},
                        )
                    )

            # Parse API relationship: spec.providesApis -> EXPOSES
            provides_apis = spec.get("providesApis", [])
            if isinstance(provides_apis, list):
                for api_ref in provides_apis:
                    if not api_ref:
                        continue
                    _, _, api_node_id = _parse_entity_ref(api_ref, default_kind="api")
                    all_edges.append(
                        EdgeData(
                            from_id=node_id,
                            to_id=api_node_id,
                            relationship="EXPOSES",
                            properties={"domain_id": domain_id},
                        )
                    )

            # Parse API relationship: spec.consumesApis -> CALLS
            consumes_apis = spec.get("consumesApis", [])
            if isinstance(consumes_apis, list):
                for api_ref in consumes_apis:
                    if not api_ref:
                        continue
                    _, _, api_node_id = _parse_entity_ref(api_ref, default_kind="api")
                    all_edges.append(
                        EdgeData(
                            from_id=node_id,
                            to_id=api_node_id,
                            relationship="CALLS",
                            properties={"domain_id": domain_id},
                        )
                    )

        return ParseResult(nodes=all_nodes, edges=all_edges)
