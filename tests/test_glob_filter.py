"""
tests/test_glob_filter.py — Cobertura para ingest.exclude_patterns (issue #10).
"""
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import BackgroundTasks

from app.core.glob_filter import filter_excluded_paths, is_excluded
from app.routes.ingest import _run_ingest_pipeline

DEFAULT_EXCLUDES = [
    "**/node_modules/**",
    "**/dist/**",
    "**/build/**",
    "**/.git/**",
    "**/__pycache__/**",
]


def test_is_excluded_matches_top_level_and_nested_node_modules():
    base = Path("/repo")
    assert is_excluded(Path("/repo/node_modules/lib/index.js"), base, DEFAULT_EXCLUDES)
    assert is_excluded(Path("/repo/packages/api/node_modules/x/y.js"), base, DEFAULT_EXCLUDES)
    assert not is_excluded(Path("/repo/src/index.ts"), base, DEFAULT_EXCLUDES)


def test_is_excluded_does_not_false_positive_on_similar_names():
    base = Path("/repo")
    assert not is_excluded(Path("/repo/app/notnode_modules_similar/x.js"), base, DEFAULT_EXCLUDES)


def test_filter_excluded_paths_empty_patterns_is_noop():
    base = Path("/repo")
    paths = [Path("/repo/a.py"), Path("/repo/node_modules/b.js")]
    assert filter_excluded_paths(paths, base, []) == paths


def test_filter_excluded_paths_removes_matches_preserving_order():
    base = Path("/repo")
    paths = [Path("/repo/a.md"), Path("/repo/node_modules/b.js"), Path("/repo/c.md")]
    result = filter_excluded_paths(paths, base, ["**/node_modules/**"])
    assert result == [Path("/repo/a.md"), Path("/repo/c.md")]


@pytest.mark.asyncio
async def test_run_ingest_pipeline_skips_excluded_files(tmp_path: Path):
    """Arquivos dentro de node_modules/ não devem ser parseados quando
    exclude_patterns é configurado (issue #10)."""
    (tmp_path / "node_modules" / "some-lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "some-lib" / "README.md").write_text("# Third-party lib\n")
    (tmp_path / "spec.md").write_text("# Real Spec\n\nConteúdo relevante.\n")

    dim_cfg = MagicMock()
    dim_cfg.dimension = "spec"
    dim_cfg.pillar = "Intent"
    dim_cfg.indexes = []
    dim_cfg.parser = "builtin.markdown_frontmatter"
    dim_cfg.source_type = "filesystem"
    dim_cfg.source_path = str(tmp_path)
    dim_cfg.source_patterns = ["**/*.md"]

    with patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest.ingest_nodes", new_callable=AsyncMock, return_value=1), \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock, return_value=0):

        res = await _run_ingest_pipeline(
            dim_config=dim_cfg,
            plugins_dir=None,
            background_tasks=BackgroundTasks(),
            domain_id="default",
            cfg={"ingest": {"exclude_patterns": ["**/node_modules/**"]}},
        )

    assert res.details["files_parsed"] == 1


@pytest.mark.asyncio
async def test_run_ingest_pipeline_without_exclude_patterns_parses_everything(tmp_path: Path):
    """Sem exclude_patterns (comportamento anterior), node_modules é ingerido normalmente."""
    (tmp_path / "node_modules" / "some-lib").mkdir(parents=True)
    (tmp_path / "node_modules" / "some-lib" / "README.md").write_text("# Third-party lib\n")
    (tmp_path / "spec.md").write_text("# Real Spec\n\nConteúdo relevante.\n")

    dim_cfg = MagicMock()
    dim_cfg.dimension = "spec"
    dim_cfg.pillar = "Intent"
    dim_cfg.indexes = []
    dim_cfg.parser = "builtin.markdown_frontmatter"
    dim_cfg.source_type = "filesystem"
    dim_cfg.source_path = str(tmp_path)
    dim_cfg.source_patterns = ["**/*.md"]

    with patch("app.routes.ingest.get_driver"), \
         patch("app.routes.ingest.ingest_nodes", new_callable=AsyncMock, return_value=2), \
         patch("app.routes.ingest.ingest_edges", new_callable=AsyncMock, return_value=0):

        res = await _run_ingest_pipeline(
            dim_config=dim_cfg,
            plugins_dir=None,
            background_tasks=BackgroundTasks(),
            domain_id="default",
        )

    assert res.details["files_parsed"] == 2
