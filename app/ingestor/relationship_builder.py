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

# Mapeamento de alias curto → service_id completo
# Usado para resolver menções como "backend" ou "bff" no Spec.repos[]
_REPO_TO_SERVICE_ID: dict[str, str] = {
    "backend-role-organizado": "service-backend",
    "bff-role-organizado": "service-bff",
    "webview-role-organizado": "service-webview",
    "frontend-admin-role-organizado": "service-admin",
    "app-android-role-organizado": "service-android",
    "app-ios-role-organizado": "service-ios",
    "lambda-notifications-role-organizado": "service-lambda",
    "landing-role-organizado": "service-landing",
    "cortex-role-organizado": "service-cortex",
    "orchestrator-openclaw-role-organizado": "service-openclaw",
    "qa-taac-role-organizado": "service-qa-taac",
    "iac-proxmox-role-organizado": "service-iac-proxmox",
    "iac-aws-role-organizado": "service-iac-aws",
    "iac-cloudflare-role-organizado": "service-iac-cloudflare",
    "iac-gcp-role-organizado": "service-iac-gcp",
    "iac-observability-role-organizado": "service-iac-observability",
    "rods-role-organizado": "service-rods",
}


def _build_service_index(service_nodes: list[NodeData]) -> dict[str, str]:
    """
    Constrói um índice repo_name → service_id a partir dos Service nodes.

    Usa o campo 'repo' de cada ServiceNode. Para repos não presentes nos service_nodes
    (ex: IaC sem agents.md), usa o mapeamento estático _REPO_TO_SERVICE_ID como fallback.
    """
    index: dict[str, str] = dict(_REPO_TO_SERVICE_ID)  # inicia com o mapeamento estático
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
