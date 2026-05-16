"""
core/parsers/manifest.py — Schema Pydantic para Ingest Manifests.

Um IngestManifest permite que o Cortex Ingestion Agent CLI local envie dados
pré-computados (derivados de git diff + análise de spec) diretamente ao servidor
via POST /api/v1/ingest/manifest, sem que o servidor precise parsear arquivos.

Isso separa:
  - "quem descobre os relacionamentos" → CLI agent local (no dev's machine)
  - "quem persiste no grafo"           → Cortex API (no servidor)

O manifesto é totalmente agnóstico ao produto. O CLI agent decide quais nós e
arestas criar; o servidor apenas persiste e adiciona propriedades bitemporais.

Exemplo de uso pelo CLI agent:
  manifest = IngestManifest(
      source="git-diff",
      commit_sha="abc123",
      nodes=[
          ManifestNode(
              node_id="spec-149",
              node_labels=["Spec", "Intent"],
              properties={"title": "Cortex v3.0", "status": "in-progress"}
          ),
          ManifestNode(
              node_id="service-cortex",
              node_labels=["Service", "System"],
              properties={"name": "cortex-context"}
          ),
      ],
      edges=[
          ManifestEdge(
              from_id="spec-149",
              to_id="service-cortex",
              relationship="IMPLEMENTS",
              properties={"files_changed": 12, "via": "git-diff"}
          ),
      ]
  )
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ManifestNode(BaseModel):
    """Nó a ser inserido/atualizado no grafo."""

    node_id: str = Field(
        description="ID único do nó (ex: 'spec-149', 'service-bff', 'workflow-payment')"
    )
    node_labels: list[str] = Field(
        min_length=1,
        description=(
            "Labels do nó. O primeiro elemento é a label primária usada no MERGE. "
            "Ex: ['Spec', 'Intent'], ['Service', 'System']"
        ),
    )
    properties: dict = Field(
        default_factory=dict,
        description="Propriedades do nó. 'id' é adicionado automaticamente se ausente.",
    )


class ManifestEdge(BaseModel):
    """Aresta direcional entre dois nós do grafo."""

    from_id: str = Field(description="ID do nó de origem")
    to_id: str = Field(description="ID do nó de destino")
    relationship: str = Field(
        description=(
            "Tipo da aresta. Use CANONICAL_EDGES quando possível: "
            "AFFECTS, IMPLEMENTS, DEPENDS_ON, EXPOSES, RELATED_TO, TRIGGERS, etc."
        )
    )
    properties: dict = Field(
        default_factory=dict,
        description="Propriedades da aresta (ex: {'via': 'git-diff', 'files_changed': 5})",
    )


class IngestManifest(BaseModel):
    """
    Manifesto de ingestão — payload completo para POST /api/v1/ingest/manifest.

    O Cortex API persiste todos os nós e arestas, adicionando propriedades
    bitemporais (ingested_at, valid_from) automaticamente.
    """

    source: str = Field(
        default="manual",
        description=(
            "Origem do manifesto. Ex: 'git-diff', 'cortex-agent', 'manual', 'ci-cd'. "
            "Armazenado como metadata, não tem impacto no comportamento."
        ),
    )
    commit_sha: str | None = Field(
        default=None,
        description=(
            "SHA do commit Git associado a esta ingestão. "
            "Propagado para a propriedade 'commit_sha' de todos os nós do manifesto."
        ),
    )
    nodes: list[ManifestNode] = Field(
        default_factory=list,
        description="Lista de nós a criar/atualizar no grafo.",
    )
    edges: list[ManifestEdge] = Field(
        default_factory=list,
        description="Lista de arestas a criar/atualizar no grafo.",
    )


class ManifestIngestResponse(BaseModel):
    """Resposta do endpoint POST /api/v1/ingest/manifest."""

    source: str
    commit_sha: str | None
    nodes_upserted: int
    edges_upserted: int
    message: str
