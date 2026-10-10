"""
tests/test_spec_issues.py — Specs vindas de issues do GitHub (app/core/spec_issues.py).

O GitHub é simulado com httpx.MockTransport: nenhuma chamada de rede de verdade.

Cobre:
  - config: repo vazio desliga o recurso; repo malformado é rejeitado
  - NNN do título, fallback no número da issue, título limpo
  - status por state / state_reason
  - paginação, PRs ignorados, header de autenticação
  - 401 / rede / payload estranho viram SpecIssuesError
  - busca por número (404 e perda do label)
"""
from __future__ import annotations

import httpx
import pytest

from app.core.spec_issues import (
    SpecIssue,
    SpecIssuesConfig,
    SpecIssuesError,
    fetch_issues_by_number,
    fetch_spec_issues,
    load_spec_issues_config,
    map_issue,
    parse_spec_title,
    status_from_issue,
)

CFG = SpecIssuesConfig(repo="acme/specs-repo", label="spec")


def _issue_json(number: int, title: str, *, state="open", state_reason=None, labels=("spec",), body="Corpo da issue.", **extra) -> dict:
    return {
        "number": number,
        "title": title,
        "body": body,
        "state": state,
        "state_reason": state_reason,
        "html_url": f"https://github.com/acme/specs-repo/issues/{number}",
        "updated_at": "2026-10-01T12:00:00Z",
        "labels": [{"name": n} for n in labels],
        **extra,
    }


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


# ─── config ───────────────────────────────────────────────────────────────────


class TestConfig:
    def test_repo_vazio_desliga_o_recurso(self):
        assert load_spec_issues_config({"spec_issues": {"repo": "", "label": "spec"}}) is None
        assert load_spec_issues_config({"spec_issues": {}}) is None
        assert load_spec_issues_config({}) is None

    def test_repo_configurado(self):
        cfg = load_spec_issues_config({"spec_issues": {"repo": " acme/specs-repo ", "label": "intent"}})
        assert cfg == SpecIssuesConfig(repo="acme/specs-repo", label="intent")

    def test_label_padrao_e_spec(self):
        assert load_spec_issues_config({"spec_issues": {"repo": "acme/x"}}).label == "spec"

    @pytest.mark.parametrize("repo", ["acme", "acme/x/y", "acme/../etc", "ht tp/x", "acme/x?y=1"])
    def test_repo_malformado_e_rejeitado(self, repo):
        with pytest.raises(SpecIssuesError):
            load_spec_issues_config({"spec_issues": {"repo": repo}})


# ─── título -> NNN ────────────────────────────────────────────────────────────


class TestParseSpecTitle:
    @pytest.mark.parametrize(
        "title,number,expected",
        [
            ("Spec 861 — Split the bill in chat", 900, ("861", "Split the bill in chat")),  # NNN do título, não do #issue
            ("Spec 861: Split the bill in chat", 900, ("861", "Split the bill in chat")),
            ("spec 861 - Split the bill in chat", 900, ("861", "Split the bill in chat")),
            ("Spec NNN — Split the bill in chat", 877, ("877", "Split the bill in chat")),  # placeholder -> nº da issue
            ("Spec — Split the bill in chat", 877, ("877", "Split the bill in chat")),
            ("Split the bill in chat", 877, ("877", "Split the bill in chat")),  # sem prefixo
            ("Spec 1000 — Grande", 1000, ("1000", "Grande")),
            ("Spec Novo — Algo", 877, ("877", "Spec Novo — Algo")),  # "Novo" não é placeholder
            ("Revisar Spec 861 — detalhes", 900, ("861", "Revisar Spec 861 — detalhes")),  # número fora do prefixo
            ("Spec 861 —", 900, ("861", "Spec 861 —")),  # nada sobra: mantém o título original
        ],
    )
    def test_nnn_e_titulo_limpo(self, title, number, expected):
        assert parse_spec_title(title, number) == expected

    def test_fallback_do_numero_da_issue_tem_3_digitos_como_as_pastas(self):
        assert parse_spec_title("Spec NNN — X", 42)[0] == "042"


class TestStatusFromIssue:
    @pytest.mark.parametrize(
        "state,reason,expected",
        [
            ("open", None, "planned"),
            ("open", "reopened", "planned"),
            ("closed", "completed", "done"),
            ("closed", None, "done"),  # issues antigas não têm state_reason
            ("closed", "not_planned", "deprecated"),
            ("closed", "duplicate", "deprecated"),
        ],
    )
    def test_status(self, state, reason, expected):
        assert status_from_issue(state, reason) == expected


# ─── issue -> nó ──────────────────────────────────────────────────────────────


class TestMapIssue:
    def test_mapeamento_completo(self):
        issue = SpecIssue.from_api(
            _issue_json(900, "Spec 861 — Split the bill in chat", state="closed", state_reason="completed", labels=("spec", "area:payments"))
        )
        m = map_issue(issue, CFG)

        assert m.node_id == "spec-861"
        assert m.issue_props == {
            "has_issue": True,
            "issue_number": 900,
            "issue_url": "https://github.com/acme/specs-repo/issues/900",
            "issue_state": "closed",
            "issue_state_reason": "completed",
            "issue_updated_at": "2026-10-01T12:00:00Z",
        }
        assert m.doc_props["title"] == "Split the bill in chat"
        assert m.doc_props["status"] == "done"
        assert m.doc_props["summary"] == "Corpo da issue."
        assert m.doc_props["labels"] == ["area:payments"]  # o próprio label de spec não entra
        assert m.doc_props["number"] == 861

    def test_issue_aberta_e_planned_e_nao_planejada_e_deprecated(self):
        opened = map_issue(SpecIssue.from_api(_issue_json(10, "Spec 010 — A")), CFG)
        dropped = map_issue(SpecIssue.from_api(_issue_json(11, "Spec 011 — B", state="closed", state_reason="not_planned")), CFG)
        assert opened.doc_props["status"] == "planned"
        assert dropped.doc_props["status"] == "deprecated"

    def test_summary_sao_os_primeiros_300_chars_do_corpo(self):
        body = "linha um\n\nlinha   dois " + "x" * 400
        m = map_issue(SpecIssue.from_api(_issue_json(861, "Spec 861 — A", body=body)), CFG)
        assert len(m.doc_props["summary"]) == 300
        assert m.doc_props["summary"].startswith("linha um linha dois xxx")

    def test_corpo_vazio(self):
        m = map_issue(SpecIssue.from_api(_issue_json(861, "Spec 861 — A", body=None)), CFG)
        assert m.doc_props["summary"] == ""
        assert m.chunks == []

    def test_chunks_do_corpo(self):
        body = "# Contexto\n\nTexto longo sobre a divisão. " * 40
        m = map_issue(SpecIssue.from_api(_issue_json(900, "Spec 861 — A", body=body)), CFG)

        assert len(m.chunks) > 1
        ids = [c.node_id for c in m.chunks]
        assert ids[0] == "spec-861__issue__chunk_0"
        assert ids[1] == "spec-861__issue__chunk_1"
        assert len(set(ids)) == len(ids)
        first = m.chunks[0]
        assert first.node_labels == ["DocumentChunk", "Intent"]
        assert first.properties["parent_id"] == "spec-861"
        assert first.properties["source_file"] == "issue#900"
        assert first.properties["chunk_source"] == "issue"
        assert "divisão" in first.properties["content"]

    def test_fallback_no_numero_da_issue_quando_titulo_sem_nnn(self):
        m = map_issue(SpecIssue.from_api(_issue_json(877, "Spec NNN — Convites")), CFG)
        assert m.node_id == "spec-877"
        assert m.doc_props["title"] == "Convites"
        assert m.chunks[0].node_id == "spec-877__issue__chunk_0"


# ─── GitHub REST ──────────────────────────────────────────────────────────────


class TestFetchSpecIssues:
    @pytest.mark.asyncio
    async def test_lista_com_label_state_all_e_ignora_prs(self):
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(
                200,
                json=[
                    _issue_json(30, "Spec 030 — C"),
                    _issue_json(10, "Spec 010 — A"),
                    _issue_json(20, "PR de spec", pull_request={"url": "x"}),
                ],
            )

        async with _client(handler) as client:
            issues = await fetch_spec_issues(CFG, "ghp_test_token", client=client)

        assert [i.number for i in issues] == [10, 30]  # ordenadas; PR fora
        req = seen[0]
        assert req.url.path == "/repos/acme/specs-repo/issues"
        assert req.url.params["labels"] == "spec"
        assert req.url.params["state"] == "all"
        assert req.url.params["per_page"] == "100"
        assert req.headers["Authorization"] == "Bearer ghp_test_token"

    @pytest.mark.asyncio
    async def test_sem_token_nao_envia_authorization(self):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=[])

        async with _client(handler) as client:
            await fetch_spec_issues(CFG, "", client=client)
        assert "Authorization" not in seen[0].headers

    @pytest.mark.asyncio
    async def test_pagina_ate_acabar(self):
        pages = {
            "1": [_issue_json(n, f"Spec {n:03d} — X") for n in range(100, 200)],
            "2": [_issue_json(n, f"Spec {n:03d} — X") for n in range(200, 250)],
        }

        def handler(request):
            return httpx.Response(200, json=pages[request.url.params["page"]])

        async with _client(handler) as client:
            issues = await fetch_spec_issues(CFG, "", client=client)
        assert len(issues) == 150

    @pytest.mark.asyncio
    async def test_401_vira_spec_issues_error_sem_vazar_token(self):
        def handler(request):
            return httpx.Response(401, json={"message": "Bad credentials"})

        async with _client(handler) as client:
            with pytest.raises(SpecIssuesError) as exc:
                await fetch_spec_issues(CFG, "ghp_segredo_123", client=client)
        assert "401" in str(exc.value)
        assert "ghp_segredo_123" not in str(exc.value)

    @pytest.mark.asyncio
    async def test_erro_de_rede_vira_spec_issues_error(self):
        def handler(request):
            raise httpx.ConnectError("sem rota")

        async with _client(handler) as client:
            with pytest.raises(SpecIssuesError):
                await fetch_spec_issues(CFG, "", client=client)

    @pytest.mark.asyncio
    async def test_payload_inesperado_vira_spec_issues_error(self):
        def handler(request):
            return httpx.Response(200, json={"message": "não é uma lista"})

        async with _client(handler) as client:
            with pytest.raises(SpecIssuesError):
                await fetch_spec_issues(CFG, "", client=client)

        def handler_sem_number(request):
            return httpx.Response(200, json=[{"title": "sem number"}])

        async with _client(handler_sem_number) as client:
            with pytest.raises(SpecIssuesError):
                await fetch_spec_issues(CFG, "", client=client)


class TestFetchIssuesByNumber:
    @pytest.mark.asyncio
    async def test_busca_cada_numero(self):
        paths: list[str] = []

        def handler(request):
            paths.append(request.url.path)
            n = int(request.url.path.rsplit("/", 1)[1])
            return httpx.Response(200, json=_issue_json(n, f"Spec {n:03d} — X"))

        async with _client(handler) as client:
            found, gone = await fetch_issues_by_number(CFG, [861, 862, 861], "", client=client)

        assert paths == ["/repos/acme/specs-repo/issues/861", "/repos/acme/specs-repo/issues/862"]  # sem repetir
        assert [i.number for i in found] == [861, 862]
        assert gone == []

    @pytest.mark.asyncio
    async def test_404_pr_e_perda_do_label_vao_para_gone(self):
        def handler(request):
            n = int(request.url.path.rsplit("/", 1)[1])
            if n == 1:
                return httpx.Response(404, json={"message": "Not Found"})
            if n == 2:
                return httpx.Response(200, json=_issue_json(2, "PR", pull_request={"url": "x"}))
            if n == 3:
                return httpx.Response(200, json=_issue_json(3, "Spec 003 — sem label", labels=("bug",)))
            return httpx.Response(200, json=_issue_json(4, "Spec 004 — Spec", labels=("SPEC",)))  # case-insensitive

        async with _client(handler) as client:
            found, gone = await fetch_issues_by_number(CFG, [1, 2, 3, 4], "", client=client)

        assert [i.number for i in found] == [4]
        assert gone == [1, 2, 3]

    @pytest.mark.asyncio
    async def test_401_levanta(self):
        def handler(request):
            return httpx.Response(401, json={"message": "Bad credentials"})

        async with _client(handler) as client:
            with pytest.raises(SpecIssuesError):
                await fetch_issues_by_number(CFG, [861], "", client=client)
