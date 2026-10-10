"""
tests/test_spec_numeros_repetidos.py — Várias pastas com o MESMO número (ex: 217-alpha-..., 217-beta-...).

Regra (igual na ingestão completa e na incremental): entre TODAS as pastas do disco com o mesmo número, em ordem
alfabética, a primeira mantém `spec-NNN` e as outras ficam com `spec-NNN-<slug>` (slug = resto do nome da pasta).
Chunks e referências cruzadas da pasta usam o id novo; as issues continuam enriquecendo `spec-NNN`. Um
frontmatter.id próprio tem prioridade; só ele pode colidir (a segunda pasta é descartada e reportada).

Se a dona sai do disco, a regra recalculada pela listagem atual promove a próxima pasta a `spec-NNN` e o
`spec-NNN-<slug>` antigo dela deixa de existir (apagado na incremental, podado na completa). Os dois modos têm
que chegar ao MESMO grafo — os testes de transição comparam um com o outro.
"""
from __future__ import annotations

import copy
import shutil
from unittest.mock import patch

import pytest

from app.core.spec_folders import assign_spec_ids, discover_spec_folders, parse_spec_folder
from app.ingestor.spec_pipeline import SpecIngestRequest
from tests.spec_store_fake import InMemorySpecStore
from tests.test_spec_folders import _spec_dim, _write
from tests.test_spec_pipeline import ISSUES_CFG, GithubStub, _issue
from tests.test_spec_pipeline import _run as _run_once

ALPHA = "217-alpha-draft-link"
BETA = "217-beta-split-null"
GAMMA = "217-gamma-register-dup"


def _make_217(tmp_path):
    root = tmp_path / "specs"
    _write(
        root, f"{ALPHA}/spec.md",
        "---\ntitle: Link de convite do rascunho\nrepos: [acme-backend]\n---\n# Link de convite\n\nO link de convite aparece no rascunho.\n",
    )
    _write(root, f"{BETA}/spec.md", "# Divisão com valor nulo\n\nA divisão falha na criação com valor nulo.\n")
    _write(
        root, f"{GAMMA}/spec.md",
        "# Registro com e-mail duplicado\n\nConfirmação ao cadastrar e-mail repetido. Depends on spec-217 para o convite.\n",
    )
    _write(root, f"{GAMMA}/RUNBOOK.md", "# Runbook\n\nReenviar a confirmação de e-mail.\n")
    return root


async def _run(store: InMemorySpecStore, root, **kwargs):
    """Uma execução depois de passar o tempo: nós gravados antes ficam mais velhos que o início desta."""
    store.age()
    return await _run_once(store, root, **kwargs)


def _snapshot(store: InMemorySpecStore) -> dict:
    """O que importa para comparar dois caminhos até o mesmo estado: nós de pasta, seus chunks e AFFECTS."""
    return {
        "nodes": {i: (n.get("source_folder"), n.get("title")) for i, n in sorted(store.nodes.items()) if n.get("has_folder")},
        "chunks": {cid: c.get("source_file") for cid, c in sorted(store.chunks.items())},
        "affects": dict(sorted(store.affects.items())),
    }


# ─── a regra, isolada ─────────────────────────────────────────────────────────


class TestAssignSpecIds:
    def test_primeira_em_ordem_alfabetica_fica_com_spec_nnn(self):
        ids = assign_spec_ids([GAMMA, ALPHA, BETA])
        assert ids == {
            ALPHA: "spec-217",
            BETA: "spec-217-beta-split-null",
            GAMMA: "spec-217-gamma-register-dup",
        }

    def test_numero_unico_nao_muda(self):
        assert assign_spec_ids(["861-split-bill", "862-outra"]) == {"861-split-bill": "spec-861", "862-outra": "spec-862"}

    def test_numeros_diferentes_sao_independentes(self):
        ids = assign_spec_ids(["217-b", "217-a", "218-z", "218-a", "1000-x"])
        assert ids == {"217-a": "spec-217", "217-b": "spec-217-b", "218-a": "spec-218", "218-z": "spec-218-z", "1000-x": "spec-1000"}

    def test_ordem_nao_diferencia_maiusculas_e_nao_depende_da_ordem_de_entrada(self):
        assert assign_spec_ids(["217-b", "217-A"]) == assign_spec_ids(["217-A", "217-b"]) == {"217-A": "spec-217", "217-b": "spec-217-b"}

    def test_slug_nao_pode_conter_o_marcador_de_chunk(self):
        ids = assign_spec_ids(["217-a", "217-b__chunk_9"])
        assert "__" not in ids["217-b__chunk_9"]

    def test_nomes_que_nao_sao_de_spec_sao_ignorados(self):
        assert assign_spec_ids(["docs", "217-a"]) == {"217-a": "spec-217"}


class TestParseWithAssignedId:
    def test_chunks_e_referencias_usam_o_id_novo(self, tmp_path):
        root = _make_217(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])[GAMMA]
        new_id = "spec-217-gamma-register-dup"

        parsed = parse_spec_folder(folder, _spec_dim(str(root)), spec_id=new_id)

        assert parsed.node.node_id == new_id and parsed.node.properties["id"] == new_id
        assert parsed.node.properties["number"] == 217  # o número continua o da pasta
        assert parsed.node.properties["source_folder"] == GAMMA
        assert {c.node_id for c in parsed.chunks} == {f"{new_id}__spec__chunk_0", f"{new_id}__runbook__chunk_0"}
        assert all(c.properties["parent_id"] == new_id for c in parsed.chunks)
        # a referência a spec-217 NÃO é descartada como auto-referência: vai para a dona
        assert [(e.from_id, e.relationship, e.to_id) for e in parsed.edges] == [(new_id, "DEPENDS_ON", "spec-217")]

    def test_frontmatter_id_proprio_tem_prioridade(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "217-b/spec.md", "---\nid: spec-meu-id\n---\n# B\n\nTexto.")
        folder = discover_spec_folders(root, ["**/*.md"])["217-b"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)), spec_id="spec-217-b")
        assert parsed.node.node_id == "spec-meu-id"

    def test_frontmatter_id_que_so_repete_o_padrao_e_re_identificado(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "217-b/spec.md", "---\nid: spec-217\n---\n# B\n\nTexto.")
        folder = discover_spec_folders(root, ["**/*.md"])["217-b"]
        parsed = parse_spec_folder(folder, _spec_dim(str(root)), spec_id="spec-217-b")
        assert parsed.node.node_id == "spec-217-b"

    def test_sem_spec_id_o_comportamento_e_o_de_antes(self, tmp_path):
        root = _make_217(tmp_path)
        folder = discover_spec_folders(root, ["**/*.md"])[GAMMA]
        assert parse_spec_folder(folder, _spec_dim(str(root))).node.node_id == "spec-217"


# ─── ingestão completa ────────────────────────────────────────────────────────


class TestFullIngestRepeatedNumbers:
    @pytest.mark.asyncio
    async def test_tres_pastas_217_viram_tres_nos(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()

        result = await _run(store, root)

        assert {i: n["source_folder"] for i, n in store.nodes.items()} == {
            "spec-217": ALPHA,
            "spec-217-beta-split-null": BETA,
            "spec-217-gamma-register-dup": GAMMA,
        }
        assert store.nodes["spec-217"]["title"] == "Link de convite do rascunho"
        assert result.details["folders_parsed"] == 3
        assert result.details["duplicate_folders"] == [
            {"folder": BETA, "id": "spec-217-beta-split-null"},
            {"folder": GAMMA, "id": "spec-217-gamma-register-dup"},
        ]
        assert "id_collisions" not in result.details
        assert result.details["pruned_nodes"] == 0

    @pytest.mark.asyncio
    async def test_chunks_da_pasta_re_identificada_usam_o_id_novo(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()

        await _run(store, root)

        register = "spec-217-gamma-register-dup"
        assert store.chunk_ids(register) == {f"{register}__spec__chunk_0", f"{register}__runbook__chunk_0"}
        assert store.chunks[f"{register}__runbook__chunk_0"]["source_file"] == f"{GAMMA}/RUNBOOK.md"
        assert store.chunk_ids("spec-217") == {"spec-217__spec__chunk_0"}  # a dona não herda chunks das outras
        assert all("__chunk_" in cid for cid in store.chunks)

    @pytest.mark.asyncio
    async def test_cross_refs_para_spec_217_vao_para_a_dona(self, tmp_path):
        root = _make_217(tmp_path)
        _write(root, "300-outra/spec.md", "# Outra\n\nDepends on spec-217 para o convite.\n")
        store = InMemorySpecStore()

        await _run(store, root)

        edges = {(e.from_id, e.relationship, e.to_id) for e in store.edges}
        assert ("spec-300", "DEPENDS_ON", "spec-217") in edges
        assert ("spec-217-gamma-register-dup", "DEPENDS_ON", "spec-217") in edges

    @pytest.mark.asyncio
    async def test_issue_enriquece_a_dona(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        github = GithubStub([_issue(217, "Spec 217 — Convite por link", state="closed", state_reason="completed")])

        result = await _run(store, root, cfg=ISSUES_CFG, github=github)

        owner = store.nodes["spec-217"]
        assert owner["issue_number"] == 217 and owner["has_issue"] is True
        assert owner["title"] == "Link de convite do rascunho"  # a pasta manda
        for other in ("spec-217-beta-split-null", "spec-217-gamma-register-dup"):
            assert "issue_number" not in store.nodes[other] and "has_issue" not in store.nodes[other]
        assert result.details["issues_parsed"] == 1

    @pytest.mark.asyncio
    async def test_poda_nao_apaga_os_re_identificados_e_limpa_o_que_sobrou(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        # sobras: uma pasta 217 que não existe mais e o lixo do parser por arquivo
        store.seed_node("spec-217-fantasma", has_folder=True, source_folder="217-fantasma")
        store.seed_chunk("spec-217-fantasma__spec__chunk_0", "spec-217-fantasma", chunk_source="folder")
        store.seed_node("spec-contracts", file_path=f"{root}/{ALPHA}/contracts/README.md")

        first = await _run(store, root)
        second = await _run(store, root)  # reingestão: nada novo a podar

        assert "spec-217-fantasma" not in store.nodes and "spec-contracts" not in store.nodes
        assert store.chunk_ids("spec-217-fantasma") == set()
        assert first.details["pruned_nodes"] == 2
        assert second.details["pruned_nodes"] == 0
        assert {"spec-217", "spec-217-beta-split-null", "spec-217-gamma-register-dup"} <= set(store.nodes)

    @pytest.mark.asyncio
    async def test_estado_da_regra_antiga_e_no_op(self, tmp_path):
        """Na regra antiga só a primeira pasta era gravada (spec-217); nada existia para os duplicados."""
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        store.seed_node("spec-217", has_folder=True, source_folder=ALPHA, title="velho")
        store.seed_chunk("spec-217__spec__chunk_0", "spec-217", chunk_source="folder")

        result = await _run(store, root)

        assert result.details["pruned_nodes"] == 0
        assert store.nodes["spec-217"]["title"] == "Link de convite do rascunho"
        assert len(store.nodes) == 3

    @pytest.mark.asyncio
    async def test_affects_da_pasta_re_identificada(self, tmp_path):
        root = _make_217(tmp_path)
        _write(
            root, f"{GAMMA}/spec.md",
            "---\nrepos: [acme-backend]\n---\n# Registro\n\nConfirmação.\n",
        )
        store = InMemorySpecStore()
        store.seed_service("service-backend", "acme-backend")

        await _run(store, root)

        assert set(store.affects) == {("spec-217", "service-backend"), ("spec-217-gamma-register-dup", "service-backend")}


# ─── ingestão incremental ─────────────────────────────────────────────────────


class TestIncrementalRepeatedNumbers:
    @pytest.mark.asyncio
    async def test_tocar_so_a_segunda_mantem_o_id_com_slug(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()

        result = await _run(store, root, request=SpecIngestRequest(spec_paths=[f"specs/{BETA}/spec.md"]))

        beta = "spec-217-beta-split-null"
        assert store.nodes[beta]["source_folder"] == BETA
        assert store.nodes["spec-217"]["source_folder"] == ALPHA  # a dona continua sendo a primeira do disco
        assert "spec-217" not in {i for i, n in store.nodes.items() if n["source_folder"] == BETA}
        assert {d["folder"] for d in result.details["duplicate_folders"]} == {BETA, GAMMA}

    @pytest.mark.asyncio
    async def test_tocar_so_a_terceira_nao_a_promove_a_spec_217(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        before = _snapshot(store)

        await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{GAMMA}/RUNBOOK.md"]))

        assert store.nodes["spec-217-gamma-register-dup"]["source_folder"] == GAMMA
        assert _snapshot(store) == before  # idempotente: o mesmo grafo da completa

    @pytest.mark.asyncio
    async def test_incremental_reprocessa_as_irmas_de_mesmo_numero(self, tmp_path):
        root = _make_217(tmp_path)
        _write(root, "862-outra/spec.md", "# Outra\n\nNão relacionada.")
        store = InMemorySpecStore()

        result = await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{BETA}/spec.md"]))

        assert result.details["folders_parsed"] == 3  # as três 217; a 862 não foi tocada
        assert "spec-862" not in store.nodes

    @pytest.mark.asyncio
    async def test_chunks_antigos_da_irma_re_identificada_sao_apagados(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        register = "spec-217-gamma-register-dup"
        store.seed_chunk(f"{register}__antigo__chunk_0", register, chunk_source="folder")

        await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{GAMMA}/spec.md"]))

        assert f"{register}__antigo__chunk_0" not in store.chunks
        assert f"{register}__runbook__chunk_0" in store.chunks

    @pytest.mark.asyncio
    async def test_irma_com_falha_no_parse_nao_perde_o_no(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        real_parse = parse_spec_folder

        def flaky(folder, dim, parser=None, **kwargs):
            if folder.name == BETA:
                raise OSError("disco com soluço")
            return real_parse(folder, dim, parser, **kwargs)

        with patch("app.ingestor.spec_pipeline.parse_spec_folder", side_effect=flaky):
            result = await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{GAMMA}/spec.md"]))

        assert "spec-217-beta-split-null" in store.nodes
        assert result.details["folders_failed"] == 1


# ─── a dona sai / uma nova entra antes: completa e incremental chegam ao mesmo grafo ──────────


class TestOwnershipTransitions:
    @pytest.mark.asyncio
    async def test_dona_removida_promove_a_proxima_igual_nos_dois_modos(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        shutil.rmtree(root / ALPHA)  # a dona (spec-217) sai do disco
        incremental, full = copy.deepcopy(store), copy.deepcopy(store)
        incremental.calls.clear()

        res_inc = await _run(incremental, root, request=SpecIngestRequest(spec_paths=[f"specs/{ALPHA}/spec.md"]))
        res_full = await _run(full, root)

        # a próxima pasta alfabética (beta) é promovida a spec-217; o spec-217-beta-... antigo deixa de existir
        assert incremental.nodes["spec-217"]["source_folder"] == BETA
        assert "spec-217-beta-split-null" not in incremental.nodes
        assert incremental.chunk_ids("spec-217-beta-split-null") == set()
        assert incremental.nodes["spec-217-gamma-register-dup"]["source_folder"] == GAMMA
        assert _snapshot(incremental) == _snapshot(full)
        # o nó spec-217 é reaproveitado (sobrescrito), nunca apagado e recriado: as arestas que outras specs têm
        # para ele (DEPENDS_ON -> spec-217) sobreviveriam só até a próxima completa
        deleted = [c[1] for c in incremental.calls if c[0] == "delete_nodes"]
        assert deleted == [["spec-217-beta-split-null"]]
        assert res_inc.details["folders_deleted"] == 1
        assert res_full.details["pruned_nodes"] == 1  # o spec-217-beta-... antigo
        assert res_inc.details["duplicate_folders"] == [
            {"folder": GAMMA, "id": "spec-217-gamma-register-dup"}
        ]

    @pytest.mark.asyncio
    async def test_dona_removida_sem_irmas_apaga_o_no(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "217-a/spec.md", "# A\n\nÚnica.")
        _write(root, "218-b/spec.md", "# B\n\nOutra.")
        store = InMemorySpecStore()
        await _run(store, root)
        shutil.rmtree(root / "217-a")

        await _run(store, root, request=SpecIngestRequest(spec_paths=["217-a/spec.md"]))

        assert "spec-217" not in store.nodes and "spec-218" in store.nodes

    @pytest.mark.asyncio
    async def test_no_da_dona_com_issue_nao_e_rebaixado_quando_outra_pasta_assume_o_id(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        store.nodes["spec-217"].update(has_issue=True, issue_number=217, issue_state="open")
        shutil.rmtree(root / ALPHA)
        store.calls.clear()

        await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{ALPHA}/spec.md"]))

        assert not any(c[0] == "strip_folder_data" and "spec-217" in c[1] for c in store.calls)  # nem rebaixado
        owner = store.nodes["spec-217"]
        assert owner["has_issue"] is True and owner["issue_number"] == 217  # a issue #217 segue na dona
        assert owner["source_folder"] == BETA and owner["has_folder"] is True

    @pytest.mark.asyncio
    async def test_pasta_nova_antes_da_dona_assume_spec_nnn_igual_nos_dois_modos(self, tmp_path):
        root = _make_217(tmp_path)
        store = InMemorySpecStore()
        await _run(store, root)
        _write(root, "217-aaa-nova/spec.md", "# Nova\n\nPasta que ordena antes das outras.")
        incremental, full = copy.deepcopy(store), copy.deepcopy(store)

        await _run(incremental, root, request=SpecIngestRequest(spec_paths=["217-aaa-nova/spec.md"]))
        await _run(full, root)

        assert incremental.nodes["spec-217"]["source_folder"] == "217-aaa-nova"
        assert incremental.nodes["spec-217-alpha-draft-link"]["source_folder"] == ALPHA  # a antiga dona
        assert incremental.chunk_ids("spec-217") == {"spec-217__spec__chunk_0"}  # sem chunks da antiga dona
        assert incremental.chunks["spec-217__spec__chunk_0"]["source_file"] == "217-aaa-nova/spec.md"
        assert _snapshot(incremental) == _snapshot(full)

    @pytest.mark.asyncio
    async def test_affects_acompanham_a_troca_de_dona(self, tmp_path):
        root = _make_217(tmp_path)  # ALPHA declara repos: [acme-backend]
        store = InMemorySpecStore()
        store.seed_service("service-backend", "acme-backend")
        await _run(store, root)
        assert ("spec-217", "service-backend") in store.affects
        shutil.rmtree(root / ALPHA)

        await _run(store, root, request=SpecIngestRequest(spec_paths=[f"{ALPHA}/spec.md"]))

        assert store.affects == {}  # as novas donas não declaram repos: nenhuma aresta velha sobra


# ─── frontmatter.id próprio ───────────────────────────────────────────────────


class TestExplicitFrontmatterIds:
    @pytest.mark.asyncio
    async def test_id_proprio_vence_e_nao_libera_o_spec_nnn(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "218-a/spec.md", "---\nid: spec-custom\n---\n# A\n\nTem id próprio.")
        _write(root, "218-b/spec.md", "# B\n\nSem id.")
        store = InMemorySpecStore()

        result = await _run(store, root)

        assert store.nodes["spec-custom"]["source_folder"] == "218-a"
        assert store.nodes["spec-218-b"]["source_folder"] == "218-b"  # a posição é decidida só pelos nomes
        assert "spec-218" not in store.nodes
        assert result.details["duplicate_folders"] == [{"folder": "218-b", "id": "spec-218-b"}]

    @pytest.mark.asyncio
    async def test_id_que_so_repete_o_padrao_nao_causa_colisao(self, tmp_path):
        """Pastas que declaram `id: spec-219` no frontmatter (o padrão) viram spec-219, spec-219-b, spec-219-c."""
        root = tmp_path / "specs"
        for name in ("219-a", "219-b", "219-c"):
            _write(root, f"{name}/spec.md", f"---\nid: spec-219\ntitle: T {name}\n---\n# T\n\nTexto.")
        store = InMemorySpecStore()

        result = await _run(store, root)

        assert set(store.nodes) == {"spec-219", "spec-219-b", "spec-219-c"}
        assert store.nodes["spec-219"]["title"] == "T 219-a"
        assert "id_collisions" not in result.details

    @pytest.mark.asyncio
    async def test_colisao_de_ids_proprios_descarta_a_segunda_e_reporta(self, tmp_path):
        root = tmp_path / "specs"
        _write(root, "220-a/spec.md", "---\nid: spec-compartilhado\n---\n# A\n\nPrimeira.")
        _write(root, "220-b/spec.md", "---\nid: spec-compartilhado\n---\n# B\n\nSegunda.")
        store = InMemorySpecStore()

        result = await _run(store, root)

        assert store.nodes["spec-compartilhado"]["source_folder"] == "220-a"
        assert result.details["id_collisions"] == [
            {"folder": "220-b", "id": "spec-compartilhado", "kept_folder": "220-a"}
        ]
        assert result.details["folders_parsed"] == 1
        assert "duplicate_folders" not in result.details
        assert store.chunk_ids("spec-compartilhado") == {"spec-compartilhado__spec__chunk_0"}
        # nada foi gravado para a descartada
        assert not any(c.get("source_file", "").startswith("220-b/") for c in store.chunks.values())
