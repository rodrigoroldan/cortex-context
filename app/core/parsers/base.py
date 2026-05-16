"""
parsers/base.py — Interfaces base do Cortex Context.

Ontologia I.S.I.R — 4 pilares universais:
  Intent         — O "Por quê". Specs, Requisitos, Épicos, User Stories.
  System         — O "Como" (alto nível). Serviços, Bancos, APIs, ADRs.
  Implementation — O "O quê" (concreto). Módulos, Funções, Componentes, Workflows.
  Runtime        — O "Mundo real". Alertas, Traces, Deployments, Incidentes.

No Neo4j cada nó recebe multi-labels: a label do pilar + a label da entidade.
  Ex: (:Intent:Spec), (:System:Service), (:Implementation:Workflow), (:Runtime:Alert)

Para criar um parser/extractor customizado (plugin):
  1. Herde de BaseCortexExtractor
  2. Defina extractor_key = "minha.chave"
  3. Implemente can_parse() e parse()
  4. Coloque o arquivo em plugins/ — o Cortex o descobre automaticamente
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig

# ─── Pilares canônicos ─────────────────────────────────────────────────────────

PILLARS = {"Intent", "System", "Implementation", "Runtime"}

# ─── Arestas canônicas (agnósticas ao produto) ────────────────────────────────

CANONICAL_EDGES = {
    # Intent ↔ Intent
    "DEPENDS_ON",        # Spec A requer Spec B
    "SUPERSEDES",        # Spec A substitui Spec B
    "EVOLVES_FROM",      # Spec A é evolução de Spec B
    "RELATED_TO",        # Referência cruzada genérica
    # Intent → System / Implementation
    "AFFECTS",           # Spec afeta Serviço / Componente
    "IMPLEMENTS",        # Entidade implementa uma Spec
    # System ↔ System / Implementation
    "EXPOSES",           # Serviço expõe EndpointAPI
    "DEPENDS_ON",        # (reusado) Serviço depende de Serviço
    # Implementation
    "CALLS",             # Função A chama Função B
    "IMPORTS",           # Módulo A importa Módulo B
    "TRIGGERS",          # Workflow A dispara Workflow B
    "MUTATES",           # Operação muta DomainEntity
    # Runtime
    "OBSERVED_IN",       # Alerta observado em Serviço
    "DEPLOYED_TO",       # Artefato deployado em Ambiente
    # Vector RAG
    "CHUNK_OF",          # DocumentChunk pertence a um nó pai (Spec, ADR, etc.)
}


# ─── Estruturas de dados ───────────────────────────────────────────────────────


@dataclass
class NodeData:
    """
    Representa um nó pronto para upsert no Neo4j.

    node_labels: lista de labels a aplicar no nó.
      - node_labels[0] é a label primária usada no MERGE (ex: "Spec").
      - As demais são aplicadas via SET (ex: "Intent").
    Exemplo: NodeData(node_labels=["Spec", "Intent"], node_id="spec-070", ...)
    """
    node_labels: list[str]   # ex: ["Spec", "Intent"]
    node_id: str             # id único (ex: "spec-070", "service-bff")
    properties: dict         # propriedades do nó (sempre inclui 'id')

    @property
    def primary_label(self) -> str:
        """Label primária usada no MERGE (primeira da lista)."""
        return self.node_labels[0] if self.node_labels else "Node"

    @property
    def pillar_label(self) -> str | None:
        """Label do pilar I.S.I.R (segunda da lista, se presente)."""
        return self.node_labels[1] if len(self.node_labels) > 1 else None


@dataclass
class EdgeData:
    """Representa uma aresta entre dois nós no grafo."""
    from_id: str
    to_id: str
    relationship: str        # Use CANONICAL_EDGES quando possível
    properties: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    """Resultado de extração: nós + arestas."""
    nodes: list[NodeData] = field(default_factory=list)
    edges: list[EdgeData] = field(default_factory=list)


def make_chunk_node(
    parent_id: str,
    chunk_index: int,
    content: str,
    pillar: str,
    *,
    embedding: list[float] | None = None,
) -> NodeData:
    """
    Cria um NodeData representando um DocumentChunk filho de um nó pai.

    O nó recebe labels ["DocumentChunk", pillar] para permitir queries
    cross-pilar no índice vetorial.

    Args:
        parent_id:   ID do nó pai (ex: "spec-070")
        chunk_index: Índice sequencial do chunk (0-based)
        content:     Texto do chunk (pronto para embedding)
        pillar:      Pilar I.S.I.R do pai (ex: "Intent")
        embedding:   Vetor float opcional; None até que o embedder processe.
    """
    chunk_id = f"{parent_id}__chunk_{chunk_index}"
    props: dict = {
        "id": chunk_id,
        "parent_id": parent_id,
        "chunk_index": chunk_index,
        "content": content,
        "pillar": pillar,
    }
    if embedding is not None:
        props["embedding"] = embedding
    return NodeData(
        node_labels=["DocumentChunk", pillar],
        node_id=chunk_id,
        properties=props,
    )


# ─── Interface base ────────────────────────────────────────────────────────────


class BaseCortexExtractor(ABC):
    """
    Interface base para todos os extractors do Cortex Context.

    Um extractor lê uma fonte de dados (arquivo, API, DB) e produz
    NodeData + EdgeData para ingestão no Knowledge Graph.

    Para criar um plugin customizado:
      1. Herde desta classe
      2. Defina extractor_key (único, ex: "jira.epic")
      3. Implemente can_parse() e parse()
      4. Coloque o arquivo em plugins/ — o Cortex descobre automaticamente
    """

    extractor_key: ClassVar[str] = ""

    @abstractmethod
    def can_parse(self, file_path: Path) -> bool:
        """Retorna True se este extractor sabe processar o arquivo dado."""
        ...

    @abstractmethod
    def parse(self, file_path: Path, dimension_config: "DimensionConfig") -> ParseResult:
        """
        Extrai nós e arestas de um arquivo.

        Args:
            file_path: Caminho absoluto do arquivo a ser processado.
            dimension_config: Config da dimensão (pillar, node_label, etc.)

        Returns:
            ParseResult com NodeData e EdgeData.
        """
        ...


# Alias backward-compatible para código legado
DimensionParser = BaseCortexExtractor
