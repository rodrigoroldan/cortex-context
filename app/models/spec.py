from __future__ import annotations

from pydantic import BaseModel


class SpecNode(BaseModel):
    id: str                          # "spec-070"
    number: int
    title: str
    status: str                      # completed | in-progress | planned | todo
    labels: list[str]
    summary: str
    repos: list[str]
    file_path: str

    @property
    def labels_str(self) -> str:
        """String concatenada dos labels — usada no índice FTS."""
        return " ".join(self.labels)

    def to_neo4j_props(self) -> dict:
        d = self.model_dump()
        d["labels_str"] = self.labels_str
        return d


class SpecEdge(BaseModel):
    from_id: str
    to_id: str
    relationship: str  # SUPERSEDES | EVOLVES_FROM | DEPENDS_ON | RELATED_TO | IMPLEMENTS
    weight: float = 1.0


class SpecGraph(BaseModel):
    nodes: list[SpecNode]
    edges: list[SpecEdge]
