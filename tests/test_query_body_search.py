"""
tests/test_query_body_search.py — Busca no CORPO das specs em GET /api/v1/query (índice doc_chunk_fts).

Depois dos seeds de título/summary (spec_fts, service_fulltext, workflow_fulltext), a rota consulta SEMPRE o
fulltext dos chunks, mapeia cada chunk para o pai (parent_id) e acrescenta até max(2, limit // 3) pais que
ainda não são seed — além dos de título (o total pode passar de `limit`). Chunks que contêm a keyword
literal vêm antes dos que só casaram por token; dentro de cada grupo vale o score. Seeds vindos do corpo
ganham `match_snippet`, anexado também ao `summary` da RESPOSTA (o MCP publicado só exibe
id/title/summary/...); nada é gravado.

Ordem das queries à sessão: spec_fts, service_fulltext, workflow_fulltext, doc_chunk_fts,
[pais dos chunks], expansão.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.routes.query import _build_match_snippet, _snippet_terms, query_context


class _FakeNode(dict):
    def __init__(self, props: dict, labels: list[str]):
        super().__init__(props)
        self.labels = labels


def _session(results: list):
    """Sessão cujo .run() devolve cada item de `results` na ordem; Exception vira raise."""
    outcomes = []
    for item in results:
        if isinstance(item, Exception):
            outcomes.append(item)
            continue
        res = MagicMock()
        res.data = AsyncMock(return_value=item)
        outcomes.append(res)
    session = AsyncMock()
    session.run = AsyncMock(side_effect=outcomes)
    session.__aenter__ = AsyncMock(return_value=session)
    session.__aexit__ = AsyncMock(return_value=False)
    driver = MagicMock()
    driver.session = MagicMock(return_value=session)
    return driver, session


def _params(call) -> dict:
    """Parâmetros de um session.run(): 2º argumento posicional (seeds) ou kwargs (expansão)."""
    return call.args[1] if len(call.args) > 1 else call.kwargs


def _spec_record(spec_id: str, title: str, summary: str = "") -> dict:
    return {"props": {"id": spec_id, "title": title, "summary": summary, "pillar": "Intent"}, "labels": ["Spec", "Intent"], "score": 1.0}


def _chunk_hit(parent_id: str, content: str, source_file: str = "861-split/RUNBOOK.md", score: float = 2.0) -> dict:
    return {"parent_id": parent_id, "source_file": source_file, "content": content, "score": score}


def _expand(*nodes: _FakeNode) -> list[dict]:
    return [{"nodes": list(nodes), "edges": []}]


async def _query(driver, **kwargs):
    params = dict(keywords="webhook", limit=8, hops=1, pillar=None, dimension=None, domain_id="default", branch="main", _token="")
    params.update(kwargs)
    with patch("app.routes.query.get_driver", return_value=driver):
        return await query_context(**params)


RUNBOOK = "Para recuperar a divisão, reiniciar o worker de webhook e conferir a fila antes de reenviar."


class TestBodySeeds:
    @pytest.mark.asyncio
    async def test_hit_so_no_corpo_vira_seed_com_trecho_no_summary(self):
        props = {"id": "spec-861", "title": "Split the bill in chat", "summary": "Splits the bill", "pillar": "Intent"}
        parent = {"props": dict(props), "labels": ["Spec", "Intent"]}
        node = _FakeNode(props, ["Spec", "Intent"])
        driver, session = _session([[], [], [], [_chunk_hit("spec-861", RUNBOOK)], [parent], _expand(node)])

        response = await _query(driver, keywords="webhook")

        assert [n.id for n in response.nodes] == ["spec-861"]
        n = response.nodes[0]
        assert n.match_snippet.startswith("861-split/RUNBOOK.md: …")
        assert n.match_snippet.endswith("…")
        assert "worker de webhook" in n.match_snippet
        assert n.summary == f"Splits the bill\n> 🔎 {n.match_snippet}"
        assert response.query_meta["body_seed_count"] == 1
        assert response.query_meta["seed_count"] == 1

        chunk_cypher, chunk_params = session.run.call_args_list[3].args
        assert "db.index.fulltext.queryNodes('doc_chunk_fts', $query)" in chunk_cypher
        assert chunk_params["query"] == "webhook"
        assert chunk_params["domain_id"] == "default"

    @pytest.mark.asyncio
    async def test_resposta_nao_persiste_o_trecho(self):
        parent = {"props": {"id": "spec-861", "title": "T", "summary": "S", "pillar": "Intent"}, "labels": ["Spec"]}
        node = _FakeNode({"id": "spec-861", "title": "T", "summary": "S", "pillar": "Intent"}, ["Spec"])
        driver, session = _session([[], [], [], [_chunk_hit("spec-861", RUNBOOK)], [parent], _expand(node)])

        response = await _query(driver)

        assert response.nodes[0].properties["summary"] == "S"  # o trecho vai só no summary da resposta
        for call in session.run.call_args_list:
            cypher = str(call.args[0])
            assert not any(w in cypher for w in ("MERGE", "CREATE (", "SET ", "DELETE"))

    @pytest.mark.asyncio
    async def test_hits_de_titulo_primeiro_e_sem_duplicar(self):
        title_node = _FakeNode({"id": "spec-861", "title": "Split webhook", "summary": "Resumo", "pillar": "Intent"}, ["Spec", "Intent"])
        body_node = _FakeNode({"id": "spec-862", "title": "Outra", "summary": "", "pillar": "Intent"}, ["Spec", "Intent"])
        parent_862 = {"props": dict(body_node), "labels": ["Spec", "Intent"]}
        driver, session = _session(
            [
                [_spec_record("spec-861", "Split webhook", "Resumo")],  # spec_fts
                [], [],
                [_chunk_hit("spec-861", RUNBOOK, score=9.0), _chunk_hit("spec-862", RUNBOOK, "862-outra/plan.md", score=3.0)],
                [parent_862],  # só o 862: o 861 já é seed
                _expand(title_node, body_node),
            ]
        )

        response = await _query(driver)

        by_id = {n.id: n for n in response.nodes}
        assert by_id["spec-861"].match_snippet is None  # veio do título
        assert by_id["spec-861"].summary == "Resumo"
        assert by_id["spec-862"].match_snippet.startswith("862-outra/plan.md: …")
        assert by_id["spec-862"].summary.startswith("> 🔎 862-outra/plan.md")  # sem summary prévio: sem linha em branco
        assert session.run.call_args_list[4].args[1]["parent_ids"] == ["spec-862"]
        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-861", "spec-862"]  # título antes do corpo
        assert response.query_meta["body_seed_count"] == 1

    @pytest.mark.asyncio
    async def test_varios_chunks_do_mesmo_pai_valem_um_seed_com_o_melhor_trecho(self):
        node = _FakeNode({"id": "spec-861", "title": "T", "summary": "", "pillar": "Intent"}, ["Spec"])
        parent = {"props": dict(node), "labels": ["Spec"]}
        hits = [
            _chunk_hit("spec-861", "melhor trecho com webhook aqui", "861-x/RUNBOOK.md", 5.0),
            _chunk_hit("spec-861", "pior trecho webhook", "861-x/plan.md", 1.0),
        ]
        driver, session = _session([[], [], [], hits, [parent], _expand(node)])

        response = await _query(driver)

        assert len(response.nodes) == 1
        assert response.nodes[0].match_snippet.startswith("861-x/RUNBOOK.md: ")
        assert session.run.call_args_list[4].args[1]["parent_ids"] == ["spec-861"]

    @staticmethod
    def _body_parents(numbers):
        return [{"props": {"id": f"spec-{n}", "title": f"B{n}", "summary": "", "pillar": "Intent"}, "labels": ["Spec"]} for n in numbers]

    @pytest.mark.asyncio
    async def test_corpo_acrescenta_ate_dois_pais_alem_dos_seeds_de_titulo(self):
        """limit=2 já foi usado pelo hit de título; mesmo assim entram max(2, limit // 3) = 2 pais de corpo."""
        title_node = _FakeNode({"id": "spec-1", "title": "A", "summary": "", "pillar": "Intent"}, ["Spec"])
        hits = [_chunk_hit(f"spec-{n}", RUNBOOK, score=10 - n) for n in (2, 3, 4)]
        driver, session = _session(
            [[_spec_record("spec-1", "A")], [], [], hits, self._body_parents((2, 3, 4)), _expand(title_node)]
        )

        response = await _query(driver, limit=2)

        seeds = _params(session.run.call_args_list[5])["seed_ids"]
        assert seeds == ["spec-1", "spec-2", "spec-3"]  # 3 > limit; spec-4 fica de fora (cota de 2)
        assert response.query_meta["body_seed_count"] == 2

    @pytest.mark.asyncio
    @pytest.mark.parametrize("limit,quota", [(1, 2), (5, 2), (6, 2), (9, 3), (30, 10)])
    async def test_cota_de_corpo_e_max_2_limit_div_3(self, limit, quota):
        numbers = list(range(100, 100 + quota + 5))
        hits = [_chunk_hit(f"spec-{n}", RUNBOOK, score=100 - n) for n in numbers]
        node = _FakeNode({"id": "spec-100", "title": "B", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session([[], [], [], hits, self._body_parents(numbers), _expand(node)])

        response = await _query(driver, limit=limit)

        assert response.query_meta["body_seed_count"] == quota
        assert _params(session.run.call_args_list[5])["seed_ids"] == [f"spec-{n}" for n in numbers[:quota]]

    @pytest.mark.asyncio
    async def test_corpo_roda_mesmo_com_os_seeds_de_titulo_enchendo_o_limit(self):
        """Regressão: os 3 índices de título enchem `limit` sozinhos e o corpo nunca era consultado."""
        title_records = [_spec_record(f"spec-{n}", f"Título {n}") for n in range(1, 9)]  # 8 == limit
        title_nodes = [_FakeNode({"id": f"spec-{n}", "title": f"Título {n}", "summary": "", "pillar": "Intent"}, ["Spec"]) for n in range(1, 9)]
        runbook = _FakeNode({"id": "spec-50", "title": "Runbook", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session(
            [
                title_records, [], [],
                [_chunk_hit("spec-50", RUNBOOK, "850-ops/RUNBOOK.md")],
                self._body_parents((50,)),
                _expand(*title_nodes, runbook),
            ]
        )

        response = await _query(driver, limit=8)

        assert "doc_chunk_fts" in str(session.run.call_args_list[3].args[0])
        assert _params(session.run.call_args_list[5])["seed_ids"][-1] == "spec-50"
        assert len(response.nodes) == 9  # 8 de título + 1 de corpo: passa de `limit`
        assert next(n for n in response.nodes if n.id == "spec-50").match_snippet.startswith("850-ops/RUNBOOK.md: …")

    @pytest.mark.asyncio
    async def test_pais_que_ja_sao_seed_de_titulo_nao_gastam_a_cota(self):
        title_node = _FakeNode({"id": "spec-1", "title": "A", "summary": "", "pillar": "Intent"}, ["Spec"])
        hits = [_chunk_hit("spec-1", RUNBOOK, score=9), _chunk_hit("spec-2", RUNBOOK, score=8), _chunk_hit("spec-3", RUNBOOK, score=7)]
        driver, session = _session(
            [[_spec_record("spec-1", "A")], [], [], hits, self._body_parents((2, 3)), _expand(title_node)]
        )

        await _query(driver, limit=3)

        assert session.run.call_args_list[4].args[1]["parent_ids"] == ["spec-2", "spec-3"]
        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-1", "spec-2", "spec-3"]

    @pytest.mark.asyncio
    async def test_busca_candidatos_suficientes_para_o_reordenamento(self):
        driver, session = _session([[], [], [], []])

        await _query(driver, limit=8)
        assert session.run.call_args_list[3].args[1]["chunk_limit"] == 80  # max(limit * 10, 50)

        driver, session = _session([[], [], [], []])
        await _query(driver, limit=2)
        assert session.run.call_args_list[3].args[1]["chunk_limit"] == 50

    @pytest.mark.asyncio
    async def test_chunk_com_a_keyword_literal_vem_antes_do_de_score_maior(self):
        """
        O analyzer quebra "https://api-staging.example.dev/x" em "api" + "staging.example.dev": o chunk que só
        casa por "api" tem score maior, mas quem tem o texto literal "api-staging" tem que entrar primeiro.
        """
        no_literal = "A api interna autentica o usuário e a api pública expõe o catálogo. " * 3
        literal = "Intro da api interna. " + ("filler " * 60) + "Health check em https://api-staging.example.dev/x para validar o deploy."
        hits = [
            _chunk_hit("spec-A", no_literal, "800-auth/plan.md", score=9.0),
            _chunk_hit("spec-B", literal, "861-ops/RUNBOOK.md", score=1.0),
        ]
        parents = self._body_parents(("A", "B"))  # ids "spec-A" e "spec-B"
        nodes = [_FakeNode(dict(p["props"]), ["Spec"]) for p in parents]
        driver, session = _session([[], [], [], hits, parents, _expand(*nodes)])

        response = await _query(driver, keywords="api-staging")

        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-B", "spec-A"]  # literal primeiro
        by_id = {n.id: n for n in response.nodes}
        snippet = by_id["spec-B"].match_snippet
        assert snippet.startswith("861-ops/RUNBOOK.md: …")
        assert "https://api-staging.example.dev/x" in snippet  # ancorado no literal, não no "api" do começo
        assert "Intro da api interna" not in snippet

    @pytest.mark.asyncio
    async def test_literal_ganha_a_vaga_quando_a_cota_e_menor_que_os_candidatos(self):
        hits = [
            _chunk_hit("spec-1", "só api e staging soltos", score=9.0),
            _chunk_hit("spec-2", "mais api sem o resto", score=8.0),
            _chunk_hit("spec-3", "URL: https://API-Staging.example.dev/health", score=1.0),  # literal, sem diferenciar maiúsculas
        ]
        node = _FakeNode({"id": "spec-3", "title": "B3", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session([[], [], [], hits, self._body_parents((1, 2, 3)), _expand(node)])

        await _query(driver, keywords="api-staging", limit=8)  # cota = 2

        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-3", "spec-1"]

    @pytest.mark.asyncio
    async def test_sem_literal_em_nenhum_chunk_vale_so_o_score(self):
        hits = [_chunk_hit("spec-1", "api staging", score=9.0), _chunk_hit("spec-2", "api", score=8.0)]
        node = _FakeNode({"id": "spec-1", "title": "B1", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session([[], [], [], hits, self._body_parents((1, 2)), _expand(node)])

        await _query(driver, keywords="api-staging")

        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-1", "spec-2"]

    @pytest.mark.asyncio
    async def test_falha_no_indice_do_corpo_nao_derruba_a_busca_por_titulo(self):
        title_node = _FakeNode({"id": "spec-1", "title": "A", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session(
            [[_spec_record("spec-1", "A")], [], [], RuntimeError("There is no such fulltext schema index: doc_chunk_fts"), _expand(title_node)]
        )

        response = await _query(driver)

        assert [n.id for n in response.nodes] == ["spec-1"]
        assert response.nodes[0].match_snippet is None

    @pytest.mark.asyncio
    async def test_pai_inexistente_ou_de_outro_pilar_nao_vira_seed(self):
        keep = _FakeNode({"id": "spec-2", "title": "Intent", "summary": "", "pillar": "Intent"}, ["Spec", "Intent"])
        parents = [
            {"props": {"id": "adr-9", "title": "ADR", "summary": "", "pillar": "System"}, "labels": ["ADR", "System"]},
            {"props": dict(keep), "labels": ["Spec", "Intent"]},
        ]
        hits = [_chunk_hit("adr-9", RUNBOOK, score=9), _chunk_hit("ghost", RUNBOOK, score=8), _chunk_hit("spec-2", RUNBOOK, score=7)]
        driver, session = _session([[], [], [], hits, parents, _expand(keep)])

        response = await _query(driver, pillar="Intent")

        assert [n.id for n in response.nodes] == ["spec-2"]
        assert _params(session.run.call_args_list[5])["seed_ids"] == ["spec-2"]

    @pytest.mark.asyncio
    async def test_sem_nenhum_seed_devolve_vazio(self):
        driver, _ = _session([[], [], [], []])

        response = await _query(driver)

        assert response.nodes == [] and response.query_meta["seed_count"] == 0

    @pytest.mark.asyncio
    async def test_filtra_dominio_e_branch_na_busca_do_corpo_e_dos_pais(self):
        parent = {"props": {"id": "spec-1", "title": "T", "summary": "", "pillar": "Intent"}, "labels": ["Spec"]}
        node = _FakeNode({"id": "spec-1", "title": "T", "summary": "", "pillar": "Intent"}, ["Spec"])
        driver, session = _session([[], [], [], [_chunk_hit("spec-1", RUNBOOK)], [parent], _expand(node)])

        await _query(driver, domain_id="acme", branch="feat-x")

        for idx in (3, 4):
            cypher, params = session.run.call_args_list[idx].args
            assert params["domain_id"] == "acme" and params["branch"] == "feat-x"
            assert "domain_id = $domain_id" in cypher and "branch = $branch" in cypher


class TestMatchSnippet:
    def test_janela_em_volta_da_primeira_ocorrencia_com_prefixo_do_arquivo(self):
        text = "x" * 500 + " Webhook " + "y" * 500
        snippet = _build_match_snippet(text, "861-x/RUNBOOK.md", ["webhook"])

        assert snippet.startswith("861-x/RUNBOOK.md: …") and snippet.endswith("…")
        body = snippet[len("861-x/RUNBOOK.md: …") : -1]
        assert len(body) <= 240
        assert "Webhook" in body  # case-insensitive, mantém o texto original

    def test_ocorrencia_no_comeco_nao_corta_o_inicio(self):
        snippet = _build_match_snippet("Webhook de pagamento. " + "z" * 600, "a.md", ["webhook"])
        assert snippet.startswith("a.md: …Webhook de pagamento.")

    def test_usa_a_ocorrencia_mais_cedo_entre_as_keywords(self):
        text = "inicio " + "a" * 400 + " fila " + "b" * 50 + " webhook " + "c" * 400
        snippet = _build_match_snippet(text, "a.md", ["webhook", "fila"])
        assert "fila" in snippet

    def test_ocorrencia_literal_tem_prioridade_sobre_as_palavras_da_keyword(self):
        text = "Notas sobre a api. " + "x" * 300 + " veja https://api-staging.example.dev/x agora " + "y" * 300
        snippet = _build_match_snippet(text, "a.md", ["api-staging"])
        assert "https://api-staging.example.dev/x" in snippet
        assert "Notas sobre a api" not in snippet

    def test_literal_ignora_aspas_e_maiusculas(self):
        snippet = _build_match_snippet("z" * 400 + " URL API-Staging aqui " + "w" * 400, "a.md", ['"api-staging"'])
        assert "API-Staging" in snippet

    def test_sem_literal_cai_nas_palavras_da_keyword(self):
        snippet = _build_match_snippet("z" * 400 + " só a palavra staging aparece " + "w" * 400, "a.md", ["api-staging"])
        assert "staging" in snippet

    def test_palavras_de_keyword_com_varias_palavras(self):
        snippet = _build_match_snippet("o worker reinicia sozinho", "a.md", ["fila de worker"])
        assert "worker" in snippet

    def test_sem_ocorrencia_literal_usa_o_inicio_do_texto(self):
        snippet = _build_match_snippet("Texto sem as palavras buscadas. " * 20, "a.md", ["inexistente"])
        assert snippet.startswith("a.md: …Texto sem as palavras")
        assert len(snippet) <= len("a.md: ……") + 240

    def test_colapsa_quebras_de_linha_e_aceita_arquivo_vazio(self):
        snippet = _build_match_snippet("linha 1\n\n   linha 2\twebhook", "", ["webhook"])
        assert snippet == "…linha 1 linha 2 webhook…"

    def test_operadores_lucene_nao_atrapalham(self):
        terms = _snippet_terms(['"webhook" OR (fila)', "a:b"])
        assert "webhook" in terms and "fila" in terms and not any(c in "".join(terms) for c in '"():')
        snippet = _build_match_snippet("o webhook caiu", "a.md", ['"webhook"', "fila*"])
        assert "webhook" in snippet
