"""
builtin/markdown_frontmatter.py — Parser genérico para arquivos Markdown com frontmatter YAML.

Estratégia de extração:
  1. Se o arquivo começa com "---", extrai o bloco YAML como frontmatter.
  2. Extrai o H1 (# Título) como título do nó.
  3. Extrai a primeira frase/parágrafo como summary.
  4. Detecta referências cruzadas a outros nós via padrões configuráveis.
  5. Infere labels a partir do slug do diretório e do conteúdo.
  6. (Opcional) Produz nós :DocumentChunk para Vector RAG quando
     CORTEX_EMBEDDING_PROVIDER != "none". Chunks são linkados ao nó pai
     via edge CHUNK_OF.

Compatível com qualquer formato de spec/doc — não depende de jargões do produto.

Uso no dimension YAML:
  parser: builtin.markdown_frontmatter
  source_type: filesystem
  source_path: "/specs"
  source_pattern: "**/plan.md"
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from app.core.chunker import chunk_markdown
from app.core.embedder import is_embedder_enabled
from app.core.parsers.base import BaseCortexExtractor, EdgeData, NodeData, ParseResult, make_chunk_node

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig

# ─── Padrões de status inferidos do corpo ────────────────────────────────────
# Ordem importa: mais específico primeiro. Ex: "deprecated" antes de "completed".
_STATUS_BODY_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("deprecated", re.compile(r"DEPRECATED|depreciada?|descontinuada?", re.IGNORECASE)),
    ("completed",  re.compile(r"\u2705|100\s*%|IMPLEMENTATION\s+COMPLETE|Completa\b|\bCOMPLETE\b|\bdone\b|\bconclu[íi]da?", re.IGNORECASE)),
    ("in-progress", re.compile(r"\U0001f6a7|em\s+andamento|in[\s\-]progress", re.IGNORECASE)),
    ("planned",    re.compile(r"\U0001f4cb|[Pp]lanejamento|[Pp]lanejada?|\bplanned\b|[Pp]lanning", re.IGNORECASE)),
    ("todo",       re.compile(r"\u2b1c|\bbacklog\b|a\s+fazer|\bto[\s\-]do\b", re.IGNORECASE)),
]

# Padrão para extração de repos do corpo markdown
# Detecta: "repos: [a, b]" (YAML inline) ou "**Repos**: a, b" (markdown bold)
_REPOS_BODY_PATTERN = re.compile(
    r"^\s*repos?\s*:\s*\[([^\]]+)\]"       # YAML style: repos: [a, b]
    r"|\*{0,2}[Rr]epos?\*{0,2}\s*:\s*([^\n]+)",  # Markdown: **Repos**: a, b
    re.MULTILINE,
)


def _infer_status_from_body(body: str) -> str | None:
    """
    Infere o status a partir do corpo do markdown quando ausente no frontmatter.
    Varre os primeiros 600 chars (cabeçalho/resumo onde o status costuma aparecer).
    """
    snippet = body[:600]
    for status_val, pattern in _STATUS_BODY_PATTERNS:
        if pattern.search(snippet):
            return status_val
    return None


def _extract_repos_from_body(body: str) -> list[str]:
    """
    Extrai lista de repos mencionados no corpo do markdown.
    Detecta padrões YAML inline (repos: [a, b]) e markdown bold (**Repos**: a, b).
    Escaneia os primeiros 1500 chars para cobrir cabeçalhos e tabelas de metadados.
    """
    repos: list[str] = []
    for match in _REPOS_BODY_PATTERN.finditer(body[:1500]):
        raw = match.group(1) or match.group(2) or ""
        for item in raw.split(","):
            clean = item.strip().strip("'\"` ").strip()
            if clean and not clean.startswith("#"):
                repos.append(clean)
    # Deduplica preservando ordem
    seen: set[str] = set()
    return [r for r in repos if r not in seen and not seen.add(r)]  # type: ignore[func-returns-value]


# Padrões de referências cruzadas detectados no texto
# Formato: [relationship_type, regex]
_CROSS_REF_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("DEPENDS_ON", re.compile(r"depends[_ ]on\s+([a-z0-9][a-z0-9\-]{2,})", re.IGNORECASE)),
    ("SUPERSEDES", re.compile(r"supersedes?\s+([a-z0-9][a-z0-9\-]{2,})", re.IGNORECASE)),
    ("EVOLVES_FROM", re.compile(r"evolves?\s+from\s+([a-z0-9][a-z0-9\-]{2,})", re.IGNORECASE)),
    ("RELATED_TO", re.compile(r"related[_ ]to\s+([a-z0-9][a-z0-9\-]{2,})", re.IGNORECASE)),
]

# Padrão para extrair número e slug de diretórios estilo "NNN-slug"
_NUMBERED_SLUG = re.compile(r"^(\d{3})-(.+)$")


def _extract_frontmatter(content: str) -> tuple[dict, str]:
    """
    Extrai frontmatter YAML (bloco ---) e retorna (frontmatter_dict, body_sem_frontmatter).
    Se não houver frontmatter, retorna ({}, conteúdo original).
    """
    if not content.startswith("---"):
        return {}, content

    end = content.find("\n---", 3)
    if end == -1:
        return {}, content

    fm_text = content[3:end].strip()
    body = content[end + 4:].strip()

    try:
        fm = yaml.safe_load(fm_text) or {}
        if not isinstance(fm, dict):
            fm = {}
    except yaml.YAMLError:
        fm = {}

    return fm, body


def _extract_h1(text: str) -> str:
    """Extrai o primeiro H1 (# Título) do markdown."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


def _extract_summary(text: str, max_chars: int = 300) -> str:
    """Extrai o primeiro parágrafo não-heading não-vazio como summary."""
    for line in text.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#") and not stripped.startswith("```"):
            return stripped[:max_chars]
    return ""


def _infer_labels_from_slug(slug: str) -> list[str]:
    """Infere labels a partir do slug do diretório (ex: 'ai-event-chat' → ['ai', 'event', 'chat'])."""
    parts = slug.replace("-", " ").replace("_", " ").split()
    return [p.lower() for p in parts if len(p) > 2]


def _detect_cross_refs(content: str, self_id: str, edge_prefix: str) -> list[EdgeData]:
    """
    Detecta referências cruzadas no corpo do texto e cria EdgeData.
    O edge_prefix é usado para normalizar os IDs referenciados (ex: "spec").
    """
    edges: list[EdgeData] = []
    for rel_type, pattern in _CROSS_REF_PATTERNS:
        for match in pattern.finditer(content):
            ref = match.group(1).strip().lower()
            # Normalizar: se não começa com o prefixo, adiciona
            target_id = ref if ref.startswith(edge_prefix) else f"{edge_prefix}-{ref}"
            if target_id != self_id:
                edges.append(EdgeData(
                    from_id=self_id,
                    to_id=target_id,
                    relationship=rel_type,
                ))
    return edges


class MarkdownFrontmatterParser(BaseCortexExtractor):
    """
    Parser built-in para arquivos Markdown com frontmatter YAML.

    Produz nós com multi-labels [node_label, pillar] conforme a DimensionConfig.
    Qualquer campo do frontmatter vira propriedade do nó.
    """

    extractor_key = "builtin.markdown_frontmatter"

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix in (".md", ".markdown")

    def parse(self, file_path: Path, dimension_config: "DimensionConfig") -> ParseResult:
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return ParseResult()

        frontmatter, body = _extract_frontmatter(content)

        # Determinar o id do nó
        # Prioridade: frontmatter.id → slug do diretório → nome do arquivo sem extensão
        parent = file_path.parent.name
        match = _NUMBERED_SLUG.match(parent)

        if "id" in frontmatter:
            node_id = str(frontmatter["id"])
        elif match:
            number, _ = match.group(1), match.group(2)
            node_id = f"{dimension_config.dimension}-{number}"
        else:
            node_id = f"{dimension_config.dimension}-{parent}"

        # Título
        title = frontmatter.get("title") or _extract_h1(body) or parent

        # Summary
        summary = frontmatter.get("summary") or _extract_summary(body)

        # Labels inferidos
        slug_labels = _infer_labels_from_slug(match.group(2) if match else parent)
        extra_labels = frontmatter.get("labels", [])
        all_labels = list(dict.fromkeys(slug_labels + extra_labels))

        # Status — frontmatter tem prioridade; fallback: inferência do corpo
        raw_status = frontmatter.get("status")
        if raw_status and str(raw_status).lower() not in ("", "unknown"):
            status = str(raw_status)
        else:
            status = _infer_status_from_body(body) or "unknown"

        # Repos — frontmatter tem prioridade; fallback: extração do corpo
        repos: list[str] = frontmatter.get("repos", [])
        if not repos:
            repos = _extract_repos_from_body(body)

        # Propriedades base
        properties: dict = {
            "id": node_id,
            "title": title,
            "summary": summary,
            "status": status,
            "labels": all_labels,
            "labels_str": " ".join(all_labels),
            "file_path": str(file_path),
            "pillar": dimension_config.pillar,
            # Número sequencial (se houver slug numerado)
            **({"number": int(match.group(1))} if match else {}),
        }

        # Adiciona repos se encontrado
        if repos:
            properties["repos"] = repos
            properties["repos_str"] = " ".join(repos)

        # Mesclar campos extras do frontmatter (não sobreescreve campos base)
        for k, v in frontmatter.items():
            if k not in properties and v is not None:
                # Serializar listas como string para compatibilidade com Neo4j FTS
                if isinstance(v, list):
                    properties[f"{k}_str"] = " ".join(str(x) for x in v)
                properties[k] = v

        node = NodeData(
            node_labels=dimension_config.node_labels,  # ex: ["Spec", "Intent"]
            node_id=node_id,
            properties=properties,
        )

        # Detectar referências cruzadas
        edges = _detect_cross_refs(content, node_id, dimension_config.dimension)

        # ── Vector RAG: produzir DocumentChunk nodes ──────────────────────────
        # Ativado apenas quando o embedder está habilitado (provider != "none").
        # Os chunks são gerados a partir do body sem frontmatter.
        # Os embeddings são preenchidos mais tarde pelo pipeline de ingestão.
        chunk_nodes: list[NodeData] = []
        chunk_edges: list[EdgeData] = []

        if is_embedder_enabled() and body.strip():
            text_chunks = chunk_markdown(body, chunk_size=600, overlap=100)
            for idx, chunk_text in enumerate(text_chunks):
                chunk_node = make_chunk_node(
                    parent_id=node_id,
                    chunk_index=idx,
                    content=chunk_text,
                    pillar=dimension_config.pillar,
                )
                chunk_nodes.append(chunk_node)
                chunk_edges.append(EdgeData(
                    from_id=chunk_node.node_id,
                    to_id=node_id,
                    relationship="CHUNK_OF",
                ))

        return ParseResult(
            nodes=[node] + chunk_nodes,
            edges=edges + chunk_edges,
        )
