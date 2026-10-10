"""
tests/test_spec_pipeline.py — Ingestão da dimensão spec: spec = pasta, incremental, poda e issues.

A orquestração (app/ingestor/spec_pipeline.py::run_spec_ingest) roda contra um store em memória
(tests/spec_store_fake.py) e um filesystem temporário; o GitHub é simulado com httpx.MockTransport.
As queries Cypher do store real são verificadas em TestSpecGraphStoreCypher e a rota em TestIngestRoute.
"""
from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from app.core.dimension_loader import load_dimensions
from app.core.parsers.base import EdgeData, NodeData
from app.core.spec_folders import parse_spec_folder
from app.core.spec_issues import SpecIssue, map_issue
from app.ingestor.relationship_builder import rebuild_spec_affects
from app.ingestor.spec_pipeline import SpecGraphStore, SpecIngestRequest, run_spec_ingest
from app.main import create_app
from tests.spec_store_fake import LONG_AGO, InMemorySpecStore
from tests.test_spec_folders import _make_specs, _spec_dim, _write

ISSUES_CFG = {"spec_issues": {"repo": "acme/specs-repo", "label": "spec"}}
EXCLUDES = ["**/node_modules/**"]


def _issue(number: int, title: str, *, state="open", state_reason=None, labels=("spec",), body="Corpo da issue.") -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/acme/specs-repo/issues/{number}",
        "updated_at": "2026-10-01T12:00:00Z",
        "labels": [{"name": n} for n in labels],
    }


class GithubStub:
    """Simula o GitHub: lista por label e busca por número. Guarda as requisições recebidas."""

    def __init__(self, issues: list[dict] | None = None, *, fail_with: int | None = None):
        self.issues = {i["number"]: i for i in (issues or [])}
        self.fail_with = fail_with
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_with:
            return httpx.Response(self.fail_with, json={"message": "Bad credentials"})
        path = request.url.path
        if path.endswith("/issues"):
            label = request.url.params["labels"]
            page = [i for i in self.issues.values() if any(lb["name"] == label for lb in i["labels"])]
            return httpx.Response(200, json=page)
        number = int(path.rsplit("/", 1)[1])
        if number not in self.issues:
            return httpx.Response(404, json={"message": "Not Found"})
        return httpx.Response(200, json=self.issues[number])

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


async def _run(store, root, *, request=None, cfg=None, github: GithubStub | None = None, token=""):
    client = github.client() if github else None
    try:
        return await run_spec_ingest(
            store,
            _spec_dim(str(root)),
            cfg=cfg or {},
            request=request,
            github_token=token,
            exclude_patterns=EXCLUDES,
            http_client=client,
        )
    finally:
        if client:
            await client.aclose()


@pytest.fixture
def specs_root(tmp_path) -> Path:
    return _make_specs(tmp_path)


# ═════════════════════════════════════════════════════════════════════════════
# Ingestão completa
# ═════════════════════════════════════════════════════════════════════════════


class TestFullIngest:
    @pytest.mark.asyncio
    async def test_cada_pasta_vira_um_no_e_nada_de_no_lixo(self, specs_root):
        store = InMemorySpecStore()

        result = await _run(store, specs_root)

        assert set(store.nodes) == {"spec-861", "spec-862"}  # sem spec-contracts / spec-README / spec-templates
        node = store.nodes["spec-861"]
        assert node["title"] == "Split the bill in chat"  # spec.md, não plan.md
        assert node["has_folder"] is True
        assert node["source_folder"] == "861-split-bill"
        assert result.details["folders_parsed"] == 2
        assert result.details["mode"] == "full"

    @pytest.mark.asyncio
    async def test_chunks_unicos_por_arquivo_sem_embedder(self, specs_root):
        store = InMemorySpecStore()

        result = await _run(store, specs_root)

        ids = store.chunk_ids("spec-861")
        assert {"spec-861__spec__chunk_0", "spec-861__plan__chunk_0", "spec-861__runbook__chunk_0"} <= ids
        assert {"spec-861__contracts-readme__chunk_0", "spec-861__contracts-api__chunk_0"} <= ids
        assert store.chunk_ids("spec-862") == {"spec-862__plan__chunk_0"}
        assert result.details["chunks_upserted"] == len(store.chunks)
        assert result.embedding_chunks == []  # embedder desligado: nada para embedar em background

    @pytest.mark.asyncio
    async def test_embedding_chunks_so_com_embedder_habilitado(self, specs_root):
        store = InMemorySpecStore()
        with patch("app.ingestor.spec_pipeline.is_embedder_enabled", return_value=True):
            result = await _run(store, specs_root)
        assert {c.node_id for c in result.embedding_chunks} == set(store.chunks)

    @pytest.mark.asyncio
    async def test_details_seguem_o_contrato(self, specs_root):
        result = await _run(InMemorySpecStore(), specs_root)

        for key in (
            "folders_parsed", "folders_deleted", "chunks_upserted", "issues_parsed",
            "issues_error", "ignored_paths", "pruned_nodes",
        ):
            assert key in result.details
        assert result.details["issues_error"] is None
        assert result.details["ignored_paths"] == []
        assert result.details["issues_enabled"] is False

    @pytest.mark.asyncio
    async def test_referencias_cruzadas_so_depois_de_todos_os_nos(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-a/spec.md", "# A\n\nDepends on spec-862 para pagar.")
        _write(root, "862-b/spec.md", "# B\n\nTexto.")
        store = InMemorySpecStore()

        await _run(store, root)

        names = store.call_names()
        assert names.index("upsert_edges") > max(i for i, n in enumerate(names) if n == "upsert_spec_nodes")
        assert [(e.from_id, e.relationship, e.to_id) for e in store.edges] == [("spec-861", "DEPENDS_ON", "spec-862")]

    @pytest.mark.asyncio
    async def test_pasta_duplicada_para_o_mesmo_id_vale_a_primeira(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "861-a/spec.md", "# A\n\nPrimeira.")
        _write(root, "861-b/spec.md", "# B\n\nSegunda.")
        store = InMemorySpecStore()

        result = await _run(store, root)

        assert store.nodes["spec-861"]["title"] == "A"
        assert result.details["duplicate_folders"] == ["861-b"]


# ═════════════════════════════════════════════════════════════════════════════
# Poda na ingestão completa
# ═════════════════════════════════════════════════════════════════════════════


class TestFullIngestPrune:
    def _seed_garbage(self, store: InMemorySpecStore, root: Path):
        # nós lixo do parser antigo (um nó por arquivo)
        store.seed_node("spec-contracts", file_path=f"{root}/861-split-bill/contracts/README.md", title="Contratos")
        store.seed_node("spec-README", file_path=f"{root}/README.md", title="Índice")
        # pasta que sumiu do disco
        store.seed_node("spec-999", has_folder=True, source_folder="999-removida", title="Removida")
        store.seed_chunk("spec-999__spec__chunk_0", "spec-999", chunk_source="folder")
        # o que NÃO pode ser podado
        store.seed_node("spec-manifest", title="veio de /ingest/manifest")  # sem file_path nem has_folder
        store.seed_node("spec-300", has_issue=True, issue_number=300, title="só issue")

    @pytest.mark.asyncio
    async def test_poda_nos_folder_nao_regravados_e_seus_chunks(self, specs_root):
        store = InMemorySpecStore()
        self._seed_garbage(store, specs_root)

        result = await _run(store, specs_root)

        assert "spec-contracts" not in store.nodes
        assert "spec-README" not in store.nodes
        assert "spec-999" not in store.nodes
        assert store.chunk_ids("spec-999") == set()
        assert result.details["pruned_nodes"] == 3
        assert {"spec-manifest", "spec-300", "spec-861", "spec-862"} <= set(store.nodes)

    @pytest.mark.asyncio
    async def test_no_legado_com_id_valido_e_regravado_nao_podado(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-861", file_path=f"{specs_root}/861-split-bill/plan.md", title="velho")

        await _run(store, specs_root)

        assert store.nodes["spec-861"]["title"] == "Split the bill in chat"
        assert store.nodes["spec-861"]["has_folder"] is True

    @pytest.mark.asyncio
    async def test_poda_chunks_velhos_de_arquivos_removidos_e_legados(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-861", has_folder=True, source_folder="861-split-bill")
        store.seed_chunk("spec-861__antigo__chunk_0", "spec-861", chunk_source="folder")  # arquivo que sumiu
        store.seed_chunk("spec-861__runbook__chunk_7", "spec-861", chunk_source="folder")  # runbook encolheu
        store.seed_chunk("spec-861__chunk_0", "spec-861", chunk_index=0)  # chunk legado do parser antigo
        store.seed_chunk("manual-chunk", "spec-861")  # chunk de outra origem: não é tocado

        await _run(store, specs_root)

        ids = store.chunk_ids("spec-861")
        assert "spec-861__antigo__chunk_0" not in ids
        assert "spec-861__runbook__chunk_7" not in ids
        assert "spec-861__chunk_0" not in ids
        assert "manual-chunk" in ids
        assert "spec-861__runbook__chunk_0" in ids

    @pytest.mark.asyncio
    async def test_sem_diretorio_fonte_nada_e_podado(self, tmp_path):
        store = InMemorySpecStore()
        self._seed_garbage(store, tmp_path / "specs")

        result = await _run(store, tmp_path / "nao-montado")

        assert {"spec-contracts", "spec-README", "spec-999", "spec-manifest", "spec-300"} <= set(store.nodes)
        assert result.details["pruned_nodes"] == 0
        assert result.details["folders_parsed"] == 0
        assert result.details["warnings"]

    @pytest.mark.asyncio
    async def test_diretorio_vazio_nada_e_podado(self, tmp_path):
        """Volume montado mas vazio (ex: bind mount de pasta inexistente) não pode zerar o grafo."""
        root = tmp_path / "specs"
        root.mkdir()
        store = InMemorySpecStore()
        self._seed_garbage(store, root)

        result = await _run(store, root)

        assert "spec-999" in store.nodes and "spec-contracts" in store.nodes
        assert result.details["pruned_nodes"] == 0
        assert any("nenhuma pasta" in w for w in result.details["warnings"])

    @pytest.mark.asyncio
    async def test_pasta_que_falhou_no_parse_nao_e_podada(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-862", has_folder=True, source_folder="862-outra", title="antes")
        store.seed_chunk("spec-862__plan__chunk_0", "spec-862", chunk_source="folder")

        real_parse = parse_spec_folder

        def flaky(folder, dim, parser=None):
            if folder.name == "862-outra":
                raise OSError("disco com soluço")
            return real_parse(folder, dim, parser)

        with patch("app.ingestor.spec_pipeline.parse_spec_folder", side_effect=flaky):
            result = await _run(store, specs_root)

        assert "spec-862" in store.nodes  # não podado
        assert "spec-862__plan__chunk_0" in store.chunks  # chunks velhos preservados
        assert result.details["folders_failed"] == 1
        assert result.details["folders_parsed"] == 1

    @pytest.mark.asyncio
    async def test_gravacao_parcial_no_neo4j_nao_vira_poda(self, specs_root):
        """O upsert em lote engole erros do Neo4j; contagem menor que o esperado = o lote não foi regravado."""

        class FlakyStore(InMemorySpecStore):
            async def upsert_spec_nodes(self, nodes):
                await super().upsert_spec_nodes([])  # só registra a chamada
                return 0  # Neo4j recusou o lote

        store = FlakyStore()
        store.seed_node("spec-862", has_folder=True, source_folder="862-outra", title="antes")
        store.seed_node("spec-861", has_folder=True, source_folder="861-split-bill", title="antes")
        store.seed_chunk("spec-862__plan__chunk_0", "spec-862", chunk_source="folder")

        result = await _run(store, specs_root)

        assert {"spec-861", "spec-862"} <= set(store.nodes)  # ingested_at velho, mas poupados
        assert "spec-862__plan__chunk_0" in store.chunks
        assert result.details["pruned_nodes"] == 0
        assert result.details["folders_failed"] == 2
        assert result.details["folders_parsed"] == 0

    @pytest.mark.asyncio
    async def test_pasta_sumida_com_issue_vira_no_so_issue(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node(
            "spec-999", has_folder=True, source_folder="999-removida", files=["spec.md"], file_path="/x/spec.md",
            has_issue=True, issue_number=999, title="Título velho da pasta",
        )
        store.seed_chunk("spec-999__spec__chunk_0", "spec-999", chunk_source="folder")

        result = await _run(store, specs_root)

        node = store.nodes["spec-999"]
        assert "has_folder" not in node and "source_folder" not in node and "files" not in node
        assert node["has_issue"] is True
        assert store.chunk_ids("spec-999") == set()
        assert result.details["folders_deleted"] == 1
        assert result.details["pruned_nodes"] == 0


# ═════════════════════════════════════════════════════════════════════════════
# Ingestão incremental
# ═════════════════════════════════════════════════════════════════════════════


class TestIncrementalIngest:
    @pytest.mark.asyncio
    async def test_reparse_so_das_pastas_afetadas(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)
        store.calls.clear()
        untouched_at = store.nodes["spec-862"]["ingested_at"]

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["specs/861-split-bill/RUNBOOK.md"]))

        upserted = [c[1] for c in store.calls if c[0] == "upsert_spec_nodes"]
        assert upserted == [["spec-861"]]
        assert store.nodes["spec-862"]["ingested_at"] == untouched_at
        assert result.details["mode"] == "incremental"
        assert result.details["folders_parsed"] == 1

    @pytest.mark.asyncio
    async def test_apaga_chunks_antigos_da_pasta_antes_do_upsert(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)
        # RUNBOOK.md encolheu e contracts/api.md foi removido desde a última ingestão
        store.seed_chunk("spec-861__runbook__chunk_5", "spec-861", chunk_source="folder")
        store.seed_chunk("spec-861__removido__chunk_0", "spec-861", chunk_source="folder")
        store.seed_chunk("spec-861__issue__chunk_0", "spec-861", chunk_source="issue")  # chunk de issue: preservado
        store.calls.clear()

        await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["861-split-bill/RUNBOOK.md"]))

        ids = store.chunk_ids("spec-861")
        assert "spec-861__runbook__chunk_5" not in ids
        assert "spec-861__removido__chunk_0" not in ids
        assert "spec-861__issue__chunk_0" in ids
        assert "spec-861__runbook__chunk_0" in ids  # regravado
        names = store.call_names()
        assert names.index("delete_folder_chunks") < names.index("upsert_chunks")
        assert store.calls[names.index("delete_folder_chunks")][1] == ["spec-861"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "specs/861-split-bill/RUNBOOK.md",  # relativo ao repo
            "861-split-bill/RUNBOOK.md",  # relativo à pasta de specs
            "docs/specs/861-split-bill/contracts/api.md",  # prefixo longo + subpasta
        ],
    )
    async def test_caminhos_com_e_sem_prefixo_mapeiam_para_a_mesma_pasta(self, specs_root, path):
        store = InMemorySpecStore()

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=[path]))

        assert set(store.nodes) == {"spec-861"}
        assert result.details["ignored_paths"] == []

    @pytest.mark.asyncio
    async def test_varios_arquivos_da_mesma_pasta_reparseiam_uma_vez(self, specs_root):
        store = InMemorySpecStore()
        paths = ["specs/861-split-bill/spec.md", "specs/861-split-bill/RUNBOOK.md", "specs/862-outra/plan.md"]

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=paths))

        assert result.details["folders_parsed"] == 2

    @pytest.mark.asyncio
    async def test_caminhos_que_nao_mapeiam_vao_para_ignored_paths(self, specs_root):
        store = InMemorySpecStore()
        request = SpecIngestRequest(spec_paths=["specs/README.md", "docs/guia.md", "specs/861-split-bill/spec.md"])

        result = await _run(store, specs_root, request=request)

        assert result.details["ignored_paths"] == ["specs/README.md", "docs/guia.md"]
        assert set(store.nodes) == {"spec-861"}

    @pytest.mark.asyncio
    async def test_so_caminhos_ignorados_nao_vira_ingestao_completa(self, specs_root):
        store = InMemorySpecStore()

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["docs/guia.md"]))

        assert store.nodes == {}
        assert result.details["mode"] == "incremental"
        assert result.details["folders_parsed"] == 0

    @pytest.mark.asyncio
    async def test_pasta_removida_apaga_chunks_e_no(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)
        shutil.rmtree(specs_root / "862-outra")

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["specs/862-outra/plan.md"]))

        assert "spec-862" not in store.nodes
        assert store.chunk_ids("spec-862") == set()
        assert "spec-861" in store.nodes  # o resto fica
        assert result.details["folders_deleted"] == 1
        assert result.details["folders_parsed"] == 0

    @pytest.mark.asyncio
    async def test_pasta_removida_com_dados_de_issue_mantem_o_no(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)
        store.nodes["spec-862"].update(has_issue=True, issue_number=862)
        shutil.rmtree(specs_root / "862-outra")

        await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["862-outra/plan.md"]))

        node = store.nodes["spec-862"]
        assert node["has_issue"] is True
        assert "has_folder" not in node and "source_folder" not in node
        assert store.chunk_ids("spec-862") == set()

    @pytest.mark.asyncio
    async def test_incremental_nunca_poda(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-contracts", file_path=f"{specs_root}/README.md")
        store.seed_node("spec-999", has_folder=True, source_folder="999-removida")

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["861-split-bill/spec.md"]))

        assert {"spec-contracts", "spec-999"} <= set(store.nodes)
        assert result.details["pruned_nodes"] == 0
        assert "purge_stale_chunks" not in store.call_names()

    @pytest.mark.asyncio
    async def test_diretorio_vazio_nao_apaga_nos_no_incremental(self, tmp_path):
        root = tmp_path / "specs"
        root.mkdir()
        store = InMemorySpecStore()
        store.seed_node("spec-862", has_folder=True, source_folder="862-outra")

        result = await _run(store, root, request=SpecIngestRequest(spec_paths=["862-outra/plan.md"]))

        assert "spec-862" in store.nodes
        assert result.details["folders_deleted"] == 0
        assert result.details["warnings"]


# ═════════════════════════════════════════════════════════════════════════════
# Issues do GitHub
# ═════════════════════════════════════════════════════════════════════════════


class TestIssues:
    @pytest.mark.asyncio
    async def test_issue_sem_pasta_define_titulo_summary_e_status(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub([_issue(877, "Spec NNN — Convites por link", body="Permitir convidar por link.")])

        result = await _run(store, specs_root, cfg=ISSUES_CFG, github=github)

        node = store.nodes["spec-877"]  # NNN = número da issue
        assert node["title"] == "Convites por link"
        assert node["summary"] == "Permitir convidar por link."
        assert node["status"] == "planned"
        assert node["issue_number"] == 877
        assert node["issue_url"] == "https://github.com/acme/specs-repo/issues/877"
        assert node["issue_state"] == "open"
        assert node["has_issue"] is True
        assert "has_folder" not in node
        assert "spec-877__issue__chunk_0" in store.chunks
        assert store.chunks["spec-877__issue__chunk_0"]["source_file"] == "issue#877"
        assert result.details["issues_parsed"] == 1
        assert result.details["issues_error"] is None

    @pytest.mark.asyncio
    async def test_status_por_state_reason(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub(
            [
                _issue(901, "Spec 901 — Feita", state="closed", state_reason="completed"),
                _issue(902, "Spec 902 — Descartada", state="closed", state_reason="not_planned"),
                _issue(903, "Spec 903 — Aberta"),
            ]
        )

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github)

        assert store.nodes["spec-901"]["status"] == "done"
        assert store.nodes["spec-902"]["status"] == "deprecated"
        assert store.nodes["spec-903"]["status"] == "planned"
        assert store.nodes["spec-901"]["issue_state_reason"] == "completed"

    @pytest.mark.asyncio
    async def test_no_com_pasta_nao_tem_titulo_nem_status_sobrescritos_pela_issue(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub([_issue(861, "Spec 861 — Título da ISSUE", state="closed", state_reason="completed")])

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github)

        node = store.nodes["spec-861"]
        assert node["title"] == "Split the bill in chat"  # da pasta
        assert node["status"] == "planned"  # da pasta (frontmatter), não "done" da issue
        assert node["summary"] == "Splits the bill in the chat."  # da pasta, não "Corpo da issue."
        assert node["has_issue"] is True and node["issue_state"] == "closed"  # issue_* sempre entram
        assert {"spec-861__issue__chunk_0", "spec-861__spec__chunk_0"} <= store.chunk_ids("spec-861")

    @pytest.mark.asyncio
    async def test_issues_sao_processadas_antes_das_pastas_na_completa(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub([_issue(861, "Spec 861 — Título da ISSUE")])

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github)

        names = store.call_names()
        assert names.index("upsert_issue_nodes") < names.index("upsert_spec_nodes")

    @pytest.mark.asyncio
    async def test_incremental_de_issue_nao_sobrescreve_no_com_pasta(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)  # pastas primeiro, sem issues
        github = GithubStub([_issue(861, "Spec 861 — Título da ISSUE", state="closed", state_reason="not_planned")])

        result = await _run(
            store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[861])
        )

        node = store.nodes["spec-861"]
        assert node["title"] == "Split the bill in chat"
        assert node["status"] == "planned"
        assert node["issue_state_reason"] == "not_planned"
        assert result.details["issues_parsed"] == 1
        assert result.details["folders_parsed"] == 0  # só a issue foi tocada
        assert [r.url.path for r in github.requests] == ["/repos/acme/specs-repo/issues/861"]

    @pytest.mark.asyncio
    async def test_incremental_de_issue_sem_pasta_cria_o_no(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub([_issue(950, "Spec 950 — Nova ideia")])

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[950]))

        assert store.nodes["spec-950"]["title"] == "Nova ideia"

    @pytest.mark.asyncio
    async def test_incremental_substitui_os_chunks_do_corpo_da_issue(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-950", has_issue=True, issue_number=950)
        store.seed_chunk("spec-950__issue__chunk_9", "spec-950", chunk_source="issue")
        github = GithubStub([_issue(950, "Spec 950 — Nova ideia", body="Corpo curto.")])

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[950]))

        assert store.chunk_ids("spec-950") == {"spec-950__issue__chunk_0"}

    @pytest.mark.asyncio
    async def test_issue_que_mudou_de_nnn_nao_deixa_no_fantasma(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-950", has_issue=True, issue_number=950, title="Spec 950 antiga")
        github = GithubStub([_issue(950, "Spec 951 — Renumerada")])

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[950]))

        assert "spec-950" not in store.nodes
        assert store.nodes["spec-951"]["issue_number"] == 950

    @pytest.mark.asyncio
    async def test_401_do_github_nao_derruba_a_ingestao_e_nao_poda_issues(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-300", has_issue=True, issue_number=300, title="só issue")
        store.seed_chunk("spec-300__issue__chunk_0", "spec-300", chunk_source="issue")
        github = GithubStub(fail_with=401)

        result = await _run(store, specs_root, cfg=ISSUES_CFG, github=github, token="ghp_x")

        assert "401" in result.details["issues_error"]
        assert result.details["issues_parsed"] == 0
        assert result.details["folders_parsed"] == 2  # pastas seguem normalmente
        assert {"spec-861", "spec-862"} <= set(store.nodes)
        assert "spec-300" in store.nodes and "spec-300__issue__chunk_0" in store.chunks  # nada de poda de issue
        assert "purge_stale_chunks" in store.call_names()  # a poda de pastas segue
        assert all(c != ("purge_stale_chunks", "issue") for c in store.calls)

    @pytest.mark.asyncio
    async def test_erro_de_rede_tambem_nao_derruba(self, specs_root):
        store = InMemorySpecStore()

        def boom(request):
            raise httpx.ConnectError("sem rota")

        client = httpx.AsyncClient(transport=httpx.MockTransport(boom))
        result = await run_spec_ingest(
            store, _spec_dim(str(specs_root)), cfg=ISSUES_CFG, http_client=client, exclude_patterns=EXCLUDES
        )
        await client.aclose()

        assert result.details["issues_error"]
        assert result.details["folders_parsed"] == 2

    @pytest.mark.asyncio
    async def test_incremental_com_401_registra_erro_e_segue_com_as_pastas(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub(fail_with=401)
        request = SpecIngestRequest(spec_paths=["861-split-bill/spec.md"], issue_numbers=[861])

        result = await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=request)

        assert "401" in result.details["issues_error"]
        assert "spec-861" in store.nodes

    @pytest.mark.asyncio
    async def test_issue_que_perdeu_o_label_apaga_no_so_issue_na_completa(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-300", has_issue=True, issue_number=300, title="perdeu o label")
        store.seed_chunk("spec-300__issue__chunk_0", "spec-300", chunk_source="issue")
        github = GithubStub([])  # nenhuma issue com o label

        result = await _run(store, specs_root, cfg=ISSUES_CFG, github=github)

        assert "spec-300" not in store.nodes
        assert store.chunk_ids("spec-300") == set()
        assert result.details["pruned_nodes"] == 1
        assert result.details["issues_error"] is None

    @pytest.mark.asyncio
    async def test_issue_que_perdeu_o_label_so_perde_issue_se_o_no_tem_pasta(self, specs_root):
        store = InMemorySpecStore()
        await _run(store, specs_root)
        store.nodes["spec-861"].update(has_issue=True, issue_number=861, issue_state="open")
        store.seed_chunk("spec-861__issue__chunk_0", "spec-861", chunk_source="issue")

        await _run(store, specs_root, cfg=ISSUES_CFG, github=GithubStub([]))

        node = store.nodes["spec-861"]
        assert "has_issue" not in node and "issue_number" not in node
        assert node["title"] == "Split the bill in chat"
        assert "spec-861__issue__chunk_0" not in store.chunks
        assert "spec-861__spec__chunk_0" in store.chunks

    @pytest.mark.asyncio
    async def test_incremental_issue_sem_label_ou_inexistente_remove_dados_de_issue(self, specs_root):
        store = InMemorySpecStore()
        store.seed_node("spec-300", has_issue=True, issue_number=300)
        store.seed_node("spec-301", has_issue=True, issue_number=301)
        github = GithubStub([_issue(300, "Spec 300 — x", labels=("bug",))])  # 301 não existe (404)

        await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[300, 301]))

        assert "spec-300" not in store.nodes and "spec-301" not in store.nodes

    @pytest.mark.asyncio
    async def test_recurso_desligado_nao_chama_o_github(self, specs_root):
        store = InMemorySpecStore()
        github = GithubStub([_issue(877, "Spec NNN — x")])

        result = await _run(store, specs_root, cfg={"spec_issues": {"repo": "", "label": "spec"}}, github=github)

        assert github.requests == []
        assert result.details["issues_enabled"] is False
        assert result.details["issues_error"] is None

    @pytest.mark.asyncio
    async def test_issue_numbers_com_recurso_desligado_avisa(self, specs_root):
        store = InMemorySpecStore()

        result = await _run(store, specs_root, request=SpecIngestRequest(issue_numbers=[861]))

        assert "não configurado" in result.details["issues_error"]

    @pytest.mark.asyncio
    async def test_repo_malformado_vira_issues_error(self, specs_root):
        store = InMemorySpecStore()

        result = await _run(store, specs_root, cfg={"spec_issues": {"repo": "isto/nao/vale"}})

        assert "inválido" in result.details["issues_error"]
        assert result.details["folders_parsed"] == 2


# ═════════════════════════════════════════════════════════════════════════════
# AFFECTS (Spec -> Service) recalculadas no fim da ingestão
# ═════════════════════════════════════════════════════════════════════════════


def _store_with_service() -> InMemorySpecStore:
    store = InMemorySpecStore()
    store.seed_service("service-backend", "acme-backend")  # spec 861 declara repos: [acme-backend]
    return store


class TestSpecAffects:
    @pytest.mark.asyncio
    async def test_completa_recalcula_as_affects_de_todas_as_specs(self, specs_root):
        store = _store_with_service()

        result = await _run(store, specs_root)

        assert store.affects == {("spec-861", "service-backend"): True}
        assert store.calls[-1] == ("rebuild_affects", None)  # None = todas, e é a última etapa
        assert result.details["affects_edges"] == 1
        assert result.edges_upserted >= 1

    @pytest.mark.asyncio
    async def test_incremental_recalcula_so_as_specs_tocadas(self, specs_root):
        store = _store_with_service()
        await _run(store, specs_root)
        store.affects[("spec-862", "service-backend")] = True  # de outra spec: não pode ser tocada
        store.calls.clear()

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["specs/861-split-bill/RUNBOOK.md"]))

        assert ("rebuild_affects", ["spec-861"]) in store.calls
        assert ("spec-862", "service-backend") in store.affects
        assert result.details["affects_edges"] == 1

    @pytest.mark.asyncio
    async def test_repos_removido_do_frontmatter_nao_deixa_aresta_velha(self, specs_root):
        store = _store_with_service()
        await _run(store, specs_root)
        assert ("spec-861", "service-backend") in store.affects
        spec_md = specs_root / "861-split-bill" / "spec.md"
        spec_md.write_text(spec_md.read_text().replace("repos: [acme-backend]\n", ""), encoding="utf-8")

        result = await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["861-split-bill/spec.md"]))

        assert "repos" not in store.nodes["spec-861"]  # o upsert não deixa o repos velho no nó
        assert ("spec-861", "service-backend") not in store.affects
        assert result.details["affects_edges"] == 0

    @pytest.mark.asyncio
    async def test_aresta_affects_manual_sem_created_at_e_preservada(self, specs_root):
        store = _store_with_service()
        store.affects[("spec-861", "service-feito-a-mao")] = False  # manifest/manual: sem created_at

        await _run(store, specs_root)

        assert store.affects[("spec-861", "service-feito-a-mao")] is False
        assert ("spec-861", "service-backend") in store.affects

    @pytest.mark.asyncio
    async def test_pasta_removida_com_issue_recalcula_a_spec_rebaixada(self, specs_root):
        store = _store_with_service()
        await _run(store, specs_root)
        store.nodes["spec-861"].update(has_issue=True, issue_number=861)
        shutil.rmtree(specs_root / "861-split-bill")

        await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["861-split-bill/spec.md"]))

        assert "repos" not in store.nodes["spec-861"]  # repos saiu junto com a pasta
        assert ("rebuild_affects", ["spec-861"]) in store.calls
        assert ("spec-861", "service-backend") not in store.affects

    @pytest.mark.asyncio
    async def test_no_apagado_leva_as_affects_junto(self, specs_root):
        store = _store_with_service()
        await _run(store, specs_root)
        shutil.rmtree(specs_root / "861-split-bill")

        await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["861-split-bill/spec.md"]))

        assert "spec-861" not in store.nodes and store.affects == {}

    @pytest.mark.asyncio
    async def test_sem_nada_tocado_nao_recalcula(self, specs_root):
        store = _store_with_service()

        await _run(store, specs_root, request=SpecIngestRequest(spec_paths=["docs/guia.md"]))
        github = GithubStub([_issue(950, "Spec 950 — X")])
        await _run(store, specs_root, cfg=ISSUES_CFG, github=github, request=SpecIngestRequest(issue_numbers=[950]))

        assert "rebuild_affects" not in store.call_names()

    @pytest.mark.asyncio
    async def test_sem_diretorio_fonte_a_completa_nao_recalcula(self, tmp_path):
        store = _store_with_service()

        await _run(store, tmp_path / "nao-montado")

        assert "rebuild_affects" not in store.call_names()


# ═════════════════════════════════════════════════════════════════════════════
# Contrato do corpo da requisição
# ═════════════════════════════════════════════════════════════════════════════


class TestSpecIngestRequest:
    def test_vazio_e_completo(self):
        assert SpecIngestRequest().is_incremental is False
        assert SpecIngestRequest(spec_paths=[], issue_numbers=[]).is_incremental is False

    def test_com_paths_ou_issues_e_incremental(self):
        assert SpecIngestRequest(spec_paths=["861-x/spec.md"]).is_incremental is True
        assert SpecIngestRequest(issue_numbers=[861]).is_incremental is True

    @pytest.mark.parametrize("bad", [0, -3])
    def test_numero_de_issue_precisa_ser_positivo(self, bad):
        with pytest.raises(ValueError):
            SpecIngestRequest(issue_numbers=[bad])


# ═════════════════════════════════════════════════════════════════════════════
# Queries Cypher do store real
# ═════════════════════════════════════════════════════════════════════════════


def _recording_driver(data=None, single=None):
    result = MagicMock()
    result.data = AsyncMock(return_value=data if data is not None else [])
    result.single = AsyncMock(return_value=single if single is not None else {"affected": 1})
    session = AsyncMock()
    session.run = AsyncMock(return_value=result)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver, session


def _last_call(session):
    args = session.run.call_args.args
    return args[0], args[1]


class TestSpecGraphStoreCypher:
    @pytest.mark.asyncio
    async def test_issue_so_define_title_summary_status_sem_pasta(self):
        driver, session = _recording_driver()
        m = map_issue(
            SpecIssue.from_api(_issue(900, "Spec 861 — Split", state="closed", state_reason="completed")),
            MagicMock(label="spec"),
        )

        n = await SpecGraphStore(driver, "acme").upsert_issue_nodes([m])

        cypher, params = _last_call(session)
        assert n == 1
        assert params["domain_id"] == "acme"
        row = params["rows"][0]
        assert row["id"] == "spec-861"
        assert row["issue_props"]["has_issue"] is True
        assert row["issue_props"]["issue_state_reason"] == "completed"
        assert set(row["doc_props"]) >= {"title", "summary", "status"}
        assert not set(row["doc_props"]) & set(row["issue_props"])  # title/summary/status nunca em issue_props
        # a decisão "pasta manda" está na própria query, atômica por nó
        assert "MERGE (n:Spec {id: row.id, domain_id: $domain_id})" in cypher
        assert "SET n += row.issue_props" in cypher
        assert "coalesce(n.has_folder, false) THEN [] ELSE [1]" in cypher
        assert "SET n += row.doc_props" in cypher
        assert cypher.index("row.issue_props") < cypher.index("row.doc_props")

    @pytest.mark.asyncio
    async def test_delete_folder_chunks_so_de_origem_folder_ou_legado_do_pai(self):
        driver, session = _recording_driver(single={"affected": 4})

        n = await SpecGraphStore(driver, "acme").delete_folder_chunks(["spec-861", "spec-862"])

        cypher, params = _last_call(session)
        assert n == 4
        assert params["parent_ids"] == ["spec-861", "spec-862"] and params["domain_id"] == "acme"
        assert "c.parent_id IN $parent_ids" in cypher
        assert "c.chunk_source = 'folder'" in cypher
        assert "c.chunk_source IS NULL" in cypher  # legado do parser antigo
        assert "'issue'" not in cypher  # chunks de issue não são tocados

    @pytest.mark.asyncio
    async def test_delete_chunks_por_origem(self):
        driver, session = _recording_driver()

        await SpecGraphStore(driver).delete_chunks(["spec-861"], "issue")

        cypher, params = _last_call(session)
        assert params["source"] == "issue" and params["domain_id"] == "default"
        assert "$source IS NULL OR c.chunk_source = $source" in cypher

    @pytest.mark.asyncio
    async def test_delete_nodes_apaga_chunks_antes_dos_nos(self):
        driver, session = _recording_driver()

        await SpecGraphStore(driver).delete_nodes(["spec-999"])

        queries = [c.args[0] for c in session.run.call_args_list]
        assert "DocumentChunk" in queries[0] and "DETACH DELETE c" in queries[0]
        assert "(n:Spec" in queries[1] and "DETACH DELETE n" in queries[1]

    @pytest.mark.asyncio
    async def test_upsert_chunks_liga_ao_pai_com_query_rotulada(self):
        driver, session = _recording_driver()
        chunk = NodeData(
            node_labels=["DocumentChunk", "Intent"],
            node_id="spec-861__runbook__chunk_0",
            properties={"parent_id": "spec-861", "content": "x", "chunk_source": "folder"},
        )

        with patch("app.ingestor.spec_pipeline.ingest_nodes", new=AsyncMock(return_value=1)) as ingest:
            n = await SpecGraphStore(driver, "acme").upsert_chunks([chunk])

        assert n == 1
        ingest.assert_awaited_once()
        cypher, params = _last_call(session)
        assert params["rows"] == [{"chunk_id": "spec-861__runbook__chunk_0", "parent_id": "spec-861"}]
        assert "MATCH (c:DocumentChunk {id: row.chunk_id, domain_id: $domain_id})" in cypher
        assert "MATCH (p:Spec {id: row.parent_id, domain_id: $domain_id})" in cypher
        assert "MERGE (c)-[r:CHUNK_OF]->(p)" in cypher

    @pytest.mark.asyncio
    async def test_find_prunable_usa_inicio_da_execucao_e_raiz_das_specs(self):
        driver, session = _recording_driver(data=[{"id": "spec-contracts"}, {"id": "spec-README"}])
        started = datetime(2026, 10, 10, tzinfo=timezone.utc)

        ids = await SpecGraphStore(driver).find_prunable_folder_nodes(started, "/specs/", ["862-outra"])

        cypher, params = _last_call(session)
        assert ids == ["spec-contracts", "spec-README"]
        assert params["started_at"] == started
        assert params["root_prefix"] == "/specs/" and params["skip_folders"] == ["862-outra"]
        assert "n.ingested_at < $started_at" in cypher
        assert "coalesce(n.has_issue, false) = false" in cypher  # nunca poda nó com dados de issue
        assert "n.file_path STARTS WITH $root_prefix" in cypher  # pega o lixo do parser antigo

    @pytest.mark.asyncio
    async def test_toda_query_e_restrita_ao_domain_id(self):
        driver, session = _recording_driver()
        store = SpecGraphStore(driver, "acme")
        started = datetime(2026, 10, 10, tzinfo=timezone.utc)

        await store.delete_folder_chunks(["spec-1"])
        await store.delete_chunks(["spec-1"], None)
        await store.delete_nodes(["spec-1"])
        await store.strip_folder_data(["spec-1"])
        await store.strip_issue_data(["spec-1"])
        await store.purge_stale_chunks("folder", started)
        await store.purge_legacy_chunks()
        await store.list_folder_names()
        await store.find_nodes_by_folder(["861-x"])
        await store.find_prunable_folder_nodes(started, "/specs/", [])
        await store.find_issue_nodes_not_in([])
        await store.find_issue_nodes_by_number(1)

        for call in session.run.call_args_list:
            cypher, params = call.args
            assert params["domain_id"] == "acme", cypher
            assert "domain_id: $domain_id" in cypher, cypher

    @pytest.mark.asyncio
    async def test_arestas_entre_specs_usam_query_rotulada_por_tipo(self):
        driver, session = _recording_driver(single={"affected": 2})
        edges = [
            EdgeData(from_id="spec-861", to_id="spec-860", relationship="DEPENDS_ON"),
            EdgeData(from_id="spec-861", to_id="spec-859", relationship="DEPENDS_ON"),
            EdgeData(from_id="spec-861", to_id="spec-100", relationship="SUPERSEDES"),
            EdgeData(from_id="spec-861", to_id="spec-1", relationship="X]->(z) DETACH DELETE z //"),  # injeção: descartada
        ]

        total = await SpecGraphStore(driver, "acme").upsert_edges(edges)

        queries = [c.args[0] for c in session.run.call_args_list]
        assert total == 4  # 2 tipos x affected=2
        assert len(queries) == 2
        assert any("MERGE (a)-[r:DEPENDS_ON]->(b)" in q for q in queries)
        assert any("MERGE (a)-[r:SUPERSEDES]->(b)" in q for q in queries)
        assert all("MATCH (a:Spec {id: row.from_id, domain_id: $domain_id})" in q for q in queries)
        assert not any("DELETE" in q for q in queries)
        depends = next(c for c in session.run.call_args_list if "DEPENDS_ON" in c.args[0])
        assert depends.args[1]["rows"] == [
            {"from_id": "spec-861", "to_id": "spec-860"},
            {"from_id": "spec-861", "to_id": "spec-859"},
        ]

    @pytest.mark.asyncio
    async def test_rebaixar_no_a_so_issue_remove_tambem_o_repos(self):
        driver, session = _recording_driver()

        await SpecGraphStore(driver).strip_folder_data(["spec-1"])

        cypher = session.run.call_args.args[0]
        assert "n.has_folder" in cypher and "n.repos" in cypher and "n.repos_str" in cypher

    @pytest.mark.asyncio
    async def test_listas_grandes_sao_divididas_em_lotes(self):
        driver, session = _recording_driver()

        await SpecGraphStore(driver).strip_issue_data([f"spec-{i}" for i in range(1200)])

        sizes = [len(c.args[1]["ids"]) for c in session.run.call_args_list]
        assert sizes == [500, 500, 200]


class TestRebuildSpecAffects:
    """Helper compartilhado por run_spec_ingest e ingest_all (app/ingestor/relationship_builder.py)."""

    @pytest.mark.asyncio
    async def test_remove_as_velhas_antes_de_criar(self):
        driver, session = _recording_driver(single={"deleted": 2, "created": 3})

        created = await rebuild_spec_affects(driver, "acme")

        queries = [c.args[0] for c in session.run.call_args_list]
        assert created == 3
        assert len(queries) == 2
        assert "DELETE r" in queries[0] and "[r:AFFECTS]->(:Service)" in queries[0]
        assert "MERGE (spec)-[r:AFFECTS]->(svc)" in queries[1]
        for call in session.run.call_args_list:
            assert call.args[1] == {"domain_id": "acme", "spec_ids": None}  # None = todas as specs
            assert "domain_id: $domain_id" in call.args[0]

    @pytest.mark.asyncio
    async def test_so_remove_as_que_o_mecanismo_criou(self):
        """Toda AFFECTS criada aqui nasce com created_at; as de /ingest/manifest ou manuais não têm."""
        driver, session = _recording_driver(single={"deleted": 0, "created": 0})

        await rebuild_spec_affects(driver, "default")

        delete_cypher, create_cypher = (c.args[0] for c in session.run.call_args_list)
        assert "r.created_at IS NOT NULL" in delete_cypher
        assert "ON CREATE SET r.created_at = datetime()" in create_cypher

    @pytest.mark.asyncio
    async def test_restringe_as_specs_pedidas(self):
        driver, session = _recording_driver(single={"deleted": 0, "created": 1})

        await rebuild_spec_affects(driver, "default", ["spec-861"])

        for call in session.run.call_args_list:
            assert call.args[1]["spec_ids"] == ["spec-861"]
            assert "$spec_ids IS NULL OR spec.id IN $spec_ids" in call.args[0]

    @pytest.mark.asyncio
    async def test_replace_false_so_cria(self):
        driver, session = _recording_driver(single={"created": 1})

        await rebuild_spec_affects(driver, "default", replace=False)

        assert session.run.call_count == 1 and "MERGE" in session.run.call_args.args[0]

    @pytest.mark.asyncio
    async def test_sem_registro_conta_zero(self):
        driver, session = _recording_driver()
        session.run.return_value.single = AsyncMock(return_value=None)

        assert await rebuild_spec_affects(driver, "default") == 0

    @pytest.mark.asyncio
    async def test_store_delega_ao_helper_no_dominio_do_store(self):
        with patch("app.ingestor.spec_pipeline.rebuild_spec_affects", new=AsyncMock(return_value=4)) as helper:
            driver = MagicMock()
            n = await SpecGraphStore(driver, "acme").rebuild_affects(["spec-1"])

        assert n == 4
        helper.assert_awaited_once_with(driver, "acme", ["spec-1"])


# ═════════════════════════════════════════════════════════════════════════════
# Rota POST /api/v1/ingest/spec
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture
def route_env(specs_root):
    """TestClient com o store em memória, o diretório de specs temporário e a config isolada."""
    store = InMemorySpecStore()
    real_load = load_dimensions

    def load_with_tmp_root(dimensions_dir, names):
        return [replace(d, source_path=str(specs_root)) for d in real_load(dimensions_dir, names)]

    cfg = {"active_dimensions": ["spec"], "dimensions_dir": "app/dimensions", "ingest": {"exclude_patterns": EXCLUDES}}
    with patch("app.routes.ingest.get_driver", return_value=MagicMock()), \
         patch("app.routes.ingest.apply_index", new=AsyncMock()), \
         patch("app.routes.ingest._load_config", return_value=cfg), \
         patch("app.routes.ingest.load_dimensions", side_effect=load_with_tmp_root), \
         patch("app.routes.ingest.SpecGraphStore", return_value=store):
        yield TestClient(create_app()), store


class TestIngestRoute:
    def test_sem_corpo_faz_ingestao_completa(self, route_env):
        client, store = route_env

        res = client.post("/api/v1/ingest/spec")

        assert res.status_code == 200
        body = res.json()
        assert body["details"]["mode"] == "full"
        assert body["details"]["folders_parsed"] == 2
        assert set(store.nodes) == {"spec-861", "spec-862"}
        for key in ("folders_parsed", "folders_deleted", "chunks_upserted", "issues_parsed", "issues_error", "ignored_paths", "pruned_nodes"):
            assert key in body["details"]
        assert body["details"]["parser"] == "builtin.markdown_frontmatter"

    def test_corpo_vazio_tambem_e_completo(self, route_env):
        client, store = route_env

        res = client.post("/api/v1/ingest/spec", json={})

        assert res.status_code == 200
        assert res.json()["details"]["mode"] == "full"
        assert len(store.nodes) == 2

    def test_corpo_json_literal_vazio_e_completo(self, route_env):
        client, _ = route_env
        res = client.post("/api/v1/ingest/spec", content=b"", headers={"Content-Type": "application/json"})
        assert res.status_code == 200
        assert res.json()["details"]["mode"] == "full"

    def test_spec_paths_faz_ingestao_incremental(self, route_env):
        client, store = route_env

        res = client.post(
            "/api/v1/ingest/spec",
            json={"spec_paths": ["specs/861-split-bill/RUNBOOK.md", "README.md"], "issue_numbers": []},
        )

        assert res.status_code == 200
        details = res.json()["details"]
        assert details["mode"] == "incremental"
        assert details["folders_parsed"] == 1
        assert details["ignored_paths"] == ["README.md"]
        assert set(store.nodes) == {"spec-861"}

    def test_issue_numbers_sem_recurso_ligado_informa_no_issues_error(self, route_env):
        client, _ = route_env

        res = client.post("/api/v1/ingest/spec", json={"issue_numbers": [861]})

        assert res.status_code == 200
        assert "não configurado" in res.json()["details"]["issues_error"]

    def test_corpo_invalido_e_422(self, route_env):
        client, _ = route_env
        assert client.post("/api/v1/ingest/spec", json={"issue_numbers": [0]}).status_code == 422
        assert client.post("/api/v1/ingest/spec", json={"spec_paths": "861-x/spec.md"}).status_code == 422

    def test_ingest_all_faz_a_ingestao_completa_da_spec(self, route_env):
        client, store = route_env

        res = client.post("/api/v1/ingest")

        assert res.status_code == 200
        spec_result = next(r for r in res.json()["results"] if r["dim_key"] == "spec")
        assert spec_result["details"]["mode"] == "full"
        assert set(store.nodes) == {"spec-861", "spec-862"}

    def test_ingest_all_recalcula_as_affects_pelo_mesmo_helper(self, route_env):
        client, _ = route_env

        with patch("app.routes.ingest.rebuild_spec_affects", new=AsyncMock(return_value=7)) as helper:
            res = client.post("/api/v1/ingest", headers={"X-Domain-ID": "acme"})

        assert res.status_code == 200
        helper.assert_awaited_once()
        assert helper.await_args.args[1] == "acme" and len(helper.await_args.args) == 2  # todas as specs do domínio
        spec_edges = next(r for r in res.json()["results"] if r["dim_key"] == "spec")["edges_upserted"]
        assert res.json()["total_edges"] == spec_edges + 7

    def test_ingest_all_nao_cai_se_o_recalculo_das_affects_falhar(self, route_env):
        client, _ = route_env

        with patch("app.routes.ingest.rebuild_spec_affects", new=AsyncMock(side_effect=RuntimeError("neo4j fora"))):
            res = client.post("/api/v1/ingest")

        assert res.status_code == 200

    def test_resposta_da_spec_traz_affects_edges(self, route_env):
        client, store = route_env
        store.seed_service("service-backend", "acme-backend")

        res = client.post("/api/v1/ingest/spec")

        assert res.json()["details"]["affects_edges"] == 1
        assert store.affects == {("spec-861", "service-backend"): True}

    def test_dimensao_sem_group_by_ignora_o_corpo(self, tmp_path):
        """O corpo só vale para spec = pasta; outras dimensões seguem o pipeline por arquivo."""
        from app.routes import ingest as ingest_module

        captured = {}

        async def fake_pipeline(dim, plugins_dir, background_tasks, domain_id="default", cfg=None, spec_request=None):
            captured["dim"] = dim.dimension
            captured["spec_request"] = spec_request
            return ingest_module.IngestResponse(
                dim_key=dim.dimension, nodes_upserted=0, edges_upserted=0, indexes_applied=0, message="ok"
            )

        cfg = {"active_dimensions": ["adr"], "dimensions_dir": "app/dimensions"}
        with patch("app.routes.ingest._load_config", return_value=cfg), \
             patch("app.routes.ingest._run_ingest_pipeline", side_effect=fake_pipeline):
            res = TestClient(create_app()).post("/api/v1/ingest/adr", json={"spec_paths": ["x"]})

        assert res.status_code == 200
        assert captured["dim"] == "adr"
        assert captured["spec_request"].spec_paths == ["x"]  # chega à função, que só usa em group_by
