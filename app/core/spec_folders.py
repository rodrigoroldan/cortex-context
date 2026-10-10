"""
core/spec_folders.py — Modo "spec = pasta" da dimensão spec (dimension.group_by: numbered_folder).

Em vez de um nó por arquivo, todos os .md dentro de uma pasta de spec contribuem para UM nó
(:Spec:Intent):

  - Pasta de spec = ancestral mais próximo do arquivo (relativo ao source_path) cujo nome casa
    ^\\d{3,}-  (ex: "861-split-bill"). Arquivos fora de uma pasta numerada são ignorados.
  - Arquivo primário (título, summary, frontmatter, status, repos, referências cruzadas):
    spec.md > README.md > plan.md (os três na raiz da pasta) > primeiro em ordem alfabética
    (arquivos da raiz antes dos de subpastas).
  - O nó ganha `files`, `files_str`, `source_folder`, `primary_file` e `has_folder: true`.
  - Cada arquivo gera seus próprios chunks (DocumentChunk), com id único por arquivo:
    "{spec_id}__{slug-do-arquivo}__chunk_{i}", `source_file`, `parent_id` e `chunk_source: "folder"`.
    Os chunks são gerados SEMPRE (com ou sem embedder) — alimentam o índice fulltext
    `doc_chunk_fts`; os embeddings continuam opcionais.

Este módulo só lê o filesystem; a persistência fica em app/ingestor/spec_pipeline.py.
"""
from __future__ import annotations

import glob
import logging
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING

from app.core.chunker import chunk_markdown
from app.core.glob_filter import filter_excluded_paths
from app.core.parsers.base import EdgeData, NodeData, ParseResult, make_chunk_node
from app.core.parsers.builtin.markdown_frontmatter import (
    NUMBERED_FOLDER_SLUG,
    MarkdownFrontmatterParser,
    _extract_frontmatter,
)

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig

logger = logging.getLogger(__name__)

GROUP_BY_NUMBERED_FOLDER = "numbered_folder"

# Nome de uma pasta de spec: 3+ dígitos, hífen, resto.
SPEC_FOLDER_NAME = re.compile(r"^\d{3,}-.+")

# Arquivos maiores que isso são ignorados (specs reais ficam muito abaixo; evita ingerir dumps).
MAX_FILE_BYTES = 1024 * 1024

CHUNK_SIZE = 600
CHUNK_OVERLAP = 100

# Ordem de precedência do arquivo primário (todos na raiz da pasta).
_PRIMARY_ORDER = ("spec.md", "readme.md", "plan.md")


@dataclass
class SpecFolder:
    """Uma pasta de spec descoberta no filesystem."""

    name: str  # ex: "861-split-bill"
    path: Path  # caminho absoluto da pasta
    files: list[Path] = field(default_factory=list)  # .md elegíveis (absolutos), ordenados

    def rel(self, file_path: Path) -> str:
        """Caminho do arquivo relativo à pasta da spec, em formato posix (ex: 'contracts/api.md')."""
        return file_path.relative_to(self.path).as_posix()


def spec_id_from_folder_name(folder_name: str, dimension: str = "spec") -> str | None:
    """'861-split-bill' -> 'spec-861'. None se o nome não for de uma pasta de spec."""
    m = NUMBERED_FOLDER_SLUG.match(folder_name)
    return f"{dimension}-{m.group(1)}" if m else None


# ─── Mapeamento caminho -> pasta ──────────────────────────────────────────────


def extract_spec_folder_name(path: str) -> str | None:
    """
    Extrai o nome da pasta de spec de um caminho vindo do cliente.

    O caminho pode ser relativo ao repo (ex: "specs/861-x/RUNBOOK.md") ou à pasta de specs
    (ex: "861-x/contracts/api.md"): vale o segmento `\\d{3,}-...` MAIS PRÓXIMO do arquivo.
    O último segmento é tratado como arquivo (um nome "001-intro.md" não vira pasta), exceto
    quando não tem extensão — aí é aceito como a própria pasta ("861-x" ou "specs/861-x/").

    Retorna None se nenhum segmento de diretório casar (ex: "docs/README.md", "../x").
    """
    normalized = (path or "").strip().replace("\\", "/")
    if not normalized:
        return None
    segments = [s for s in normalized.split("/") if s not in ("", ".")]
    if not segments or ".." in segments:
        return None

    dirs = segments[:-1]
    last = segments[-1]
    # Termina em "/" => tudo é diretório; sem extensão => trata o último como pasta também.
    if normalized.endswith("/") or PurePosixPath(last).suffix == "":
        dirs = segments

    for segment in reversed(dirs):
        if SPEC_FOLDER_NAME.match(segment):
            return segment
    return None


# ─── Descoberta no filesystem ─────────────────────────────────────────────────


def _spec_folder_of(rel_parts: tuple[str, ...]) -> int | None:
    """Índice (em rel_parts[:-1]) do ancestral numerado mais próximo do arquivo, ou None."""
    for idx in range(len(rel_parts) - 2, -1, -1):
        if SPEC_FOLDER_NAME.match(rel_parts[idx]):
            return idx
    return None


def discover_spec_folders(
    source_path: Path,
    patterns: list[str],
    exclude_patterns: list[str] | None = None,
) -> dict[str, SpecFolder]:
    """
    Varre source_path com os `patterns` da dimensão (ex: "**/*.md"), agrupa os arquivos por
    pasta de spec e devolve {nome_da_pasta: SpecFolder}, em ordem alfabética.

    - Arquivos fora de pasta numerada são descartados; arquivos > 1 MB também.
    - Duas pastas com o mesmo nome em lugares diferentes: vale a primeira (ordem de caminho).
    """
    matched: list[Path] = []
    for pattern in patterns or ["**/*.md"]:
        matched.extend(Path(p) for p in glob.glob(str(source_path / pattern), recursive=True))
    unique = sorted({p for p in matched if p.is_file()})
    unique = filter_excluded_paths(unique, source_path, exclude_patterns or [])

    folders: dict[str, SpecFolder] = {}
    for file_path in unique:
        try:
            rel_parts = file_path.relative_to(source_path).parts
        except ValueError:
            continue
        idx = _spec_folder_of(rel_parts)
        if idx is None:
            continue
        try:
            if file_path.stat().st_size > MAX_FILE_BYTES:
                logger.warning("Spec: arquivo ignorado por exceder 1 MB: %s", file_path)
                continue
        except OSError:
            continue

        folder_path = source_path.joinpath(*rel_parts[: idx + 1])
        name = rel_parts[idx]
        folder = folders.get(name)
        if folder is None:
            folder = folders[name] = SpecFolder(name=name, path=folder_path)
        elif folder.path != folder_path:
            logger.warning(
                "Spec: pasta duplicada '%s' em %s e %s — ignorando a segunda", name, folder.path, folder_path
            )
            continue
        folder.files.append(file_path)

    return {name: folders[name] for name in sorted(folders)}


# ─── Parse de uma pasta ───────────────────────────────────────────────────────


def choose_primary(rel_paths: list[str]) -> str:
    """
    Escolhe o arquivo primário entre caminhos relativos à pasta da spec:
    spec.md > README.md > plan.md (raiz da pasta, sem diferenciar maiúsculas) > primeiro em ordem
    alfabética (raiz antes das subpastas).
    """
    root_files = {p.lower(): p for p in rel_paths if "/" not in p}
    for wanted in _PRIMARY_ORDER:
        if wanted in root_files:
            return root_files[wanted]
    return sorted(rel_paths, key=lambda p: (p.count("/"), p.casefold(), p))[0]


def file_slug(rel_path: str) -> str:
    """'contracts/API v2.md' -> 'contracts-api-v2' (ascii, minúsculo, sem '_' para não colidir com '__chunk_')."""
    stem = rel_path[:-3] if rel_path.lower().endswith(".md") else rel_path
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_stem.lower()).strip("-")
    return slug or "doc"


@dataclass
class SpecFolderParse:
    """Resultado do parse de uma pasta de spec."""

    node: NodeData
    chunks: list[NodeData]
    edges: list[EdgeData]  # referências cruzadas do arquivo primário (sem CHUNK_OF)
    files_parsed: int = 0
    files_failed: int = 0


def parse_spec_folder(
    folder: SpecFolder,
    dimension_config: "DimensionConfig",
    parser: MarkdownFrontmatterParser | None = None,
) -> SpecFolderParse | None:
    """
    Gera o nó Spec (+ chunks por arquivo) de uma pasta. Retorna None se nenhum arquivo for legível.
    """
    parser = parser or MarkdownFrontmatterParser()

    # Lê todos os arquivos uma vez (utf-8); ilegíveis são pulados e contados.
    contents: dict[str, str] = {}
    failed = 0
    for file_path in folder.files:
        rel = folder.rel(file_path)
        try:
            contents[rel] = file_path.read_text(encoding="utf-8")
        except Exception as e:  # arquivo ilegível não derruba a pasta
            logger.warning("Spec %s: não foi possível ler %s: %s", folder.name, rel, e)
            failed += 1
    if not contents:
        return None

    rels = sorted(contents, key=lambda p: (p.count("/"), p.casefold(), p))
    primary = choose_primary(rels)
    primary_path = folder.path / primary

    result: ParseResult = parser.parse(primary_path, dimension_config, folder_name=folder.name, emit_chunks=False)
    if not result.nodes:
        return None
    node = result.nodes[0]

    node.properties.update(
        {
            "files": rels,
            "files_str": " ".join(rels),
            "source_folder": folder.name,
            "primary_file": primary,
            "has_folder": True,
        }
    )
    # O upsert faz `SET n += props` (não substitui): sem isto, um `repos` removido do frontmatter ficaria
    # no nó e alimentaria AFFECTS velhas. None remove a propriedade no Neo4j.
    for key in ("repos", "repos_str"):
        node.properties.setdefault(key, None)

    # Chunks: um conjunto por arquivo, ids únicos mesmo com arquivos de mesmo slug.
    chunks: list[NodeData] = []
    used_slugs: set[str] = set()
    for rel in rels:
        slug = file_slug(rel)
        unique_slug, n = slug, 2
        while unique_slug in used_slugs:
            unique_slug = f"{slug}-{n}"
            n += 1
        used_slugs.add(unique_slug)

        _, body = _extract_frontmatter(contents[rel])
        for idx, text in enumerate(chunk_markdown(body, chunk_size=CHUNK_SIZE, overlap=CHUNK_OVERLAP)):
            chunks.append(
                make_chunk_node(
                    parent_id=node.node_id,
                    chunk_index=idx,
                    content=text,
                    pillar=dimension_config.pillar,
                    chunk_key=unique_slug,
                    extra_props={"source_file": f"{folder.name}/{rel}", "chunk_source": "folder"},
                )
            )

    return SpecFolderParse(
        node=node,
        chunks=chunks,
        edges=list(result.edges),
        files_parsed=len(contents),
        files_failed=failed,
    )
