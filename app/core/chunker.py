"""
core/chunker.py — Divisão de texto longo em chunks para Vector RAG.

Estratégia:
  - Divide o corpo de documentos Markdown em chunks de tamanho configurável.
  - Usa sobreposição (overlap) para garantir contexto entre chunks adjacentes.
  - Preserva a quebra em parágrafos/seções sempre que possível.
  - Produz chunks prontos para embeddings e indexação vetorial no Neo4j.

Uso:
    from app.core.chunker import chunk_markdown

    chunks = chunk_markdown(content, chunk_size=500, overlap=100)
    # chunks: list[str] — cada item é um trecho de texto para embedding
"""
from __future__ import annotations

import re


_HEADING_RE = re.compile(r"^#{1,6}\s", re.MULTILINE)


def _split_by_sections(text: str) -> list[str]:
    """
    Divide o texto em seções baseadas em headings Markdown (##, ###, etc).
    Fallback para parágrafos se não houver headings.
    """
    positions = [m.start() for m in _HEADING_RE.finditer(text)]

    if len(positions) < 2:
        # Sem seções claras — dividir por parágrafos duplos
        paragraphs = re.split(r"\n\n+", text)
        return [p.strip() for p in paragraphs if p.strip()]

    sections: list[str] = []
    for i, start in enumerate(positions):
        end = positions[i + 1] if i + 1 < len(positions) else len(text)
        sections.append(text[start:end].strip())

    return [s for s in sections if s]


def chunk_markdown(
    content: str,
    chunk_size: int = 600,
    overlap: int = 100,
) -> list[str]:
    """
    Divide um documento Markdown em chunks textuais para embedding.

    Args:
        content:    Texto completo do documento (sem frontmatter).
        chunk_size: Número máximo de caracteres por chunk.
        overlap:    Sobreposição entre chunks consecutivos (em chars).

    Returns:
        Lista de strings — cada item é um chunk pronto para embedding.
    """
    if not content or not content.strip():
        return []

    # Primeira passada: dividir por seções/parágrafos
    sections = _split_by_sections(content)

    chunks: list[str] = []
    current = ""

    for section in sections:
        # Se a seção sozinha já é maior que chunk_size, quebrar por frase
        if len(section) > chunk_size:
            sentences = re.split(r"(?<=[.!?])\s+", section)
            for sent in sentences:
                if len(current) + len(sent) <= chunk_size:
                    current += (" " if current else "") + sent
                else:
                    if current:
                        chunks.append(current.strip())
                    # Overlap: re-usa o final do chunk anterior
                    if overlap > 0 and chunks:
                        tail = chunks[-1][-overlap:]
                        current = tail + " " + sent
                    else:
                        current = sent
        else:
            if len(current) + len(section) <= chunk_size:
                current += ("\n\n" if current else "") + section
            else:
                if current:
                    chunks.append(current.strip())
                if overlap > 0 and chunks:
                    tail = chunks[-1][-overlap:]
                    current = tail + "\n\n" + section
                else:
                    current = section

    if current.strip():
        chunks.append(current.strip())

    return chunks
