"""
dimension_loader.py — Carrega e valida dimension specs YAML.

Cada dimension YAML define:
  - pillar: Um dos 4 pilares I.S.I.R (Intent | System | Implementation | Runtime)
  - node_label: Label específica da entidade (ex: "Spec", "Service", "Workflow")
  - parser: Chave do extractor a usar (builtin.* ou plugin customizado)
  - source_type: filesystem | github_api | url | plugin
  - source_path: Caminho/URL/referência da fonte de dados

No Neo4j o nó recebe multi-labels: [node_label, pillar]
  Ex: pillar=Intent, node_label=Spec → MERGE (n:Spec) SET n:Intent
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)

# Pilares válidos da ontologia I.S.I.R
VALID_PILLARS = {"Intent", "System", "Implementation", "Runtime"}


@dataclass
class FieldConfig:
    name: str
    type: str
    description: str = ""
    required: bool = False
    derivation: str = ""


@dataclass
class RelationshipConfig:
    type: str
    direction: str  # outbound | inbound
    target_label: str = ""
    source_label: str = ""
    description: str = ""
    built_by: str = "parser"  # parser | relationship_builder


@dataclass
class IndexConfig:
    index_type: str  # constraint_unique | fulltext
    cypher: str
    name: str = ""
    property: str = ""
    properties: list[str] = field(default_factory=list)


@dataclass
class DimensionConfig:
    dimension: str           # ex: "spec", "service", "workflow"
    node_label: str          # ex: "Spec", "Service", "Workflow"
    pillar: str              # Intent | System | Implementation | Runtime
    parser: str              # chave no ParserRegistry (ex: "builtin.markdown_frontmatter")
    source_type: str         # filesystem | github_api | url | plugin
    source_path: str         # caminho/URL/referência da fonte
    source_patterns: list[str]
    fields: list[FieldConfig] = field(default_factory=list)
    relationships: list[RelationshipConfig] = field(default_factory=list)
    indexes: list[IndexConfig] = field(default_factory=list)
    extra: dict = field(default_factory=dict)  # campos adicionais livres

    @property
    def node_labels(self) -> list[str]:
        """Retorna a lista de labels multi-label: [entity_label, pillar_label]."""
        return [self.node_label, self.pillar]


def _parse_field(raw: dict) -> FieldConfig:
    return FieldConfig(
        name=raw["name"],
        type=raw.get("type", "string"),
        description=raw.get("description", ""),
        required=raw.get("required", False),
        derivation=raw.get("derivation", ""),
    )


def _parse_relationship(raw: dict) -> RelationshipConfig:
    return RelationshipConfig(
        type=raw["type"],
        direction=raw.get("direction", "outbound"),
        target_label=raw.get("target_label", ""),
        source_label=raw.get("source_label", ""),
        description=raw.get("description", ""),
        built_by=raw.get("built_by", "parser"),
    )


def _parse_index(raw: dict) -> IndexConfig:
    return IndexConfig(
        index_type=raw["type"],
        cypher=raw.get("cypher", ""),
        name=raw.get("name", ""),
        property=raw.get("property", ""),
        properties=raw.get("properties", []),
    )


def load_dimension(yaml_path: Path) -> DimensionConfig | None:
    """Carrega e valida um único dimension YAML. Retorna None se inválido."""
    try:
        raw = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Erro ao ler dimension YAML %s: %s", yaml_path, e)
        return None

    if not isinstance(raw, dict):
        logger.error("Dimension YAML inválido (não é dict): %s", yaml_path)
        return None

    # Campos obrigatórios
    for required_key in ("dimension", "node_label", "pillar", "parser"):
        if not raw.get(required_key):
            logger.error("Campo obrigatório '%s' ausente em %s", required_key, yaml_path)
            return None

    # Validar pillar
    pillar = raw["pillar"]
    if pillar not in VALID_PILLARS:
        logger.error(
            "Pilar inválido '%s' em %s. Use: %s",
            pillar,
            yaml_path,
            ", ".join(sorted(VALID_PILLARS)),
        )
        return None

    # source_pattern ou source_patterns (opcional)
    patterns: list[str] = []
    if "source_pattern" in raw:
        patterns = [raw["source_pattern"]]
    elif "source_patterns" in raw:
        patterns = raw["source_patterns"]

    # Reservar campos conhecidos; o restante vai em extra
    known_keys = {
        "dimension", "node_label", "pillar", "parser",
        "source_type", "source_path", "source_pattern", "source_patterns",
        "fields", "relationships", "indexes",
    }
    extra = {k: v for k, v in raw.items() if k not in known_keys}

    return DimensionConfig(
        dimension=raw["dimension"],
        node_label=raw["node_label"],
        pillar=raw["pillar"],
        parser=raw["parser"],
        source_type=raw.get("source_type", "filesystem"),
        source_path=raw.get("source_path", ""),
        source_patterns=patterns,
        fields=[_parse_field(f) for f in raw.get("fields", [])],
        relationships=[_parse_relationship(r) for r in raw.get("relationships", [])],
        indexes=[_parse_index(i) for i in raw.get("indexes", [])],
        extra=extra,
    )


def load_dimensions(dimensions_dir: Path, active_dimensions: list[str]) -> list[DimensionConfig]:
    """
    Carrega todos os dimension YAMLs listados em active_dimensions.

    Args:
        dimensions_dir: Diretório onde ficam os arquivos .yaml
        active_dimensions: Lista de nomes de dimensões a carregar (ex: ["spec", "service"])

    Returns:
        Lista de DimensionConfig válidos (inválidos são pulados com log de erro)
    """
    configs: list[DimensionConfig] = []

    for dim_name in active_dimensions:
        yaml_path = dimensions_dir / f"{dim_name}.yaml"
        if not yaml_path.exists():
            logger.error("Dimension YAML não encontrado: %s", yaml_path)
            continue
        config = load_dimension(yaml_path)
        if config is not None:
            configs.append(config)
            logger.info(
                "Dimension carregada: %s (pillar=%s, node_label=%s, parser=%s)",
                dim_name,
                config.pillar,
                config.node_label,
                config.parser,
            )

    return configs
