"""
dimension_loader.py — Carrega e valida dimension specs YAML.

Lê arquivos .yaml em app/dimensions/, valida campos obrigatórios e retorna
uma lista de DimensionConfig prontos para uso pelo ingest pipeline.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import yaml

logger = logging.getLogger(__name__)


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
    dimension: str          # ex: "spec", "service"
    node_label: str         # ex: "Spec", "Service"
    parser: str             # chave no ParserRegistry
    source_patterns: list[str]
    fields: list[FieldConfig] = field(default_factory=list)
    relationships: list[RelationshipConfig] = field(default_factory=list)
    indexes: list[IndexConfig] = field(default_factory=list)


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
    for required_key in ("dimension", "node_label", "parser"):
        if not raw.get(required_key):
            logger.error("Campo obrigatório '%s' ausente em %s", required_key, yaml_path)
            return None

    # source_pattern ou source_patterns
    patterns: list[str] = []
    if "source_pattern" in raw:
        patterns = [raw["source_pattern"]]
    elif "source_patterns" in raw:
        patterns = raw["source_patterns"]

    return DimensionConfig(
        dimension=raw["dimension"],
        node_label=raw["node_label"],
        parser=raw["parser"],
        source_patterns=patterns,
        fields=[_parse_field(f) for f in raw.get("fields", [])],
        relationships=[_parse_relationship(r) for r in raw.get("relationships", [])],
        indexes=[_parse_index(i) for i in raw.get("indexes", [])],
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
            logger.info("Dimension carregada: %s (node_label=%s, parser=%s)", dim_name, config.node_label, config.parser)

    return configs
