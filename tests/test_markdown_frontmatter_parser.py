"""
tests/test_markdown_frontmatter_parser.py — Testes do parser builtin.markdown_frontmatter.

Cobre:
  - Extração de frontmatter YAML
  - Inferência de status a partir do corpo do markdown
  - Extração de repos do corpo do markdown
  - Lógica de ID / slug
  - Referências cruzadas (cross-refs)
  - Títulos H1 e summary
"""
from __future__ import annotations

import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from app.core.parsers.builtin.markdown_frontmatter import (
    MarkdownFrontmatterParser,
    _extract_frontmatter,
    _extract_h1,
    _extract_repos_from_body,
    _extract_summary,
    _infer_labels_from_slug,
    _infer_status_from_body,
)


# ─── Fixtures de DimensionConfig ──────────────────────────────────────────────


class _FakeDimConfig:
    """Substituto mínimo de DimensionConfig para os testes."""

    def __init__(self, dimension: str = "spec", pillar: str = "Intent"):
        self.dimension = dimension
        self.pillar = pillar
        self.node_labels = [dimension.capitalize(), pillar]


# ─── _extract_frontmatter ──────────────────────────────────────────────────────


class TestExtractFrontmatter:
    def test_retorna_dict_quando_ha_frontmatter_valido(self):
        content = "---\ntitle: Minha Spec\nstatus: completed\n---\nCorpo aqui."
        fm, body = _extract_frontmatter(content)
        assert fm["title"] == "Minha Spec"
        assert fm["status"] == "completed"
        assert body == "Corpo aqui."

    def test_retorna_vazio_sem_frontmatter(self):
        content = "# Título\n\nAlgum texto."
        fm, body = _extract_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_retorna_vazio_com_yaml_invalido(self):
        content = "---\n: invalid: yaml: [\n---\nCorpo."
        fm, body = _extract_frontmatter(content)
        assert fm == {}

    def test_retorna_vazio_quando_frontmatter_nao_fecha(self):
        content = "---\ntitle: Sem fechar"
        fm, body = _extract_frontmatter(content)
        assert fm == {}
        assert body == content


# ─── _extract_h1 ──────────────────────────────────────────────────────────────


class TestExtractH1:
    def test_extrai_h1(self):
        assert _extract_h1("# Meu Título\n\nTexto.") == "Meu Título"

    def test_ignora_h2(self):
        assert _extract_h1("## Subtítulo\n\nTexto.") == ""

    def test_retorna_vazio_sem_heading(self):
        assert _extract_h1("Apenas texto sem heading.") == ""


# ─── _extract_summary ─────────────────────────────────────────────────────────


class TestExtractSummary:
    def test_extrai_primeiro_paragrafo(self):
        text = "# Título\n\nPrimeiro parágrafo útil.\n\nSegundo parágrafo."
        assert _extract_summary(text) == "Primeiro parágrafo útil."

    def test_ignora_headings_e_vazios(self):
        text = "\n\n## Heading\n\nParágrafo real aqui."
        assert _extract_summary(text) == "Parágrafo real aqui."

    def test_respeita_max_chars(self):
        text = "A" * 500
        assert len(_extract_summary(text, max_chars=100)) == 100


# ─── _infer_labels_from_slug ──────────────────────────────────────────────────


class TestInferLabelsFromSlug:
    def test_separa_por_hifen(self):
        assert _infer_labels_from_slug("ai-event-chat") == ["event", "chat"]

    def test_remove_palavras_muito_curtas(self):
        # "ai" tem 2 chars, deve ser removido (len > 2)
        labels = _infer_labels_from_slug("ai-payment")
        assert "ai" not in labels
        assert "payment" in labels

    def test_normaliza_para_lowercase(self):
        labels = _infer_labels_from_slug("Payment-Checkout")
        assert "payment" in labels
        assert "checkout" in labels


# ─── _infer_status_from_body ──────────────────────────────────────────────────


class TestInferStatusFromBody:
    @pytest.mark.parametrize("snippet,expected", [
        ("**Status**: ✅ 100% Completa", "completed"),
        ("**Status**: ✅ **IMPLEMENTATION COMPLETE** (100%)", "completed"),
        ("Status: done", "completed"),
        ("Status: concluída", "completed"),
        ("🚧 em andamento", "in-progress"),
        ("Status: in-progress", "in-progress"),
        ("📋 Planejamento Completo", "planned"),
        ("Status: planned", "planned"),
        ("⬜ backlog", "todo"),
        ("to-do item", "todo"),
        ("⚠️ DEPRECATED — spec unificada", "deprecated"),
    ])
    def test_infere_status_correto(self, snippet, expected):
        assert _infer_status_from_body(snippet) == expected

    def test_retorna_none_para_status_desconhecido(self):
        assert _infer_status_from_body("Apenas texto sem indicador de status.") is None

    def test_deprecated_tem_prioridade_sobre_completed(self):
        # Corpo com ambos — deprecated deve vencer por estar primeiro na lista
        body = "DEPRECATED — esta spec foi IMPLEMENTATION COMPLETE antes"
        assert _infer_status_from_body(body) == "deprecated"

    def test_scana_apenas_primeiros_600_chars(self):
        # Status escondido além dos 600 chars não deve ser detectado
        prefix = "X" * 700
        body = prefix + "\n✅ 100% Completa"
        assert _infer_status_from_body(body) is None


# ─── _extract_repos_from_body ─────────────────────────────────────────────────


class TestExtractReposFromBody:
    def test_extrai_repos_yaml_inline(self):
        body = "repos: [backend-service, frontend-app]"
        assert _extract_repos_from_body(body) == ["backend-service", "frontend-app"]

    def test_extrai_repos_markdown_bold(self):
        body = "**Repos**: backend-service, frontend-app"
        repos = _extract_repos_from_body(body)
        assert "backend-service" in repos
        assert "frontend-app" in repos

    def test_extrai_repos_sem_bold(self):
        body = "Repos: my-repo, other-repo"
        repos = _extract_repos_from_body(body)
        assert "my-repo" in repos

    def test_deduplica_repos(self):
        body = "**Repos**: backend-service, backend-service"
        assert _extract_repos_from_body(body).count("backend-service") == 1

    def test_retorna_vazio_sem_repos(self):
        body = "Nenhuma menção a repositórios aqui."
        assert _extract_repos_from_body(body) == []

    def test_scana_apenas_primeiros_1500_chars(self):
        # Repos muito longe no documento não devem ser detectados
        prefix = "X" * 1600
        body = prefix + "\nrepos: [late-repo]"
        assert _extract_repos_from_body(body) == []

    def test_remove_comentarios_markdown(self):
        body = "**Repos**: backend-service, # comentário"
        repos = _extract_repos_from_body(body)
        assert all(not r.startswith("#") for r in repos)


# ─── MarkdownFrontmatterParser.parse ──────────────────────────────────────────


class TestMarkdownFrontmatterParserParse:
    def setup_method(self):
        self.parser = MarkdownFrontmatterParser()
        self.dim = _FakeDimConfig(dimension="spec", pillar="Intent")

    def _write(self, tmp_path: Path, dirname: str, filename: str, content: str) -> Path:
        d = tmp_path / dirname
        d.mkdir(parents=True, exist_ok=True)
        f = d / filename
        f.write_text(content, encoding="utf-8")
        return f

    def test_can_parse_md(self, tmp_path):
        f = self._write(tmp_path, "x", "spec.md", "# Hello")
        assert self.parser.can_parse(f) is True

    def test_nao_parseia_yaml(self, tmp_path):
        f = self._write(tmp_path, "x", "config.yaml", "key: value")
        assert self.parser.can_parse(f) is False

    def test_id_gerado_do_slug_numerado(self, tmp_path):
        f = self._write(tmp_path, "043-past-events", "spec.md", "# Título")
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].node_id == "spec-043"

    def test_id_do_frontmatter_tem_prioridade(self, tmp_path):
        content = "---\nid: spec-override\n---\n# Título"
        f = self._write(tmp_path, "043-past-events", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].node_id == "spec-override"

    def test_titulo_extraido_do_h1(self, tmp_path):
        content = "# Meu Título Real\n\nAlgum texto."
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["title"] == "Meu Título Real"

    def test_titulo_do_frontmatter_tem_prioridade(self, tmp_path):
        content = "---\ntitle: Título do FM\n---\n# Título H1"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["title"] == "Título do FM"

    # ── Status ────────────────────────────────────────────────────────────────

    def test_status_do_frontmatter(self, tmp_path):
        content = "---\nstatus: in-progress\n---\n# Spec"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "in-progress"

    def test_status_inferido_do_corpo_quando_ausente_no_fm(self, tmp_path):
        content = "# Spec\n\n**Status**: ✅ 100% Completa"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "completed"

    def test_status_inferido_como_planned(self, tmp_path):
        content = "# Spec\n\n📋 Planejamento Completo"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "planned"

    def test_status_inferido_como_in_progress(self, tmp_path):
        content = "# Spec\n\n🚧 em andamento"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "in-progress"

    def test_status_frontmatter_nao_sobrescrito_pela_inferencia(self, tmp_path):
        # FM tem "completed", corpo tem "deprecated" — FM deve vencer
        content = "---\nstatus: completed\n---\n# Spec\n\nDEPRECATED"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "completed"

    def test_status_unknown_do_fm_usa_inferencia(self, tmp_path):
        # FM explicitamente "unknown" deve cair na inferência
        content = "---\nstatus: unknown\n---\n# Spec\n\n✅ 100% Completa"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "completed"

    def test_status_unknown_quando_sem_indicador(self, tmp_path):
        content = "# Spec\n\nTexto sem nenhum indicador de status."
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert result.nodes[0].properties["status"] == "unknown"

    # ── Repos ─────────────────────────────────────────────────────────────────

    def test_repos_do_frontmatter(self, tmp_path):
        content = "---\nrepos:\n  - backend-service\n  - frontend-app\n---\n# Spec"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        props = result.nodes[0].properties
        assert "backend-service" in props["repos"]
        assert "frontend-app" in props["repos"]

    def test_repos_extraidos_do_corpo(self, tmp_path):
        content = "# Spec\n\n**Repos**: backend-service, frontend-app"
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        props = result.nodes[0].properties
        assert "backend-service" in props["repos"]
        assert "frontend-app" in props["repos"]
        assert "repos_str" in props

    def test_repos_ausentes_quando_nao_mencionados(self, tmp_path):
        content = "# Spec\n\nTexto sem repos."
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        assert "repos" not in result.nodes[0].properties

    def test_repos_fm_tem_prioridade_sobre_corpo(self, tmp_path):
        content = textwrap.dedent("""\
            ---
            repos:
              - fm-repo
            ---
            # Spec

            **Repos**: body-repo
        """)
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        props = result.nodes[0].properties
        assert props["repos"] == ["fm-repo"]

    # ── Cross-refs ────────────────────────────────────────────────────────────

    def test_detecta_depends_on(self, tmp_path):
        content = "# Spec\n\nDepends on spec-010 para funcionar."
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        rels = {e.relationship for e in result.edges}
        assert "DEPENDS_ON" in rels

    def test_detecta_related_to(self, tmp_path):
        content = "# Spec\n\nRelated to spec-020."
        f = self._write(tmp_path, "001-test", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        rels = {e.relationship for e in result.edges}
        assert "RELATED_TO" in rels

    # ── Labels ────────────────────────────────────────────────────────────────

    def test_labels_inferidas_do_slug(self, tmp_path):
        content = "# Spec"
        f = self._write(tmp_path, "043-past-events-payment", "spec.md", content)
        result = self.parser.parse(f, self.dim)
        props = result.nodes[0].properties
        assert "payment" in props["labels"]
        assert "events" in props["labels"]

    # ── Arquivo inválido ──────────────────────────────────────────────────────

    def test_retorna_resultado_vazio_para_arquivo_ilegivel(self, tmp_path):
        f = tmp_path / "001-test" / "spec.md"
        f.parent.mkdir(parents=True, exist_ok=True)
        # Não cria o arquivo — parse deve tratar o FileNotFoundError
        result = self.parser.parse(f, self.dim)
        assert result.nodes == []
        assert result.edges == []
