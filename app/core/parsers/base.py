"""
parsers/base.py — Classe abstrata para todos os dimension parsers do Cortex.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class NodeData:
    """Dados de um nó extraído por um parser, pronto para upsert no Neo4j."""
    node_label: str           # ex: "Spec", "Service"
    node_id: str              # id único do nó (ex: "spec-070", "service-bff")
    properties: dict          # campos do nó (incluindo o 'id')


@dataclass
class EdgeData:
    """Dados de uma aresta criada diretamente pelo parser (não pelo relationship_builder)."""
    from_id: str
    to_id: str
    relationship: str
    properties: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    """Resultado completo de um parse: nós + arestas diretas."""
    nodes: list[NodeData] = field(default_factory=list)
    edges: list[EdgeData] = field(default_factory=list)


class DimensionParser(ABC):
    """
    Interface base para todos os parsers de dimension.

    Um parser é responsável por:
    1. Determinar se consegue processar um arquivo (can_parse)
    2. Extrair NodeData (e opcionalmente EdgeData) de um arquivo (parse)
    """

    @abstractmethod
    def can_parse(self, file_path: Path) -> bool:
        """Retorna True se este parser sabe processar o arquivo dado."""
        ...

    @abstractmethod
    def parse(self, file_path: Path) -> ParseResult:
        """
        Parseia o arquivo e retorna nós + arestas.

        Args:
            file_path: Caminho absoluto do arquivo a ser parseado.

        Returns:
            ParseResult com listas de NodeData e EdgeData.
        """
        ...
