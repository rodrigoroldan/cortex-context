"""
routes/consolidate.py — Endpoint for PR CI deploy draft node consolidation (Feature 9).
Promotes speculative draft nodes (status: draft, is_draft: true) into canonical main graph nodes.
"""
from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.db.neo4j import get_driver

logger = logging.getLogger(__name__)

router = APIRouter(tags=["consolidate"])


class ConsolidateRequest(BaseModel):
    branch: str = Field(..., min_length=1, description="Branch name containing draft nodes")
    domain_id: str = Field(default="default", description="Multi-tenant domain identifier")


class ConsolidateResponse(BaseModel):
    branch: str
    domain_id: str
    consolidated_count: int
    promoted_nodes: list[str]
    status: str
    message: str = ""


@router.post("/graph/consolidate", response_model=ConsolidateResponse)
@router.post("/consolidate", response_model=ConsolidateResponse)
async def consolidate_draft_nodes(req: ConsolidateRequest) -> ConsolidateResponse:
    """
    Consolidate speculative draft nodes on a PR merge to main.
    Promotes draft nodes of a branch to canonical main nodes by setting status='active', is_draft=False, branch='main'.
    """
    if not req.branch or not req.branch.strip():
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Branch name cannot be empty")

    branch = req.branch.strip()
    domain_id = req.domain_id or "default"
    promoted_nodes: list[str] = []
    consolidated_count = 0

    try:
        driver = get_driver()
        async with driver.session() as session:
            cypher = """
            MATCH (n)
            WHERE n.branch = $branch
              AND (n.domain_id = $domain_id OR ($domain_id = 'default' AND n.domain_id IS NULL))
              AND (n.status = 'draft' OR n.is_draft = true)
            SET n.status = 'active', n.is_draft = false, n.branch = 'main', n.consolidated_at = datetime()
            RETURN n.id AS id, n.canonical_id AS canonical_id
            """
            result = await session.run(cypher, branch=branch, domain_id=domain_id)
            records = await result.data()
            if records:
                promoted_nodes = [r.get("canonical_id") or r.get("id") for r in records if r.get("id") or r.get("canonical_id")]
                consolidated_count = len(promoted_nodes)
    except Exception as e:
        logger.error("Neo4j error during draft node consolidation: %s", e)

    return ConsolidateResponse(
        branch=branch,
        domain_id=domain_id,
        consolidated_count=consolidated_count,
        promoted_nodes=promoted_nodes,
        status="success",
        message=f"Consolidated {consolidated_count} draft nodes from branch '{branch}' to main (domain: {domain_id}).",
    )
