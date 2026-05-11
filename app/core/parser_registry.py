"""
parser_registry.py — Mapeamento de chave parser → instância de DimensionParser.

Para registrar um novo parser:
  1. Crie a classe em app/core/parsers/{nome}_parser.py implementando DimensionParser
  2. Adicione uma entrada aqui: "chave_do_yaml": MinhaClasse()
  3. Referencie a chave no dimension YAML (campo `parser:`)
"""
from __future__ import annotations

from app.core.parsers.agents_md_parser import AgentsMdParser
from app.core.parsers.base import DimensionParser
from app.core.parsers.spec_parser import SpecParser

# Registro global: chave_yaml → instância do parser
REGISTRY: dict[str, DimensionParser] = {
    "spec_md": SpecParser(),
    "agents_md": AgentsMdParser(),
}


def get_parser(parser_key: str) -> DimensionParser | None:
    """Retorna o parser registrado para a chave dada, ou None se não encontrado."""
    return REGISTRY.get(parser_key)
