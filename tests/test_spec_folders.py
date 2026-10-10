"""
tests/test_spec_folders.py — Modo "spec = pasta" (dimension.group_by: numbered_folder).

Cobre:
  - agrupamento: vários .md numa pasta numerada viram UM nó; README aninhado não vira spec
  - precedência do arquivo primário (spec.md > README.md > plan.md > alfabético)
  - ids de chunk únicos entre arquivos; chunks gerados mesmo sem embedder
  - mapeamento caminho -> pasta (com e sem prefixo "specs/")
  - as outras dimensões que usam markdown_frontmatter continuam como estavam
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.dimension_loader import DimensionConfig, load_dimension
from app.core.parsers.builtin.markdown_frontmatter import MarkdownFrontmatterParser
from app.core.spec_folders import (
    MAX_FILE_BYTES,
    choose_primary,
    discover_spec_folders,
    extract_spec_folder_name,
    file_slug,
    parse_spec_folder,
    spec_id_from_folder_name,
)

SPEC_YAML = Path(__file__).parent.parent / "app" / "dimensions" / "spec.yaml"


def _spec_dim(source_path: str = "/specs") -> DimensionConfig:
    return DimensionConfig(
        dimension="spec",
        node_label="Spec",
        pillar="Intent",
        parser="builtin.markdown_frontmatter",
        source_type="filesystem",
        source_path=source_path,
        source_patterns=["**/*.md"],
        extra={"group_by": "numbered_folder"},
    )


def _write(root: Path, rel: str, content: str) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _make_specs(tmp_path: Path) -> Path:
    root = tmp_path / "specs"
    _write(root, "README.md", "# Índice de specs\n\nNão é uma spec.")
    _write(
        root,
        "861-split-bill/spec.md",
        "---\ntitle: Split the bill in chat\nstatus: planned\nrepos: [acme-backend]\n---\n# Split the bill in chat\n\nSplits the bill in the chat.\n",
    )
    _write(root, "861-split-bill/plan.md", "# Plano\n\nFases 0, 1 e 2 da divisão.\n")
    _write(root, "861-split-bill/RUNBOOK.md", "# Runbook\n\nReiniciar o worker de webhook.\n")
    _write(root, "861-split-bill/contracts/README.md", "# Contratos\n\nOpenAPI do webhook.\n")
    _write(root, "861-split-bill/contracts/api.md", "# API\n\nPOST /webhook\n")
    _write(root, "862-outra/plan.md", "# Outra spec\n\nSó tem plan.md.\n")
    _write(root, "_templates/modelo.md", "# Modelo\n\nFora de pasta numerada.\n")
    return root


# ─── yaml da dimensão ─────────────────────────────────────────────────────────


class TestSpecDimensionYaml:
    def test_spec_yaml_ativa_agrupamento_por_pasta(self):
        dim = load_dimension(SPEC_YAML)
        assert dim is not None
        assert dim.group_by == "numbered_folder"
        assert dim.source_patterns == ["**/*.md"]
        assert dim.parser == "builtin.markdown_frontmatter"

    def test_spec_yaml_declara_indice_fulltext_do_corpo(self):
        dim = load_dimension(SPEC_YAML)
        cyphers = " ".join(i.cypher for i in dim.indexes)
        assert "CREATE FULLTEXT INDEX doc_chunk_fts IF NOT EXISTS FOR (n:DocumentChunk) ON EACH [n.content]" in cyphers

    @pytest.mark.parametrize("name", ["adr", "concept", "history", "workflow", "service"])
    def test_outras_dimensoes_nao_usam_group_by(self, name):
        dim = load_dimension(SPEC_YAML.parent / f"{name}.yaml")
        assert dim is not None
        assert dim.group_by == ""


# ─── mapeamento caminho -> pasta ──────────────────────────────────────────────


class TestExtractSpecFolderName:
    @pytest.mark.parametrize(
        "path,expected",
        [
            ("specs/861-split-bill/RUNBOOK.md", "861-split-bill"),  # relativo ao repo
            ("861-split-bill/RUNBOOK.md", "861-split-bill"),  # relativo à pasta de specs
            ("861-split-bill/contracts/api.md", "861-split-bill"),  # arquivo em subpasta
            ("docs/specs/861-x/spec.md", "861-x"),  # prefixo com vários níveis
            ("specs/1000-quatro-digitos/spec.md", "1000-quatro-digitos"),  # 3+ dígitos
            ("specs/861-a/002-b/spec.md", "002-b"),  # o mais próximo do arquivo
            ("specs\\861-x\\spec.md", "861-x"),  # separador do Windows
            ("specs/861-x/", "861-x"),  # a própria pasta
            ("861-x", "861-x"),
        ],
    )
    def test_caminhos_que_mapeiam(self, path, expected):
        assert extract_spec_folder_name(path) == expected

    @pytest.mark.parametrize(
        "path",
        [
            "README.md",
            "docs/README.md",
            "specs/README.md",
            "specs/_templates/modelo.md",
            "specs/12-curto/spec.md",  # menos de 3 dígitos
            "specs/001-intro.md",  # 'arquivo' numerado não é pasta
            "../861-x/spec.md",  # sai da raiz
            "",
            "   ",
        ],
    )
    def test_caminhos_que_nao_mapeiam(self, path):
        assert extract_spec_folder_name(path) is None

    def test_spec_id_a_partir_do_nome_da_pasta(self):
        assert spec_id_from_folder_name("861-split-bill") == "spec-861"
        assert spec_id_from_folder_name("1000-x") == "spec-1000"
        assert spec_id_from_folder_name("docs") is None


# ─── descoberta ───────────────────────────────────────────────────────────────


class TestDiscoverSpecFolders:
    def test_agrupa_por_pasta_numerada_e_ignora_o_resto(self, tmp_path):
        root = _make_specs(tmp_path)
        folders = discover_spec_folders(root, ["**/*.md"])

        assert list(folders) == ["861-split-bill", "862-outra"]
        rels = sorted(folders["861-split-bill"].rel(f) for f in folders["861-split-bill"].files)
        assert rels == ["RUNBOOK.md", "contracts/README.md", "contracts/api.md", "plan.md", "spec.md"]

    def test_arquivo_acima_de_1mb_e_ignorado(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-x/spec.md", "# X\n\nOk.")
        _write(root, "861-x/dump.md", "a" * (MAX_FILE_BYTES + 1))
        folders = discover_spec_folders(root, ["**/*.md"])
        assert [folders["861-x"].rel(f) for f in folders["861-x"].files] == ["spec.md"]

    def test_respeita_exclude_patterns(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-x/spec.md", "# X")
        _write(root, "861-x/node_modules/pkg/README.md", "# lixo")
        folders = discover_spec_folders(root, ["**/*.md"], ["**/node_modules/**"])
        assert [folders["861-x"].rel(f) for f in folders["861-x"].files] == ["spec.md"]

    def test_pasta_numerada_dentro_de_diretorio_agrupador(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "fase-1/861-x/spec.md", "# X")
        folders = discover_spec_folders(root, ["**/*.md"])
        assert list(folders) == ["861-x"]


# ─── arquivo primário ─────────────────────────────────────────────────────────


class TestChoosePrimary:
    @pytest.mark.parametrize(
        "files,expected",
        [
            (["plan.md", "README.md", "spec.md", "RUNBOOK.md"], "spec.md"),
            (["plan.md", "README.md", "RUNBOOK.md"], "README.md"),
            (["RUNBOOK.md", "plan.md", "TECH_DESIGN.md"], "plan.md"),
            (["TECH_DESIGN.md", "RUNBOOK.md", "PROJECT_BRIEF.md"], "PROJECT_BRIEF.md"),  # alfabético
            (["contracts/README.md", "RUNBOOK.md"], "RUNBOOK.md"),  # README aninhado não conta; raiz antes
            (["contracts/spec.md", "RUNBOOK.md"], "RUNBOOK.md"),  # spec.md aninhado também não
            (["contracts/b.md", "contracts/a.md"], "contracts/a.md"),
            (["Spec.md", "plan.md"], "Spec.md"),  # sem diferenciar maiúsculas
        ],
    )
    def test_precedencia(self, files, expected):
        assert choose_primary(files) == expected


# ─── parse de uma pasta ───────────────────────────────────────────────────────


class TestParseSpecFolder:
    def test_uma_pasta_gera_um_no_com_dados_do_primario(self, tmp_path):
        root = _make_specs(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])["861-split-bill"]

        parsed = parse_spec_folder(folder, _spec_dim(str(root)))

        assert parsed is not None
        props = parsed.node.properties
        assert parsed.node.node_id == "spec-861"
        assert parsed.node.node_labels == ["Spec", "Intent"]
        assert props["title"] == "Split the bill in chat"  # do spec.md, não do plan.md
        assert props["status"] == "planned"
        assert props["repos"] == ["acme-backend"]
        assert props["has_folder"] is True
        assert props["source_folder"] == "861-split-bill"
        assert props["primary_file"] == "spec.md"
        assert props["files"] == ["plan.md", "RUNBOOK.md", "spec.md", "contracts/api.md", "contracts/README.md"]
        assert props["files_str"] == " ".join(props["files"])
        assert props["number"] == 861
        assert props["file_path"].endswith("861-split-bill/spec.md")

    def test_readme_aninhado_nao_vira_spec(self, tmp_path):
        root = _make_specs(tmp_path)
        folders = discover_spec_folders(root, ["**/*.md"])
        nodes = [parse_spec_folder(f, _spec_dim(str(root))).node for f in folders.values()]
        ids = {n.node_id for n in nodes}
        assert ids == {"spec-861", "spec-862"}
        assert "spec-contracts" not in ids and "spec-README" not in ids

    def test_so_plan_md_e_o_primario(self, tmp_path):
        root = _make_specs(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])["862-outra"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        assert parsed.node.node_id == "spec-862"
        assert parsed.node.properties["title"] == "Outra spec"
        assert parsed.node.properties["files"] == ["plan.md"]

    def test_frontmatter_id_do_primario_vence(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-x/spec.md", "---\nid: spec-split\ntitle: Split\n---\n# Split\n\nTexto.")
        folder = discover_spec_folders(root, ["**/*.md"])["861-x"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        assert parsed.node.node_id == "spec-split"
        assert all(c.properties["parent_id"] == "spec-split" for c in parsed.chunks)

    def test_pasta_com_quatro_digitos_gera_id_spec_1000(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "1000-grande/spec.md", "# Grande\n\nTexto.")
        folder = discover_spec_folders(root, ["**/*.md"])["1000-grande"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        assert parsed.node.node_id == "spec-1000"
        assert parsed.node.properties["number"] == 1000

    def test_chunks_por_arquivo_com_ids_unicos(self, tmp_path):
        root = _make_specs(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])["861-split-bill"]

        parsed = parse_spec_folder(folder, _spec_dim(str(root)))

        ids = [c.node_id for c in parsed.chunks]
        assert len(ids) == len(set(ids)), "ids de chunk colidiram entre arquivos"
        assert all("__chunk_" in i for i in ids)
        assert "spec-861__runbook__chunk_0" in ids
        assert "spec-861__spec__chunk_0" in ids
        assert "spec-861__plan__chunk_0" in ids
        assert "spec-861__contracts-readme__chunk_0" in ids
        assert "spec-861__contracts-api__chunk_0" in ids

        runbook = next(c for c in parsed.chunks if c.node_id == "spec-861__runbook__chunk_0")
        assert runbook.node_labels == ["DocumentChunk", "Intent"]
        assert runbook.properties["parent_id"] == "spec-861"
        assert runbook.properties["source_file"] == "861-split-bill/RUNBOOK.md"
        assert runbook.properties["chunk_source"] == "folder"
        assert "worker de webhook" in runbook.properties["content"]
        # frontmatter não vai para o conteúdo do chunk
        spec_chunk = next(c for c in parsed.chunks if c.node_id == "spec-861__spec__chunk_0")
        assert "repos:" not in spec_chunk.properties["content"]

    def test_chunks_sao_gerados_sem_embedder(self, tmp_path):
        from unittest.mock import patch

        root = _make_specs(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])["862-outra"]
        with patch("app.core.parsers.builtin.markdown_frontmatter.is_embedder_enabled", return_value=False):
            parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        assert [c.node_id for c in parsed.chunks] == ["spec-862__plan__chunk_0"]

    def test_slugs_de_arquivo_que_colidem_ficam_distintos(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-x/spec.md", "# X\n\nTexto.")
        _write(root, "861-x/a-b.md", "# A-B\n\nPrimeiro.")
        _write(root, "861-x/a/b.md", "# A/B\n\nSegundo.")
        folder = discover_spec_folders(root, ["**/*.md"])["861-x"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        ids = [c.node_id for c in parsed.chunks]
        assert len(ids) == len(set(ids))

    def test_referencias_cruzadas_vem_so_do_primario(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-x/spec.md", "# X\n\nDepends on spec-860 para auth.")
        _write(root, "861-x/RUNBOOK.md", "# R\n\nDepends on spec-999 aqui.")
        folder = discover_spec_folders(root, ["**/*.md"])["861-x"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)))
        assert [(e.relationship, e.to_id) for e in parsed.edges] == [("DEPENDS_ON", "spec-860")]

    def test_file_slug(self):
        assert file_slug("RUNBOOK.md") == "runbook"
        assert file_slug("contracts/API v2.md") == "contracts-api-v2"
        assert file_slug("Descrição_técnica.md") == "descricao-tecnica"
        assert file_slug("TECH_DESIGN.md") == "tech-design"  # sem "_", que colidiria com o marcador __chunk_
        assert file_slug("???.md") == "doc"


# ─── compatibilidade: as demais dimensões não mudam ───────────────────────────


class TestOtherDimensionsUnchanged:
    def test_parser_sem_folder_name_mantem_um_no_por_arquivo(self, tmp_path):
        """adr/concept/history/workflow chamam parse(file, cfg): id continua vindo de file.parent.name."""
        f = _write(tmp_path / "adr", "007-use-temporal/README.md", "# Usar Temporal\n\nDecisão.")
        cfg = DimensionConfig(dimension="adr", node_label="ADR", pillar="System")
        res = MarkdownFrontmatterParser().parse(f, cfg)
        assert [n.node_id for n in res.nodes] == ["adr-007"]
        assert res.nodes[0].properties["title"] == "Usar Temporal"
        assert "has_folder" not in res.nodes[0].properties
        assert "files" not in res.nodes[0].properties

    def test_pasta_com_quatro_digitos_segue_sem_numero_fora_do_modo_pasta(self, tmp_path):
        f = _write(tmp_path, "1000-foo/README.md", "# Foo\n\nTexto.")
        cfg = DimensionConfig(dimension="adr", node_label="ADR", pillar="System")
        res = MarkdownFrontmatterParser().parse(f, cfg)
        # comportamento legado preservado (regex de 3 dígitos exatos)
        assert res.nodes[0].node_id == "adr-1000-foo"

    def test_dimensao_sem_group_by_nao_dispara_o_pipeline_de_pasta(self):
        for name in ("adr", "concept", "history", "workflow"):
            dim = load_dimension(SPEC_YAML.parent / f"{name}.yaml")
            assert dim.group_by != "numbered_folder"
