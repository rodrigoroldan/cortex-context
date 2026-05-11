"""
spec_parser.py — Parsing determinístico das specs do Rolê Organizado.

Lê cada specs/NNN-slug/plan.md (ou spec.md) e extrai:
  - número, título, status
  - repos mencionados no texto
  - referências cruzadas a outras specs (spec-NNN ou 'NNN-slug')
  - labels inferidos do slug + texto
"""
from __future__ import annotations

import re
from pathlib import Path

from app.models.spec import SpecEdge, SpecNode

# Repos conhecidos do ecossistema
KNOWN_REPOS = [
    "webview-role-organizado",
    "bff-role-organizado",
    "backend-role-organizado",
    "frontend-admin-role-organizado",
    "app-android-role-organizado",
    "app-ios-role-organizado",
    "lambda-notifications-role-organizado",
    "iac-proxmox-role-organizado",
    "iac-aws-role-organizado",
    "iac-cloudflare-role-organizado",
    "iac-gcp-role-organizado",
    "iac-observability-role-organizado",
    "qa-taac-role-organizado",
    "cortex-role-organizado",
    "orchestrator-openclaw-role-organizado",
    "landing-role-organizado",
]

# Mapeamento de aliases curtos para repos
REPO_ALIASES = {
    "frontend": "webview-role-organizado",
    "webview": "webview-role-organizado",
    "bff": "bff-role-organizado",
    "backend": "backend-role-organizado",
    "admin": "frontend-admin-role-organizado",
    "android": "app-android-role-organizado",
    "ios": "app-ios-role-organizado",
    "lambda": "lambda-notifications-role-organizado",
    "iac": "iac-proxmox-role-organizado",
}

# Padrões de referência cruzada entre specs
_SPEC_REF_PATTERNS = [
    re.compile(r"spec[- _](\d{3})", re.IGNORECASE),
    re.compile(r"\b(\d{3})-[a-z0-9][a-z0-9\-]+"),  # "049-ai-event-chat"
]

# Palavras-chave de status nos arquivos
_STATUS_MAP = {
    "✅": "completed",
    "concluída": "completed",
    "concluido": "completed",
    "completa": "completed",
    "completed": "completed",
    "done": "completed",
    "100%": "completed",
    "🚧": "in-progress",
    "em implementação": "in-progress",
    "em andamento": "in-progress",
    "in-progress": "in-progress",
    "in progress": "in-progress",
    "planejado": "planned",
    "planejada": "planned",
    "aprovado para execução": "planned",
    "planned": "planned",
    "todo": "todo",
    "pendente": "todo",
}

# Relações detectadas por palavras-chave no texto
_RELATION_KEYWORDS = {
    "SUPERSEDES": ["substitui", "supersedes", "substitution of", "substituição de", "replace"],
    "EVOLVES_FROM": [
        "evolução de",
        "evoluir o",
        "evolves from",
        "v2 de",
        "v3 de",
        "redesign de",
        "reimplementação de",
    ],
    "DEPENDS_ON": ["depende de", "depends on", "requer", "requires", "bloqueado por", "blocked by"],
    "IMPLEMENTS": ["implementa", "implements", "parte de", "part of"],
}


def _extract_number_and_slug(dir_name: str) -> tuple[int, str] | None:
    """Extrai (número, slug) de nomes como '070-ai-event-chat-v2-redesign'."""
    m = re.match(r"^(\d{3})-(.+)$", dir_name)
    if not m:
        return None
    return int(m.group(1)), m.group(2)


def _infer_labels(slug: str, content: str) -> list[str]:
    """Infere labels a partir do slug e do conteúdo da spec."""
    labels: set[str] = set()

    # Partes do slug como labels
    parts = slug.replace("-", " ").split()
    for p in parts:
        if len(p) > 2:
            labels.add(p.lower())

    # Repos mencionados viram labels também
    for repo in KNOWN_REPOS:
        short = repo.replace("-role-organizado", "")
        if short in content.lower():
            labels.add(short)

    # Aliases
    for alias in REPO_ALIASES:
        if alias in content.lower():
            labels.add(alias)

    return sorted(labels)[:20]  # limita a 20 labels


def _extract_repos(content: str) -> list[str]:
    """Detecta repos mencionados no texto da spec."""
    found: set[str] = set()
    content_lower = content.lower()
    for repo in KNOWN_REPOS:
        if repo in content_lower:
            found.add(repo)
    for alias, repo in REPO_ALIASES.items():
        if alias in content_lower and repo not in found:
            found.add(repo)
    return sorted(found)


def _extract_status(content: str) -> str:
    """Detecta o status da spec pelo conteúdo."""
    content_lower = content.lower()
    for keyword, status in _STATUS_MAP.items():
        if keyword.lower() in content_lower:
            return status
    return "planned"


def _extract_summary(content: str, max_len: int = 200) -> str:
    """Extrai a primeira frase significativa como summary."""
    for line in content.splitlines():
        stripped = line.strip()
        # Pula headers, badges, dividers e linhas muito curtas
        if (
            stripped.startswith("#")
            or stripped.startswith(">")
            or stripped.startswith("|")
            or stripped.startswith("**")
            or stripped.startswith("---")
            or len(stripped) < 20
        ):
            continue
        # Limita o tamanho
        return stripped[:max_len]
    return ""


def _extract_title(content: str, slug: str) -> str:
    """Extrai o título do primeiro H1, ou deriva do slug."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            title = stripped[2:].strip()
            # Remove prefixos de número tipo "Plan 070: "
            title = re.sub(r"^(Plan|Feature|Spec|Spec\.|#)\s*\d*[:\-\s]*", "", title, flags=re.IGNORECASE)
            return title.strip()
    # Fallback: humaniza o slug
    return slug.replace("-", " ").title()


def _extract_cross_refs(content: str, own_number: int) -> list[int]:
    """Extrai referências a outras specs pelo número."""
    refs: set[int] = set()
    for pattern in _SPEC_REF_PATTERNS:
        for m in pattern.finditer(content):
            num = int(m.group(1))
            if num != own_number and 1 <= num <= 999:
                refs.add(num)
    return sorted(refs)


def _infer_relationship(content: str, ref_number: int) -> str:
    """Tenta inferir o tipo de relação com base no contexto do texto."""
    # Busca uma janela de texto ao redor da referência ao número
    pattern = re.compile(rf"\b0*{ref_number:03d}\b")
    for m in pattern.finditer(content):
        window = content[max(0, m.start() - 120) : m.end() + 120].lower()
        for rel, keywords in _RELATION_KEYWORDS.items():
            for kw in keywords:
                if kw in window:
                    return rel
    return "RELATED_TO"


def parse_spec_dir(spec_dir: Path) -> SpecNode | None:
    """Parseia um diretório de spec e retorna um SpecNode, ou None se inválido."""
    result = _extract_number_and_slug(spec_dir.name)
    if result is None:
        return None
    number, slug = result

    # Procura arquivo principal: plan.md > spec.md > README.md
    content = ""
    file_path = ""
    for candidate in ("plan.md", "spec.md", "README.md"):
        candidate_path = spec_dir / candidate
        if candidate_path.exists():
            content = candidate_path.read_text(encoding="utf-8", errors="replace")
            file_path = f"specs/{spec_dir.name}/{candidate}"
            break

    if not content:
        # Spec vazia mas diretório existe — cria nó mínimo
        return SpecNode(
            id=f"spec-{number:03d}",
            number=number,
            title=slug.replace("-", " ").title(),
            status="todo",
            labels=[slug],
            summary="",
            repos=[],
            file_path=f"specs/{spec_dir.name}/",
        )

    return SpecNode(
        id=f"spec-{number:03d}",
        number=number,
        title=_extract_title(content, slug),
        status=_extract_status(content),
        labels=_infer_labels(slug, content),
        summary=_extract_summary(content),
        repos=_extract_repos(content),
        file_path=file_path,
    )


def parse_edges(nodes_by_number: dict[int, SpecNode], specs_dir: Path) -> list[SpecEdge]:
    """Detecta arestas entre specs a partir de referências cruzadas."""
    edges: list[SpecEdge] = []
    seen: set[tuple[str, str, str]] = set()

    for spec_dir in sorted(specs_dir.iterdir()):
        if not spec_dir.is_dir():
            continue
        result = _extract_number_and_slug(spec_dir.name)
        if result is None:
            continue
        number, _ = result

        content = ""
        for candidate in ("plan.md", "spec.md", "README.md"):
            p = spec_dir / candidate
            if p.exists():
                content = p.read_text(encoding="utf-8", errors="replace")
                break

        if not content:
            continue

        refs = _extract_cross_refs(content, number)
        source_node = nodes_by_number.get(number)
        if source_node is None:
            continue

        for ref_num in refs:
            target_node = nodes_by_number.get(ref_num)
            if target_node is None:
                continue
            rel = _infer_relationship(content, ref_num)
            key = (source_node.id, target_node.id, rel)
            if key not in seen:
                seen.add(key)
                edges.append(
                    SpecEdge(
                        from_id=source_node.id,
                        to_id=target_node.id,
                        relationship=rel,
                    )
                )

    return edges


def parse_all_specs(specs_dir: Path) -> tuple[list[SpecNode], list[SpecEdge]]:
    """Parseia todas as specs e retorna (nodes, edges)."""
    nodes: list[SpecNode] = []
    nodes_by_number: dict[int, SpecNode] = {}

    for spec_dir in sorted(specs_dir.iterdir()):
        if not spec_dir.is_dir():
            continue
        node = parse_spec_dir(spec_dir)
        if node:
            nodes.append(node)
            nodes_by_number[node.number] = node

    edges = parse_edges(nodes_by_number, specs_dir)
    return nodes, edges
