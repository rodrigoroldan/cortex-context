"""
core/glob_filter.py — Filtro de exclusão para globs de dimensões filesystem.

Aplica `ingest.exclude_patterns` (cortex.config.yaml) sobre uma lista de
caminhos já resolvidos pelo glob, comparando o caminho relativo à raiz da
dimensão (`source_path`) contra os padrões (sintaxe estilo .gitignore: `**`
casa zero ou mais segmentos de diretório, `*` casa qualquer coisa exceto `/`).
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=256)
def _compile_pattern(pattern: str) -> re.Pattern:
    parts = pattern.split("/")
    regex_parts: list[str] = []
    for i, part in enumerate(parts):
        if part == "**":
            # "**/" no meio/início casa zero ou mais segmentos completos
            regex_parts.append(r"(?:.*/)?" if i < len(parts) - 1 else r".*")
        else:
            segment = re.escape(part).replace(r"\*", "[^/]*").replace(r"\?", "[^/]")
            regex_parts.append(segment)
    regex = "^" + "/".join(regex_parts).replace("(?:.*/)?/", "(?:.*/)?") + "$"
    return re.compile(regex)


def is_excluded(path: Path, base_dir: Path, exclude_patterns: list[str]) -> bool:
    """Retorna True se o caminho relativo a base_dir bater com algum exclude_pattern."""
    if not exclude_patterns:
        return False
    try:
        rel_path = path.relative_to(base_dir).as_posix()
    except ValueError:
        rel_path = path.as_posix()
    return any(_compile_pattern(pattern).match(rel_path) for pattern in exclude_patterns)


def filter_excluded_paths(paths: list[Path], base_dir: Path, exclude_patterns: list[str]) -> list[Path]:
    """Remove de `paths` qualquer caminho que bata com `exclude_patterns`."""
    if not exclude_patterns:
        return paths
    return [p for p in paths if not is_excluded(p, base_dir, exclude_patterns)]
