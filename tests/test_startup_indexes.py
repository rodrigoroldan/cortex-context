"""
tests/test_startup_indexes.py — Cobertura para aplicação de constraints/índices
de dimension YAMLs no startup (issue #13).

Sem isso, dimensões ingeridas exclusivamente via POST /ingest/manifest (ex:
parsing de AST client-side, sem nunca chamar /ingest/code) nunca teriam suas
constraints/fulltext indexes criadas.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def mock_neo4j_driver():
    mock_result = MagicMock()
    mock_result.single = AsyncMock(return_value={"cnt": 1})

    mock_session = AsyncMock()
    mock_session.run = AsyncMock(return_value=mock_result)
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=False)

    driver = MagicMock()
    driver.session = MagicMock(return_value=mock_session)
    driver.verify_connectivity = AsyncMock()
    driver.close = AsyncMock()

    with patch("app.db.neo4j.AsyncGraphDatabase.driver", return_value=driver):
        yield driver, mock_session


def test_startup_applies_code_symbol_constraint(mock_neo4j_driver):
    """CodeSymbol precisa ter sua constraint de unicidade criada no startup,
    mesmo que /ingest/code nunca seja chamado (fluxo manifest-only)."""
    driver, mock_session = mock_neo4j_driver

    app = create_app()
    with TestClient(app):
        pass

    executed_cypher = " ".join(str(call.args[0]) for call in mock_session.run.call_args_list)
    assert "CREATE CONSTRAINT code_symbol_id" in executed_cypher
    assert "code_symbol_fulltext" in executed_cypher


def test_startup_applies_service_and_spec_constraints(mock_neo4j_driver):
    driver, mock_session = mock_neo4j_driver

    app = create_app()
    with TestClient(app):
        pass

    executed_cypher = " ".join(str(call.args[0]) for call in mock_session.run.call_args_list)
    assert "CREATE CONSTRAINT service_id" in executed_cypher
    assert "CREATE CONSTRAINT spec_id" in executed_cypher


def test_startup_applies_doc_chunk_fulltext_and_parent_indexes(mock_neo4j_driver):
    """A busca no corpo das specs (/query) depende do fulltext doc_chunk_fts, e a limpeza de chunks
    por pai do índice em DocumentChunk.parent_id — ambos precisam existir já no startup."""
    driver, mock_session = mock_neo4j_driver

    app = create_app()
    with TestClient(app):
        pass

    executed_cypher = " ".join(str(call.args[0]) for call in mock_session.run.call_args_list)
    assert "CREATE FULLTEXT INDEX doc_chunk_fts IF NOT EXISTS FOR (n:DocumentChunk) ON EACH [n.content]" in executed_cypher
    assert "CREATE INDEX documentchunk_parent_id_idx IF NOT EXISTS FOR (n:DocumentChunk) ON (n.parent_id)" in executed_cypher


def test_startup_cria_spec_fts_que_o_query_consulta(mock_neo4j_driver):
    """/query busca título/summary no índice `spec_fts`; o startup tem que criá-lo (IF NOT EXISTS)."""
    driver, mock_session = mock_neo4j_driver

    app = create_app()
    with TestClient(app):
        pass

    executed = [str(call.args[0]) for call in mock_session.run.call_args_list]
    assert "CREATE FULLTEXT INDEX spec_fts IF NOT EXISTS FOR (n:Spec) ON EACH [n.title, n.summary, n.labels_str]" in executed
    assert not any("spec_fulltext" in q for q in executed)
