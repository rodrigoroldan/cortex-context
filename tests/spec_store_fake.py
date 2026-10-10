"""
tests/spec_store_fake.py — SpecGraphStore em memória, para testar a orquestração da ingestão de specs
(app/ingestor/spec_pipeline.py) sem Neo4j.

Reproduz, em Python, a semântica de cada operação do store real (as queries Cypher em si são cobertas
em tests/test_spec_pipeline.py::TestSpecGraphStoreCypher). Todo método que escreve registra uma entrada
em `self.calls`, o que permite asserções de ORDEM (ex: chunks velhos apagados antes do upsert).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.parsers.base import EdgeData, NodeData
from app.core.spec_issues import MappedIssue

_ISSUE_KEYS = ("has_issue", "issue_number", "issue_url", "issue_state", "issue_state_reason", "issue_updated_at")
_FOLDER_KEYS = ("has_folder", "files", "files_str", "source_folder", "primary_file", "file_path", "repos", "repos_str")
LONG_AGO = datetime(2020, 1, 1, tzinfo=timezone.utc)


def _is_legacy_chunk(chunk: dict) -> bool:
    """Chunk do parser antigo: sem chunk_source e id "{pai}__chunk_{i}"."""
    return chunk.get("chunk_source") is None and chunk["id"] == f"{chunk.get('parent_id')}__chunk_{chunk.get('chunk_index')}"


class InMemorySpecStore:
    def __init__(self) -> None:
        self.nodes: dict[str, dict] = {}
        self.chunks: dict[str, dict] = {}
        self.edges: list[EdgeData] = []
        self.calls: list[tuple] = []
        self.services: dict[str, str] = {}  # Service.repo -> id do Service
        self.affects: dict[tuple[str, str], bool] = {}  # (spec, service) -> criada por rebuild_affects (tem created_at)

    @staticmethod
    def _now() -> datetime:
        # Sempre posterior ao started_at do orquestrador, sem depender da resolução do relógio.
        return datetime.now(tz=timezone.utc) + timedelta(seconds=5)

    def age(self, days: int = 1) -> None:
        """Faz passar o tempo entre duas execuções: tudo que já foi gravado fica `days` dias mais velho."""
        delta = timedelta(days=days)
        for item in [*self.nodes.values(), *self.chunks.values()]:
            item["ingested_at"] = item["ingested_at"] - delta

    # -- seed de estado prévio ----------------------------------------------

    def seed_node(self, node_id: str, *, ingested_at: datetime = LONG_AGO, **props) -> dict:
        self.nodes[node_id] = {"id": node_id, "ingested_at": ingested_at, **props}
        return self.nodes[node_id]

    def seed_service(self, service_id: str, repo: str) -> None:
        self.services[repo] = service_id

    def seed_chunk(self, chunk_id: str, parent_id: str, *, ingested_at: datetime = LONG_AGO, **props) -> dict:
        self.chunks[chunk_id] = {"id": chunk_id, "parent_id": parent_id, "ingested_at": ingested_at, **props}
        return self.chunks[chunk_id]

    def chunk_ids(self, parent_id: str | None = None) -> set[str]:
        return {cid for cid, c in self.chunks.items() if parent_id is None or c.get("parent_id") == parent_id}

    def call_names(self) -> list[str]:
        return [c[0] for c in self.calls]

    # -- escrita -------------------------------------------------------------

    async def upsert_spec_nodes(self, nodes: list[NodeData]) -> int:
        self.calls.append(("upsert_spec_nodes", [n.node_id for n in nodes]))
        for n in nodes:
            node = self.nodes.setdefault(n.node_id, {"id": n.node_id})
            for k, v in n.properties.items():
                if v is None:  # `SET n += {k: null}` remove a propriedade no Neo4j
                    node.pop(k, None)
                else:
                    node[k] = v
            node["ingested_at"] = self._now()
        return len(nodes)

    async def upsert_edges(self, edges: list[EdgeData]) -> int:
        self.calls.append(("upsert_edges", len(edges)))
        self.edges.extend(edges)
        return len(edges)

    async def upsert_chunks(self, chunks: list[NodeData]) -> int:
        self.calls.append(("upsert_chunks", [c.node_id for c in chunks]))
        for c in chunks:
            self.chunks[c.node_id] = {**c.properties, "ingested_at": self._now()}
        return len(chunks)

    async def upsert_issue_nodes(self, mapped: list[MappedIssue]) -> int:
        self.calls.append(("upsert_issue_nodes", [m.node_id for m in mapped]))
        for m in mapped:
            node = self.nodes.setdefault(m.node_id, {"id": m.node_id})
            for k, v in m.issue_props.items():
                if v is None:
                    node.pop(k, None)
                else:
                    node[k] = v
            if not node.get("has_folder"):  # a pasta manda em title/summary/status
                node.update(m.doc_props)
            node["ingested_at"] = self._now()
        return len(mapped)

    # -- remoção -------------------------------------------------------------

    async def delete_folder_chunks(self, parent_ids: list[str]) -> int:
        self.calls.append(("delete_folder_chunks", list(parent_ids)))
        doomed = [
            cid
            for cid, c in self.chunks.items()
            if c.get("parent_id") in parent_ids and (c.get("chunk_source") == "folder" or _is_legacy_chunk(c))
        ]
        for cid in doomed:
            del self.chunks[cid]
        return len(doomed)

    async def delete_chunks(self, parent_ids: list[str], source: str | None = None) -> int:
        self.calls.append(("delete_chunks", list(parent_ids), source))
        doomed = [
            cid
            for cid, c in self.chunks.items()
            if c.get("parent_id") in parent_ids and (source is None or c.get("chunk_source") == source)
        ]
        for cid in doomed:
            del self.chunks[cid]
        return len(doomed)

    async def delete_nodes(self, ids: list[str]) -> int:
        self.calls.append(("delete_nodes", list(ids)))
        for cid in [cid for cid, c in self.chunks.items() if c.get("parent_id") in ids]:
            del self.chunks[cid]
        removed = [i for i in ids if i in self.nodes]
        for i in removed:
            del self.nodes[i]
        for key in [k for k in self.affects if k[0] in ids]:  # DETACH DELETE leva as arestas
            del self.affects[key]
        return len(removed)

    async def strip_folder_data(self, ids: list[str]) -> int:
        self.calls.append(("strip_folder_data", list(ids)))
        for i in ids:
            for k in _FOLDER_KEYS:
                self.nodes[i].pop(k, None)
        return len(ids)

    async def strip_issue_data(self, ids: list[str]) -> int:
        self.calls.append(("strip_issue_data", list(ids)))
        for i in ids:
            for k in _ISSUE_KEYS:
                self.nodes[i].pop(k, None)
        return len(ids)

    async def purge_stale_chunks(self, source: str, started_at: datetime) -> int:
        self.calls.append(("purge_stale_chunks", source))
        doomed = [cid for cid, c in self.chunks.items() if c.get("chunk_source") == source and c["ingested_at"] < started_at]
        for cid in doomed:
            del self.chunks[cid]
        return len(doomed)

    async def purge_legacy_chunks(self) -> int:
        self.calls.append(("purge_legacy_chunks",))
        doomed = [
            cid
            for cid, c in self.chunks.items()
            if _is_legacy_chunk(c) and self.nodes.get(c.get("parent_id"), {}).get("has_folder")
        ]
        for cid in doomed:
            del self.chunks[cid]
        return len(doomed)

    async def rebuild_affects(self, spec_ids: list[str] | None = None) -> int:
        self.calls.append(("rebuild_affects", None if spec_ids is None else list(spec_ids)))
        for key in [k for k, ours in self.affects.items() if ours and (spec_ids is None or k[0] in spec_ids)]:
            del self.affects[key]  # só as que o mecanismo criou; as manuais ficam
        created = 0
        for node in self.nodes.values():
            if spec_ids is not None and node["id"] not in spec_ids:
                continue
            for repo in node.get("repos") or []:
                if repo in self.services:
                    self.affects.setdefault((node["id"], self.services[repo]), True)
                    created += 1
        return created

    # -- consulta ------------------------------------------------------------

    async def list_folder_names(self) -> set[str]:
        return {n["source_folder"] for n in self.nodes.values() if n.get("has_folder") and n.get("source_folder")}

    async def find_nodes_by_folder(self, names: list[str]) -> list[dict]:
        return [
            {
                "id": n["id"],
                "source_folder": n.get("source_folder"),
                "has_issue": bool(n.get("has_issue")),
                "issue_number": n.get("issue_number"),
            }
            for n in self.nodes.values()
            if n.get("source_folder") in names
        ]

    async def find_prunable_folder_nodes(self, started_at: datetime, root_prefix: str, skip_folders: list[str]) -> list[str]:
        return [
            n["id"]
            for n in self.nodes.values()
            if (n.get("has_folder") is True or str(n.get("file_path") or "").startswith(root_prefix))
            and not n.get("has_issue")
            and n["ingested_at"] < started_at
            and (n.get("source_folder") or "") not in skip_folders
        ]

    async def find_issue_nodes_not_in(self, keep_ids: list[str]) -> list[dict]:
        return [
            {"id": n["id"], "has_folder": bool(n.get("has_folder"))}
            for n in self.nodes.values()
            if n.get("has_issue") and n["id"] not in keep_ids
        ]

    async def find_issue_nodes_by_number(self, number: int, exclude_id: str | None = None) -> list[dict]:
        return [
            {"id": n["id"], "has_folder": bool(n.get("has_folder"))}
            for n in self.nodes.values()
            if n.get("issue_number") == number and n["id"] != exclude_id
        ]
