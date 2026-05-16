"""
ingestor/relationship_builder.py — Pós-processamento de arestas cross-dimension.

Gera arestas (:Spec)-[:AFFECTS]->(:Service) cruzando:
  - Spec.repos[] — lista de repos mencionados na spec
  - Service.repo  — repo canônico do serviço

Esse pós-processamento ocorre após todos os nós (Spec e Service) já estarem
upsertados no grafo, para garantir que ambos os endpoints existam.
"""
from __future__ import annotations

import logging

from app.core.parsers.base import EdgeData, NodeData

logger = logging.getLogger(__name__)

# Fallback estático: repos bem conhecidos → service_id canônico.
# Usado apenas quando um repo não aparece no agents.md do workspace.
# Cada instalação deve sobrescrever via Service nodes com a propriedade `repo`.
_REPO_TO_SERVICE_ID: dict[str, str] = {}


def _build_service_index(service_nodes: list[NodeData]) -> dict[str, str]:
    """
    Constrói um índice repo_name → service_id a partir dos Service nodes.

    Usa o campo 'repo' de cada ServiceNode. Para repos não presentes nos service_nodes
    (ex: IaC sem agents.md), usa o mapeamento estático _REPO_TO_SERVICE_ID como fallback.
    """
    index: dict[str, str] = dict(_REPO_TO_SERVICE_ID)  # starts with optional static fallback map
    for node in service_nodes:
        repo = node.properties.get("repo", "")
        if repo and node.node_id:
            index[repo] = node.node_id
    return index


def build_affects_edges(
    spec_nodes: list[NodeData],
    service_nodes: list[NodeData],
) -> list[EdgeData]:
    """
    Gera arestas (:Spec)-[:AFFECTS]->(:Service) a partir de Spec.repos[].

    Para cada spec, verifica cada repo em props["repos"] e tenta mapear
    para um service_id conhecido. Se encontrar, cria a aresta AFFECTS.

    Args:
        spec_nodes: NodeData de todos os nós :Spec (devem ter props["repos"])
        service_nodes: NodeData de todos os nós :Service (devem ter props["repo"])

    Returns:
        Lista de EdgeData de arestas AFFECTS únicas
    """
    service_index = _build_service_index(service_nodes)
    known_service_ids = {node.node_id for node in service_nodes}

    edges: list[EdgeData] = []
    seen: set[tuple[str, str]] = set()

    for spec_node in spec_nodes:
        spec_id = spec_node.node_id
        repos: list[str] = spec_node.properties.get("repos", [])

        for repo in repos:
            service_id = service_index.get(repo)
            if service_id is None:
                continue
            # Só cria aresta se o Service node foi upsertado nesta execução
            # OU está no mapeamento estático (pode já existir no grafo)
            if service_id not in known_service_ids and service_id not in set(_REPO_TO_SERVICE_ID.values()):
                continue

            key = (spec_id, service_id)
            if key not in seen:
                seen.add(key)
                edges.append(
                    EdgeData(
                        from_id=spec_id,
                        to_id=service_id,
                        relationship="AFFECTS",
                        properties={},
                    )
                )

    logger.info(
        "relationship_builder: %d AFFECTS edges geradas de %d specs × %d services",
        len(edges),
        len(spec_nodes),
        len(service_nodes),
    )
    return edges
