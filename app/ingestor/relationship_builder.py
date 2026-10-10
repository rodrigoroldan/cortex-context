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
from typing import Any

from app.core.parsers.base import EdgeData, NodeData

logger = logging.getLogger(__name__)

# Remove as AFFECTS que ESTE mecanismo criou (todas nascem com created_at, ver _CREATE_SPEC_AFFECTS); arestas
# AFFECTS feitas à mão ou por /ingest/manifest não têm created_at e não são tocadas.
_DELETE_SPEC_AFFECTS = """
MATCH (spec:Spec {domain_id: $domain_id})-[r:AFFECTS]->(:Service)
WHERE r.created_at IS NOT NULL AND ($spec_ids IS NULL OR spec.id IN $spec_ids)
DELETE r
RETURN count(r) AS deleted
"""

_CREATE_SPEC_AFFECTS = """
MATCH (spec:Spec {domain_id: $domain_id})
WHERE spec.repos IS NOT NULL AND size(spec.repos) > 0 AND ($spec_ids IS NULL OR spec.id IN $spec_ids)
UNWIND spec.repos AS repo
MATCH (svc:Service {domain_id: $domain_id}) WHERE svc.repo = repo
MERGE (spec)-[r:AFFECTS]->(svc)
ON CREATE SET r.created_at = datetime(), r.domain_id = $domain_id
RETURN count(r) AS created
"""


async def rebuild_spec_affects(
    driver: Any,
    domain_id: str,
    spec_ids: list[str] | None = None,
    *,
    replace: bool = True,
) -> int:
    """
    Recalcula as arestas (:Spec)-[:AFFECTS]->(:Service) a partir de Spec.repos[] x Service.repo, no domain_id.

    Args:
        spec_ids: só estas specs (ingestão incremental); None = todas as specs do domínio.
        replace:  remove antes as AFFECTS dessas specs que este mecanismo criou, para que um `repos`
                  removido do frontmatter não deixe aresta velha. Arestas sem created_at (manifest,
                  manuais) são preservadas.

    Retorna o número de arestas criadas/verificadas.
    """
    params = {"domain_id": domain_id, "spec_ids": spec_ids}
    async with driver.session() as session:
        if replace:
            deleted = await session.run(_DELETE_SPEC_AFFECTS, params)
            record = await deleted.single()
            logger.info("AFFECTS edges (domain: %s): %d removidas antes do recálculo", domain_id, record["deleted"] if record else 0)
        result = await session.run(_CREATE_SPEC_AFFECTS, params)
        record = await result.single()
    created = record["created"] if record else 0
    logger.info("AFFECTS edges (Spec→Service, domain: %s): %d criadas/verificadas", domain_id, created)
    return created

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
        repos: list[str] = spec_node.properties.get("repos") or []

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
