"""
app/routes/dependencies.py — Shared dependency functions for FastAPI routes.
"""
from __future__ import annotations

from fastapi import Header, Query


def get_domain_id(
    x_domain_id: str | None = Header(default=None, alias="X-Domain-ID"),
    x_cortex_domain: str | None = Header(default=None, alias="X-Cortex-Domain"),
    domain_id: str | None = Query(default=None, alias="domain_id"),
) -> str:
    """
    Resolves domain_id from query parameter 'domain_id', headers 'X-Domain-ID' / 'X-Cortex-Domain',
    or defaults to 'default'.

    Priority:
      1. Query param 'domain_id' (if provided)
      2. Header 'X-Domain-ID' (if provided)
      3. Header 'X-Cortex-Domain' (if provided)
      4. Default fallback: 'default'
    """
    if domain_id is not None and domain_id.strip() != "":
        return domain_id.strip()
    if x_domain_id is not None and x_domain_id.strip() != "":
        return x_domain_id.strip()
    if x_cortex_domain is not None and x_cortex_domain.strip() != "":
        return x_cortex_domain.strip()
    return "default"


def get_branch(
    x_cortex_branch: str | None = Header(default=None, alias="X-Cortex-Branch"),
    x_branch_name: str | None = Header(default=None, alias="X-Branch-Name"),
    x_branch: str | None = Header(default=None, alias="X-Branch"),
    branch: str | None = Query(default=None, alias="branch"),
) -> str:
    """
    Resolves branch from query parameter 'branch', headers 'X-Cortex-Branch' / 'X-Branch-Name' / 'X-Branch',
    or defaults to 'main'.

    Priority:
      1. Query param 'branch' (if provided)
      2. Header 'X-Cortex-Branch' (if provided)
      3. Header 'X-Branch-Name' (if provided)
      4. Header 'X-Branch' (if provided)
      5. Default fallback: 'main'
    """
    if branch is not None and branch.strip() != "":
        return branch.strip()
    if x_cortex_branch is not None and x_cortex_branch.strip() != "":
        return x_cortex_branch.strip()
    if x_branch_name is not None and x_branch_name.strip() != "":
        return x_branch_name.strip()
    if x_branch is not None and x_branch.strip() != "":
        return x_branch.strip()
    return "main"
