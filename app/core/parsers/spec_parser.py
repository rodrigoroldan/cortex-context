"""
parsers/spec_parser.py — Parser de dimension :Spec.

Adaptador que encapsula a lógica legada de app/ingestor/spec_parser.py
implementando a interface DimensionParser.
"""
from __future__ import annotations

from pathlib import Path

from app.core.parsers.base import DimensionParser, EdgeData, NodeData, ParseResult
from app.ingestor.spec_parser import parse_spec_dir


class SpecParser(DimensionParser):
    """
    Parseia um diretório de spec (specs/NNN-slug/) usando o parser legado.

    Aceita tanto arquivos plan.md quanto spec.md e README.md dentro de um
    diretório com padrão NNN-slug.
    """

    def can_parse(self, file_path: Path) -> bool:
        """Aceita plan.md, spec.md ou README.md dentro de um diretório NNN-slug."""
        if file_path.name not in ("plan.md", "spec.md", "README.md"):
            return False
        parent = file_path.parent
        import re
        return bool(re.match(r"^\d{3}-", parent.name))

    def parse(self, file_path: Path) -> ParseResult:
        """
        Parseia o diretório pai da spec (não apenas o arquivo).

        O parser legado opera em nível de diretório — o arquivo é usado
        apenas para localizar o spec_dir correto.
        """
        spec_dir = file_path.parent
        node = parse_spec_dir(spec_dir)
        if node is None:
            return ParseResult()

        props = node.to_neo4j_props()

        return ParseResult(
            nodes=[
                NodeData(
                    node_label="Spec",
                    node_id=node.id,
                    properties=props,
                )
            ],
            edges=[],  # Arestas entre Specs são geradas por parse_edges() no pipeline legado
        )
