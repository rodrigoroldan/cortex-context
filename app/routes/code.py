"""
routes/code.py — Rotas para análise de AST, Call Graph, Rastreabilidade e Blast Radius no Cortex Context.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Security, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel, Field

from app.config import get_settings
from app.db.neo4j import get_driver
from app.routes.dependencies import get_branch, get_domain_id

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/code", tags=["code"])
bearer = HTTPBearer(auto_error=False)


def _verify_token(
    credentials: Optional[HTTPAuthorizationCredentials] = Security(bearer),
    settings=Depends(get_settings),
) -> str:
    if not settings.cortex_api_token:
        return ""
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token obrigatório")
    if credentials.credentials != settings.cortex_api_token:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Token inválido")
    return credentials.credentials


# ─── Response Models ──────────────────────────────────────────────────────────


class CodeSymbolNode(BaseModel):
    id: str
    name: str
    kind: str = "function"
    file_path: str = ""
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    signature: str = ""
    docstring: str = ""
    domain_id: str = "default"
    branch: str = "main"


class CallHierarchyEdge(BaseModel):
    caller_id: str
    callee_id: str
    depth: int = 1


class CallHierarchyResponse(BaseModel):
    root_symbol: str
    direction: str  # "callers" | "callees"
    depth: int
    symbols: list[CodeSymbolNode]
    calls: list[CallHierarchyEdge]
    total_symbols: int
    domain_id: str
    branch: str


class TraceLink(BaseModel):
    entity_type: str  # "Spec" | "Service" | "API" | "ADR" | "Concept"
    id: str
    title: str = ""
    relationship: str


class SymbolTraceResponse(BaseModel):
    symbol: CodeSymbolNode
    service: Optional[dict[str, Any]] = None
    implements_specs: list[TraceLink] = Field(default_factory=list)
    exposes_apis: list[TraceLink] = Field(default_factory=list)
    complies_with_adrs: list[TraceLink] = Field(default_factory=list)
    domain_id: str
    branch: str


class BlastRadiusImpact(BaseModel):
    target: str
    direct_callers_count: int
    indirect_callers_count: int
    affected_files: list[str] = Field(default_factory=list)
    affected_symbols: list[CodeSymbolNode] = Field(default_factory=list)
    affected_apis: list[str] = Field(default_factory=list)
    affected_services: list[str] = Field(default_factory=list)
    affected_specs: list[str] = Field(default_factory=list)
    risk_level: str = "LOW"  # "LOW" | "MEDIUM" | "HIGH" | "CRITICAL"
    domain_id: str
    branch: str


class ImplementationsResponse(BaseModel):
    query_target: str
    target_type: str  # "Spec" | "Concept"
    implementations: list[CodeSymbolNode]
    total: int
    domain_id: str
    branch: str


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _sanitize_props(props: dict) -> dict:
    result = {}
    for k, v in props.items():
        if hasattr(v, "iso_format"):
            result[k] = v.iso_format()
        elif hasattr(v, "__class__") and v.__class__.__module__.startswith("neo4j"):
            result[k] = str(v)
        else:
            result[k] = v
    return result


# ─── Endpoints ────────────────────────────────────────────────────────────────


@router.get(
    "/hierarchy",
    response_model=CallHierarchyResponse,
    summary="Obtém a hierarquia de chamadas (callers/callees) de uma função ou classe",
)
async def get_call_hierarchy(
    symbol: str = Query(..., description="Nome ou ID do símbolo/função"),
    direction: str = Query("callers", pattern="^(callers|callees)$", description="Direção: callers (quem chama) ou callees (quem é chamado)"),
    depth: int = Query(2, ge=1, le=5, description="Profundidade máxima de travessia (1 a 5)"),
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> CallHierarchyResponse:
    driver = get_driver()

    async with driver.session() as session:
        if direction == "callers":
            cypher = f"""
            MATCH (target:CodeSymbol)
            WHERE (target.id = $symbol OR target.name = $symbol OR target.id ENDS WITH ('#' + $symbol))
              AND (target.domain_id = $domain_id OR ($domain_id = 'default' AND target.domain_id IS NULL))
              AND (target.branch = $branch OR target.branch = 'main' OR target.branch IS NULL OR target.is_draft = false)
            OPTIONAL MATCH path = (caller:CodeSymbol)-[r:CALLS*1..{depth}]->(target)
            WHERE (caller.domain_id = $domain_id OR ($domain_id = 'default' AND caller.domain_id IS NULL))
              AND (caller.branch = $branch OR caller.branch = 'main' OR caller.branch IS NULL OR caller.is_draft = false)
            WITH target, collect(DISTINCT caller) AS callers, collect(DISTINCT path) AS paths
            RETURN target {{.*}} AS target_props,
                   [c IN callers | c {{.*}}] AS caller_props_list,
                   [p IN paths | [rel IN relationships(p) | {{
                       caller_id: startNode(rel).id,
                       callee_id: endNode(rel).id
                   }}]] AS path_edges
            """
        else:
            cypher = f"""
            MATCH (target:CodeSymbol)
            WHERE (target.id = $symbol OR target.name = $symbol OR target.id ENDS WITH ('#' + $symbol))
              AND (target.domain_id = $domain_id OR ($domain_id = 'default' AND target.domain_id IS NULL))
              AND (target.branch = $branch OR target.branch = 'main' OR target.branch IS NULL OR target.is_draft = false)
            OPTIONAL MATCH path = (target)-[r:CALLS*1..{depth}]->(callee:CodeSymbol)
            WHERE (callee.domain_id = $domain_id OR ($domain_id = 'default' AND callee.domain_id IS NULL))
              AND (callee.branch = $branch OR callee.branch = 'main' OR callee.branch IS NULL OR callee.is_draft = false)
            WITH target, collect(DISTINCT callee) AS callees, collect(DISTINCT path) AS paths
            RETURN target {{.*}} AS target_props,
                   [c IN callees | c {{.*}}] AS caller_props_list,
                   [p IN paths | [rel IN relationships(p) | {{
                       caller_id: startNode(rel).id,
                       callee_id: endNode(rel).id
                   }}]] AS path_edges
            """

        result = await session.run(cypher, symbol=symbol, domain_id=domain_id, branch=branch)
        records = await result.data()

        if not records or not records[0].get("target_props"):
            # Fallback gracioso se o símbolo não for encontrado
            return CallHierarchyResponse(
                root_symbol=symbol,
                direction=direction,
                depth=depth,
                symbols=[],
                calls=[],
                total_symbols=0,
                domain_id=domain_id,
                branch=branch,
            )

        row = records[0]
        target_props = row["target_props"] or {}
        symbols_map: dict[str, CodeSymbolNode] = {}

        root_node = CodeSymbolNode(
            id=str(target_props.get("id", symbol)),
            name=str(target_props.get("name", symbol)),
            kind=str(target_props.get("kind", "function")),
            file_path=str(target_props.get("file_path", "")),
            line_start=target_props.get("line_start"),
            line_end=target_props.get("line_end"),
            signature=str(target_props.get("signature", "")),
            docstring=str(target_props.get("docstring", "")),
            domain_id=str(target_props.get("domain_id", domain_id)),
            branch=str(target_props.get("branch", branch)),
        )
        symbols_map[root_node.id] = root_node

        for c_props in row.get("caller_props_list", []):
            if not c_props or not c_props.get("id"):
                continue
            cid = str(c_props.get("id"))
            symbols_map[cid] = CodeSymbolNode(
                id=cid,
                name=str(c_props.get("name", "")),
                kind=str(c_props.get("kind", "function")),
                file_path=str(c_props.get("file_path", "")),
                line_start=c_props.get("line_start"),
                line_end=c_props.get("line_end"),
                signature=str(c_props.get("signature", "")),
                docstring=str(c_props.get("docstring", "")),
                domain_id=str(c_props.get("domain_id", domain_id)),
                branch=str(c_props.get("branch", branch)),
            )

        calls: list[CallHierarchyEdge] = []
        seen_calls = set()
        for path_edge_group in row.get("path_edges", []):
            for edge in path_edge_group:
                c_from = edge.get("caller_id")
                c_to = edge.get("callee_id")
                if c_from and c_to:
                    key = f"{c_from}->{c_to}"
                    if key not in seen_calls:
                        seen_calls.add(key)
                        calls.append(CallHierarchyEdge(caller_id=c_from, callee_id=c_to))

        return CallHierarchyResponse(
            root_symbol=root_node.name or symbol,
            direction=direction,
            depth=depth,
            symbols=list(symbols_map.values()),
            calls=calls,
            total_symbols=len(symbols_map),
            domain_id=domain_id,
            branch=branch,
        )


@router.get(
    "/trace",
    response_model=SymbolTraceResponse,
    summary="Rastreia um símbolo de código até sua Spec, Serviço, API e ADRs",
)
async def trace_symbol(
    symbol: str = Query(..., description="Nome ou ID do símbolo"),
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> SymbolTraceResponse:
    driver = get_driver()

    async with driver.session() as session:
        cypher = """
        MATCH (target:CodeSymbol)
        WHERE (target.id = $symbol OR target.name = $symbol OR target.id ENDS WITH ('#' + $symbol))
          AND (target.domain_id = $domain_id OR ($domain_id = 'default' AND target.domain_id IS NULL))
          AND (target.branch = $branch OR target.branch = 'main' OR target.branch IS NULL OR target.is_draft = false)
        OPTIONAL MATCH (target)-[:IMPLEMENTS_SPEC]->(spec:Spec)
        OPTIONAL MATCH (target)-[:EXPOSES_API]->(api:API)
        OPTIONAL MATCH (target)-[:COMPLIES_WITH]->(adr:ADR)
        OPTIONAL MATCH (svc:Service)-[:CONTAINS_CODE]->(target)
        RETURN target {.*} AS target_props,
               svc {.*} AS service_props,
               collect(DISTINCT spec {.*}) AS specs,
               collect(DISTINCT api {.*}) AS apis,
               collect(DISTINCT adr {.*}) AS adrs
        LIMIT 1
        """
        result = await session.run(cypher, symbol=symbol, domain_id=domain_id, branch=branch)
        records = await result.data()

        if not records or not records[0].get("target_props"):
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Símbolo de código '{symbol}' não encontrado no domínio '{domain_id}'.",
            )

        row = records[0]
        tp = row["target_props"]
        sym_node = CodeSymbolNode(
            id=str(tp.get("id", symbol)),
            name=str(tp.get("name", symbol)),
            kind=str(tp.get("kind", "function")),
            file_path=str(tp.get("file_path", "")),
            line_start=tp.get("line_start"),
            line_end=tp.get("line_end"),
            signature=str(tp.get("signature", "")),
            docstring=str(tp.get("docstring", "")),
            domain_id=str(tp.get("domain_id", domain_id)),
            branch=str(tp.get("branch", branch)),
        )

        implements_specs = [
            TraceLink(
                entity_type="Spec",
                id=str(s.get("id", "")),
                title=str(s.get("title", s.get("id", ""))),
                relationship="IMPLEMENTS_SPEC",
            )
            for s in row.get("specs", [])
            if s and s.get("id")
        ]

        exposes_apis = [
            TraceLink(
                entity_type="API",
                id=str(a.get("id", "")),
                title=str(a.get("title", a.get("id", ""))),
                relationship="EXPOSES_API",
            )
            for a in row.get("apis", [])
            if a and a.get("id")
        ]

        complies_with_adrs = [
            TraceLink(
                entity_type="ADR",
                id=str(adr.get("id", "")),
                title=str(adr.get("title", adr.get("id", ""))),
                relationship="COMPLIES_WITH",
            )
            for adr in row.get("adrs", [])
            if adr and adr.get("id")
        ]

        service_props = row.get("service_props")
        return SymbolTraceResponse(
            symbol=sym_node,
            service=_sanitize_props(service_props) if service_props else None,
            implements_specs=implements_specs,
            exposes_apis=exposes_apis,
            complies_with_adrs=complies_with_adrs,
            domain_id=domain_id,
            branch=branch,
        )


@router.get(
    "/blast-radius",
    response_model=BlastRadiusImpact,
    summary="Calcula o raio de impacto de alterar um símbolo ou arquivo de código",
)
async def get_blast_radius(
    symbol: Optional[str] = Query(None, description="Nome ou ID do símbolo"),
    file_path: Optional[str] = Query(None, description="Caminho do arquivo de código"),
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> BlastRadiusImpact:
    if not symbol and not file_path:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe ao menos 'symbol' ou 'file_path'.",
        )

    driver = get_driver()
    target_label = symbol or file_path or ""

    async with driver.session() as session:
        cypher = """
        MATCH (target:CodeSymbol)
        WHERE ($symbol IS NOT NULL AND (target.id = $symbol OR target.name = $symbol OR target.id ENDS WITH ('#' + $symbol)))
           OR ($file_path IS NOT NULL AND target.file_path = $file_path)
          AND (target.domain_id = $domain_id OR ($domain_id = 'default' AND target.domain_id IS NULL))
          AND (target.branch = $branch OR target.branch = 'main' OR target.branch IS NULL OR target.is_draft = false)
        OPTIONAL MATCH (direct_caller:CodeSymbol)-[:CALLS]->(target)
        OPTIONAL MATCH (indirect_caller:CodeSymbol)-[:CALLS*2..5]->(target)
        OPTIONAL MATCH (all_callers:CodeSymbol)-[:CALLS*0..5]->(target)
        OPTIONAL MATCH (all_callers)-[:EXPOSES_API]->(api:API)
        OPTIONAL MATCH (svc:Service)-[:CONTAINS_CODE]->(all_callers)
        OPTIONAL MATCH (all_callers)-[:IMPLEMENTS_SPEC]->(spec:Spec)
        RETURN count(DISTINCT direct_caller) AS direct_count,
               count(DISTINCT indirect_caller) AS indirect_count,
               collect(DISTINCT direct_caller.file_path) + collect(DISTINCT indirect_caller.file_path) + collect(DISTINCT target.file_path) AS files,
               collect(DISTINCT direct_caller {.*}) + collect(DISTINCT indirect_caller {.*}) AS callers,
               collect(DISTINCT api.id) AS apis,
               collect(DISTINCT svc.name) AS services,
               collect(DISTINCT spec.id) AS specs
        """
        result = await session.run(cypher, symbol=symbol, file_path=file_path, domain_id=domain_id, branch=branch)
        records = await result.data()

        row = records[0] if records else {}
        direct_count = int(row.get("direct_count", 0))
        indirect_count = int(row.get("indirect_count", 0))
        affected_files = sorted(list({f for f in row.get("files", []) if f}))
        affected_apis = sorted(list({a for a in row.get("apis", []) if a}))
        affected_services = sorted(list({s for s in row.get("services", []) if s}))
        affected_specs = sorted(list({sp for sp in row.get("specs", []) if sp}))

        affected_symbols = []
        for c in row.get("callers", []):
            if c and c.get("id"):
                affected_symbols.append(
                    CodeSymbolNode(
                        id=str(c.get("id")),
                        name=str(c.get("name", "")),
                        kind=str(c.get("kind", "function")),
                        file_path=str(c.get("file_path", "")),
                        line_start=c.get("line_start"),
                        line_end=c.get("line_end"),
                        signature=str(c.get("signature", "")),
                        domain_id=str(c.get("domain_id", domain_id)),
                        branch=str(c.get("branch", branch)),
                    )
                )

        # Determinar nível de risco
        if len(affected_apis) > 0 or len(affected_services) > 1:
            risk = "CRITICAL"
        elif direct_count > 5 or indirect_count > 10:
            risk = "HIGH"
        elif direct_count > 0 or indirect_count > 0:
            risk = "MEDIUM"
        else:
            risk = "LOW"

        return BlastRadiusImpact(
            target=target_label,
            direct_callers_count=direct_count,
            indirect_callers_count=indirect_count,
            affected_files=affected_files,
            affected_symbols=affected_symbols[:50],  # cap para payload razoável
            affected_apis=affected_apis,
            affected_services=affected_services,
            affected_specs=affected_specs,
            risk_level=risk,
            domain_id=domain_id,
            branch=branch,
        )


@router.get(
    "/implementations",
    response_model=ImplementationsResponse,
    summary="Busca implementações de código vinculadas a uma Spec ou Conceito de Negócio",
)
async def find_implementations(
    spec_id: Optional[str] = Query(None, description="ID da Spec"),
    concept_name: Optional[str] = Query(None, description="Nome do Conceito de Negócio"),
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> ImplementationsResponse:
    if not spec_id and not concept_name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Informe ao menos 'spec_id' ou 'concept_name'.",
        )

    target_type = "Spec" if spec_id else "Concept"
    target_val = spec_id or concept_name or ""
    driver = get_driver()

    async with driver.session() as session:
        if spec_id:
            cypher = """
            MATCH (sym:CodeSymbol)-[:IMPLEMENTS_SPEC]->(s:Spec)
            WHERE (s.id = $spec_id OR s.id ENDS WITH ('-' + $spec_id))
              AND (sym.domain_id = $domain_id OR ($domain_id = 'default' AND sym.domain_id IS NULL))
              AND (sym.branch = $branch OR sym.branch = 'main' OR sym.branch IS NULL OR sym.is_draft = false)
            RETURN sym {.*} AS sym_props
            LIMIT 50
            """
        else:
            cypher = """
            MATCH (sym:CodeSymbol)-[:REFERENCES_CONCEPT]->(c:Concept)
            WHERE (toLower(c.name) = toLower($concept_name) OR c.id = $concept_name)
              AND (sym.domain_id = $domain_id OR ($domain_id = 'default' AND sym.domain_id IS NULL))
              AND (sym.branch = $branch OR sym.branch = 'main' OR sym.branch IS NULL OR sym.is_draft = false)
            RETURN sym {.*} AS sym_props
            LIMIT 50
            """

        result = await session.run(cypher, spec_id=spec_id, concept_name=concept_name, domain_id=domain_id, branch=branch)
        records = await result.data()

        implementations = []
        for r in records:
            p = r.get("sym_props") or {}
            if p and p.get("id"):
                implementations.append(
                    CodeSymbolNode(
                        id=str(p.get("id")),
                        name=str(p.get("name", "")),
                        kind=str(p.get("kind", "function")),
                        file_path=str(p.get("file_path", "")),
                        line_start=p.get("line_start"),
                        line_end=p.get("line_end"),
                        signature=str(p.get("signature", "")),
                        domain_id=str(p.get("domain_id", domain_id)),
                        branch=str(p.get("branch", branch)),
                    )
                )

        return ImplementationsResponse(
            query_target=target_val,
            target_type=target_type,
            implementations=implementations,
            total=len(implementations),
            domain_id=domain_id,
            branch=branch,
        )


# ─── Ingestion Models & Endpoint ──────────────────────────────────────────────


class CodeSymbolInput(BaseModel):
    id: str
    name: str
    kind: str = "function"
    file_path: str = ""
    line_start: Optional[int] = None
    line_end: Optional[int] = None
    signature: str = ""
    docstring: str = ""


class CodeFileInput(BaseModel):
    file_path: str
    language: str = "typescript"
    symbols: list[CodeSymbolInput] = Field(default_factory=list)
    imports: list[str] = Field(default_factory=list)


class CallEdgeInput(BaseModel):
    caller_id: str
    callee_id: str


class SpecLinkInput(BaseModel):
    symbol_id: str
    spec_id: str


class AdrLinkInput(BaseModel):
    symbol_id: str
    adr_id: str


class ApiLinkInput(BaseModel):
    symbol_id: str
    api_id: str
    endpoint: Optional[str] = None


class CodeGraphIngestRequest(BaseModel):
    domain_id: str = "default"
    branch: str = "main"
    draft: bool = False
    repo: str = ""
    commit_sha: Optional[str] = None
    files: list[CodeFileInput] = Field(default_factory=list)
    symbols: list[CodeSymbolInput] = Field(default_factory=list)
    calls: list[CallEdgeInput] = Field(default_factory=list)
    implements_specs: list[SpecLinkInput] = Field(default_factory=list)
    complies_adrs: list[AdrLinkInput] = Field(default_factory=list)
    exposes_apis: list[ApiLinkInput] = Field(default_factory=list)


class CodeGraphIngestResponse(BaseModel):
    domain_id: str
    branch: str
    draft: bool
    symbols_upserted: int
    files_upserted: int
    calls_upserted: int
    specs_linked: int
    adrs_linked: int
    apis_linked: int
    message: str


@router.post(
    "/ingest",
    response_model=CodeGraphIngestResponse,
    summary="Ingere o AST Code Graph (arquivos, símbolos, chamadas, vínculos de spec/adr)",
)
async def ingest_code_graph(
    payload: CodeGraphIngestRequest,
    domain_id: str = Depends(get_domain_id),
    branch: str = Depends(get_branch),
    _token: str = Depends(_verify_token),
) -> CodeGraphIngestResponse:
    total_items = (
        len(payload.files)
        + len(payload.symbols)
        + len(payload.calls)
        + len(payload.implements_specs)
        + len(payload.complies_adrs)
        + len(payload.exposes_apis)
    )
    max_items = get_settings().code_ingest_max_items
    if total_items > max_items:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=(
                f"Payload com {total_items} itens excede o limite de {max_items} "
                "(files+symbols+calls+implements_specs+complies_adrs+exposes_apis). "
                "Divida o AST em chunks menores e envie múltiplas requisições."
            ),
        )

    effective_domain = payload.domain_id if (payload.domain_id and payload.domain_id != "default") else domain_id
    effective_branch = payload.branch or branch or "main"
    is_draft = payload.draft

    from app.core.parsers.base import EdgeData, NodeData
    from app.core.graph_builder import ingest_nodes, ingest_edges

    driver = get_driver()
    nodes: list[NodeData] = []
    defines_edges: list[EdgeData] = []
    calls_edges: list[EdgeData] = []
    specs_edges: list[EdgeData] = []
    adrs_edges: list[EdgeData] = []
    apis_edges: list[EdgeData] = []

    # 1. Processar Arquivos
    for f in payload.files:
        file_id = f"file:{f.file_path}"
        if is_draft and effective_branch != "main":
            file_node_id = f"draft:{effective_branch}:{file_id}"
        else:
            file_node_id = file_id

        nodes.append(
            NodeData(
                node_labels=["CodeFile", "Implementation"],
                node_id=file_node_id,
                properties={
                    "id": file_node_id,
                    "canonical_id": file_id,
                    "file_path": f.file_path,
                    "language": f.language,
                    "domain_id": effective_domain,
                    "branch": effective_branch,
                    "status": "draft" if is_draft else "canonical",
                    "is_draft": is_draft,
                    "repo": payload.repo,
                },
            )
        )

    # 2. Processar Símbolos
    for s in payload.symbols:
        canonical_id = s.id
        if is_draft and effective_branch != "main" and not s.id.startswith("draft:"):
            symbol_node_id = f"draft:{effective_branch}:{canonical_id}"
        else:
            symbol_node_id = s.id

        nodes.append(
            NodeData(
                node_labels=["CodeSymbol", "Implementation", s.kind.capitalize()],
                node_id=symbol_node_id,
                properties={
                    "id": symbol_node_id,
                    "canonical_id": canonical_id,
                    "name": s.name,
                    "kind": s.kind,
                    "file_path": s.file_path,
                    "line_start": s.line_start,
                    "line_end": s.line_end,
                    "signature": s.signature,
                    "docstring": s.docstring,
                    "domain_id": effective_domain,
                    "branch": effective_branch,
                    "status": "draft" if is_draft else "canonical",
                    "is_draft": is_draft,
                    "repo": payload.repo,
                },
            )
        )

        # Aresta DEFINES: (file) -> (symbol)
        file_id = f"file:{s.file_path}"
        file_from_id = f"draft:{effective_branch}:{file_id}" if (is_draft and effective_branch != "main") else file_id
        defines_edges.append(
            EdgeData(
                from_id=file_from_id,
                to_id=symbol_node_id,
                relationship="DEFINES",
                properties={"domain_id": effective_domain, "branch": effective_branch, "is_draft": is_draft},
            )
        )

    # 3. Processar Chamadas (CALLS)
    for c in payload.calls:
        is_draft_caller = is_draft and effective_branch != "main" and not c.caller_id.startswith("draft:")
        from_id = f"draft:{effective_branch}:{c.caller_id}" if is_draft_caller else c.caller_id

        is_draft_callee = is_draft and effective_branch != "main" and not c.callee_id.startswith("draft:")
        to_id = f"draft:{effective_branch}:{c.callee_id}" if is_draft_callee else c.callee_id

        calls_edges.append(
            EdgeData(
                from_id=from_id,
                to_id=to_id,
                relationship="CALLS",
                properties={"domain_id": effective_domain, "branch": effective_branch, "is_draft": is_draft},
            )
        )

    # 4. Processar Vínculos com Specs (IMPLEMENTS_SPEC)
    for imp in payload.implements_specs:
        is_draft_sym = is_draft and effective_branch != "main" and not imp.symbol_id.startswith("draft:")
        sym_id = f"draft:{effective_branch}:{imp.symbol_id}" if is_draft_sym else imp.symbol_id
        specs_edges.append(
            EdgeData(
                from_id=sym_id,
                to_id=imp.spec_id,
                relationship="IMPLEMENTS_SPEC",
                properties={"domain_id": effective_domain, "branch": effective_branch, "is_draft": is_draft},
            )
        )

    # 5. Processar Vínculos com ADRs (COMPLIES_WITH)
    for comp in payload.complies_adrs:
        is_draft_sym = is_draft and effective_branch != "main" and not comp.symbol_id.startswith("draft:")
        sym_id = f"draft:{effective_branch}:{comp.symbol_id}" if is_draft_sym else comp.symbol_id
        adrs_edges.append(
            EdgeData(
                from_id=sym_id,
                to_id=comp.adr_id,
                relationship="COMPLIES_WITH",
                properties={"domain_id": effective_domain, "branch": effective_branch, "is_draft": is_draft},
            )
        )

    # 6. Processar APIs (EXPOSES_API)
    for exp in payload.exposes_apis:
        is_draft_sym = is_draft and effective_branch != "main" and not exp.symbol_id.startswith("draft:")
        sym_id = f"draft:{effective_branch}:{exp.symbol_id}" if is_draft_sym else exp.symbol_id
        apis_edges.append(
            EdgeData(
                from_id=sym_id,
                to_id=exp.api_id,
                relationship="EXPOSES_API",
                properties={"domain_id": effective_domain, "branch": effective_branch, "is_draft": is_draft, "endpoint": exp.endpoint or ""},
            )
        )

    nodes_ok = await ingest_nodes(driver, nodes, commit_sha=payload.commit_sha, domain_id=effective_domain)
    # Ingerido por categoria (em vez de uma lista única) para que a resposta reporte
    # quantas arestas de cada tipo foram REALMENTE persistidas — antes, specs_linked/
    # adrs_linked/apis_linked/calls_upserted apenas ecoavam len(payload.x), reportando
    # sucesso mesmo quando 0 arestas eram de fato criadas no Neo4j (ver issue #16).
    defines_ok = await ingest_edges(driver, defines_edges, domain_id=effective_domain)
    calls_ok = await ingest_edges(driver, calls_edges, domain_id=effective_domain)
    specs_ok = await ingest_edges(driver, specs_edges, domain_id=effective_domain)
    adrs_ok = await ingest_edges(driver, adrs_edges, domain_id=effective_domain)
    apis_ok = await ingest_edges(driver, apis_edges, domain_id=effective_domain)
    edges_ok = defines_ok + calls_ok + specs_ok + adrs_ok + apis_ok

    return CodeGraphIngestResponse(
        domain_id=effective_domain,
        branch=effective_branch,
        draft=is_draft,
        symbols_upserted=len(payload.symbols),
        files_upserted=len(payload.files),
        calls_upserted=calls_ok,
        specs_linked=specs_ok,
        adrs_linked=adrs_ok,
        apis_linked=apis_ok,
        message=(
            f"AST Code Graph ingerido: {nodes_ok} nós, {edges_ok} arestas persistidas "
            f"({defines_ok} defines, {calls_ok} calls, {specs_ok} specs, {adrs_ok} adrs, {apis_ok} apis)."
        ),
    )
