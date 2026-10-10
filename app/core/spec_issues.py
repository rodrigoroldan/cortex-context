"""
core/spec_issues.py — Specs que o time cria como issue do GitHub.

Fonte opcional da dimensão spec. Configuração genérica em cortex.config.yaml:

    spec_issues:
      repo: "acme/specs-repo"   # vazio = recurso desligado
      label: "spec"

Convenção: label `spec`, título "Spec NNN — <título>", e o número da issue vira o NNN quando o
título não traz um (ex: "Spec NNN — ...", "Spec — ..."). Cada issue vira (ou enriquece) o nó
Spec `spec-NNN`:

  - sempre: issue_number, issue_url, issue_state, issue_state_reason, issue_updated_at,
    has_issue: true, e os chunks do corpo (`spec-NNN__issue__chunk_<i>`, chunk_source "issue");
  - só quando o nó NÃO tem pasta (has_folder): title, summary, status, labels. Se tem pasta,
    a pasta manda nesses campos (a regra é aplicada na persistência — ver
    app/ingestor/spec_pipeline.py).

Falhas do GitHub (401, rede, rate limit) viram SpecIssuesError; quem chama registra em
details.issues_error e segue — nunca derrubam a ingestão.

Este módulo faz HTTP (httpx) mas não toca no Neo4j.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core.chunker import chunk_markdown
from app.core.parsers.base import NodeData, make_chunk_node

logger = logging.getLogger(__name__)

GITHUB_API_URL = "https://api.github.com"
DEFAULT_LABEL = "spec"
PER_PAGE = 100
MAX_PAGES = 100  # teto de segurança: 10 mil issues
REQUEST_TIMEOUT_S = 15.0
SUMMARY_CHARS = 300
PILLAR = "Intent"

_REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_SPEC_NUMBER_RE = re.compile(r"Spec\s+(\d{3,})", re.IGNORECASE)
# "Spec 861 — ", "Spec NNN: ", "Spec - " ... (prefixo a remover do título)
_TITLE_PREFIX_RE = re.compile(r"^\s*spec(?:\s+(?:\d{3,}|n+|x+|#+))?\s*[—–:|\-]\s*", re.IGNORECASE)

# Propriedades que vêm SEMPRE da issue (tenha o nó pasta ou não).
ISSUE_PROP_KEYS = (
    "has_issue",
    "issue_number",
    "issue_url",
    "issue_state",
    "issue_state_reason",
    "issue_updated_at",
)


class SpecIssuesError(Exception):
    """Falha ao falar com o GitHub (rede, auth, rate limit, payload inesperado)."""


@dataclass(frozen=True)
class SpecIssuesConfig:
    repo: str
    label: str = DEFAULT_LABEL


def load_spec_issues_config(cfg: dict) -> SpecIssuesConfig | None:
    """
    Lê `spec_issues` do cortex.config.yaml. None = recurso desligado (repo vazio/ausente).
    Levanta SpecIssuesError se o repo estiver mal formado (precisa ser "owner/repo").
    """
    raw = (cfg or {}).get("spec_issues") or {}
    repo = str(raw.get("repo") or "").strip()
    if not repo:
        return None
    if not _REPO_RE.match(repo):
        raise SpecIssuesError("spec_issues.repo inválido — use o formato 'owner/repo'")
    label = str(raw.get("label") or DEFAULT_LABEL).strip() or DEFAULT_LABEL
    return SpecIssuesConfig(repo=repo, label=label)


@dataclass
class SpecIssue:
    """Subconjunto da issue do GitHub de que a ingestão precisa."""

    number: int
    title: str
    body: str
    state: str  # open | closed
    state_reason: str | None
    url: str
    updated_at: str
    labels: list[str] = field(default_factory=list)

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> "SpecIssue":
        labels: list[str] = []
        for lbl in data.get("labels") or []:
            name = lbl.get("name") if isinstance(lbl, dict) else lbl
            if name:
                labels.append(str(name))
        return cls(
            number=int(data["number"]),
            title=str(data.get("title") or ""),
            body=str(data.get("body") or ""),
            state=str(data.get("state") or "open"),
            state_reason=(str(data["state_reason"]) if data.get("state_reason") else None),
            url=str(data.get("html_url") or ""),
            updated_at=str(data.get("updated_at") or ""),
            labels=labels,
        )

    def has_label(self, label: str) -> bool:
        return label.casefold() in {lbl.casefold() for lbl in self.labels}


# ─── GitHub REST ──────────────────────────────────────────────────────────────


def _headers(token: str) -> dict[str, str]:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "cortex-context",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _http_error(resp: httpx.Response, what: str) -> SpecIssuesError:
    detail = ""
    try:
        message = resp.json().get("message")
        if message:
            detail = f": {str(message)[:120]}"
    except Exception:  # corpo não-JSON — só o status importa
        pass
    return SpecIssuesError(f"GitHub retornou HTTP {resp.status_code} ao {what}{detail}")


async def fetch_spec_issues(
    config: SpecIssuesConfig,
    token: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> list[SpecIssue]:
    """
    Lista todas as issues (abertas e fechadas) com o label da config, paginando
    `GET /repos/{repo}/issues?labels=<label>&state=all&per_page=100`. PRs são ignorados.
    Ordenadas por número. Levanta SpecIssuesError em qualquer falha.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
    issues: list[SpecIssue] = []
    try:
        for page in range(1, MAX_PAGES + 1):
            resp = await http.get(
                f"{GITHUB_API_URL}/repos/{config.repo}/issues",
                headers=_headers(token),
                params={"labels": config.label, "state": "all", "per_page": PER_PAGE, "page": page},
            )
            if resp.status_code != 200:
                raise _http_error(resp, "listar issues")
            items = resp.json()
            if not isinstance(items, list):
                raise SpecIssuesError("resposta inesperada do GitHub ao listar issues")
            for item in items:
                if not isinstance(item, dict) or "pull_request" in item:
                    continue
                issues.append(SpecIssue.from_api(item))
            if len(items) < PER_PAGE:
                break
    except SpecIssuesError:
        raise
    except Exception as e:  # httpx.HTTPError, JSON inválido, KeyError de payload estranho...
        raise SpecIssuesError(f"falha ao consultar o GitHub ({type(e).__name__})") from e
    finally:
        if owns_client:
            await http.aclose()
    return sorted(issues, key=lambda i: i.number)


async def fetch_issues_by_number(
    config: SpecIssuesConfig,
    numbers: list[int],
    token: str = "",
    *,
    client: httpx.AsyncClient | None = None,
) -> tuple[list[SpecIssue], list[int]]:
    """
    Busca issues específicas (`GET /repos/{repo}/issues/{n}`) para a ingestão incremental.

    Retorna (issues_com_o_label, numeros_que_nao_valem): 404/410, PRs e issues que perderam o
    label entram na segunda lista — quem chama remove os dados de issue desses números.
    Levanta SpecIssuesError em 401/403/5xx/rede.
    """
    owns_client = client is None
    http = client or httpx.AsyncClient(timeout=REQUEST_TIMEOUT_S)
    found: list[SpecIssue] = []
    gone: list[int] = []
    try:
        for number in dict.fromkeys(numbers):  # dedupe preservando ordem
            resp = await http.get(
                f"{GITHUB_API_URL}/repos/{config.repo}/issues/{int(number)}",
                headers=_headers(token),
            )
            if resp.status_code in (404, 410):
                gone.append(number)
                continue
            if resp.status_code != 200:
                raise _http_error(resp, f"buscar a issue #{number}")
            data = resp.json()
            if not isinstance(data, dict) or "pull_request" in data:
                gone.append(number)
                continue
            issue = SpecIssue.from_api(data)
            if issue.has_label(config.label):
                found.append(issue)
            else:
                gone.append(number)
    except SpecIssuesError:
        raise
    except Exception as e:
        raise SpecIssuesError(f"falha ao consultar o GitHub ({type(e).__name__})") from e
    finally:
        if owns_client:
            await http.aclose()
    return found, gone


# ─── Mapeamento issue -> nó Spec ──────────────────────────────────────────────


def parse_spec_title(title: str, issue_number: int) -> tuple[str, str]:
    """
    ("Spec 861 — Split the bill in chat", 900) -> ("861", "Split the bill in chat").

    NNN vem de `Spec\\s+(\\d{3,})` no título; sem ele ("Spec NNN — ...", "Spec — ...") usa o número
    da issue, preenchido com zeros a 3 dígitos para casar com o nome das pastas (#42 -> "042").
    O título sai sem o prefixo "Spec NNN — ".
    """
    m = _SPEC_NUMBER_RE.search(title or "")
    nnn = m.group(1) if m else f"{issue_number:03d}"
    clean = _TITLE_PREFIX_RE.sub("", title or "", count=1).strip() or (title or "").strip()
    return nnn, clean


def status_from_issue(state: str, state_reason: str | None) -> str:
    """open -> planned; closed+not_planned (ou duplicate) -> deprecated; closed (completed/outro) -> done."""
    if state != "closed":
        return "planned"
    if state_reason in ("not_planned", "duplicate"):
        return "deprecated"
    return "done"


def _summary_from_body(body: str) -> str:
    return re.sub(r"\s+", " ", body or "").strip()[:SUMMARY_CHARS]


@dataclass
class MappedIssue:
    """Uma issue traduzida para o que a persistência precisa."""

    node_id: str  # "spec-861"
    issue_number: int
    issue_props: dict  # sempre aplicadas (has_issue, issue_*)
    doc_props: dict  # só aplicadas se o nó não tiver pasta (title, summary, status, labels...)
    chunks: list[NodeData]


def map_issue(issue: SpecIssue, config: SpecIssuesConfig, dimension: str = "spec") -> MappedIssue:
    nnn, title = parse_spec_title(issue.title, issue.number)
    node_id = f"{dimension}-{nnn}"

    labels = [lbl for lbl in issue.labels if lbl.casefold() != config.label.casefold()]
    doc_props: dict[str, Any] = {
        "title": title,
        "summary": _summary_from_body(issue.body),
        "status": status_from_issue(issue.state, issue.state_reason),
        "labels": labels,
        "labels_str": " ".join(labels),
        "pillar": PILLAR,
    }
    if nnn.isdigit():
        doc_props["number"] = int(nnn)

    issue_props = {
        "has_issue": True,
        "issue_number": issue.number,
        "issue_url": issue.url,
        "issue_state": issue.state,
        "issue_state_reason": issue.state_reason,  # None remove a propriedade (ex: issue reaberta)
        "issue_updated_at": issue.updated_at,
    }

    chunks: list[NodeData] = []
    for idx, text in enumerate(chunk_markdown(issue.body.strip(), chunk_size=600, overlap=100)):
        chunks.append(
            make_chunk_node(
                parent_id=node_id,
                chunk_index=idx,
                content=text,
                pillar=PILLAR,
                chunk_key="issue",
                extra_props={"source_file": f"issue#{issue.number}", "chunk_source": "issue"},
            )
        )

    return MappedIssue(
        node_id=node_id,
        issue_number=issue.number,
        issue_props=issue_props,
        doc_props=doc_props,
        chunks=chunks,
    )
