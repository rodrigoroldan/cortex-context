"""
core/parsers/manifest.py — Schema Pydantic para Ingest Manifests com suporte a domain_id.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class ManifestNode(BaseModel):
    """Nó a ser inserido/atualizado no grafo."""

    node_id: str = Field(
        default="",
        description="ID único do nó (ex: 'spec-149', 'service-bff', 'workflow-payment', ou 'draft:feat/auth:spec-149')"
    )
    node_labels: list[str] = Field(
        min_length=1,
        description=(
            "Labels do nó. O primeiro elemento é a label primária usada no MERGE. "
            "Ex: ['Spec', 'Intent'], ['Service', 'System']"
        ),
    )
    domain_id: str | None = Field(
        default=None,
        description="Identificador do domínio do nó. Se ausente, herda do IngestManifest."
    )
    branch: str | None = Field(
        default=None,
        description="Branch Git associada ao nó (ex: 'feat/auth'). Se ausente, herda do IngestManifest."
    )
    status: str | None = Field(
        default=None,
        description="Status do nó ('draft', 'canonical', 'active', etc.). Default: 'draft' se is_draft=True else 'canonical'."
    )
    is_draft: bool | None = Field(
        default=None,
        description="Indica se é um nó especulativo (draft). Se ausente, herda do IngestManifest."
    )
    canonical_id: str | None = Field(
        default=None,
        description="ID canônico base (sem prefixo draft:). Se ausente, derivado de node_id."
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
    domain_id: str = Field(
        default="default",
        description="ID do domínio multi-tenant corporativo para isolamento.",
    )
    branch: str = Field(
        default="main",
        description="Nome da branch Git para ingestão (ex: 'main', 'feat/payment-v2').",
    )
    draft: bool = Field(
        default=False,
        description="Se True, marca todos os nós como especulativos (is_draft=True, status='draft').",
    )
    dry_run: bool = Field(
        default=False,
        description="Se True, executa apenas validação sem persistir no Neo4j (modo PR Linter).",
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
    domain_id: str = "default"
    nodes_upserted: int
    edges_upserted: int
    message: str
    dry_run: bool = False
    linter_status: str = "passed"
    validation_errors: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
