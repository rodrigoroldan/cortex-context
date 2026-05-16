"""
builtin/agents_manifest.py — Parser para manifestos de serviço (AGENTS.md / agents.md / README.md).

Extrai metadados de serviço de documentos markdown estruturados:
  - Nome, stack tecnológico, capacidades, porta, URL
  - Conexões com outras dimensões via seções conhecidas

Compatível com qualquer formato de manifesto de serviço —
não depende de jargões do produto.

Uso no dimension YAML:
  parser: builtin.agents_manifest
  source_type: github_api | filesystem
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

from app.core.parsers.base import BaseCortexExtractor, NodeData, ParseResult

if TYPE_CHECKING:
    from app.core.dimension_loader import DimensionConfig

# Seções de stack tecnológico
_TECH_PATTERNS = [
    re.compile(r"\b(Java|Spring Boot|Python|FastAPI|Node\.?js|TypeScript|React|Next\.?js|Kotlin|Swift|Go|Rust|Ruby|PHP|\.NET|C#)\b", re.IGNORECASE),
    re.compile(r"\b(MongoDB|PostgreSQL|MySQL|Redis|Neo4j|DynamoDB|SQLite|Cassandra)\b", re.IGNORECASE),
    re.compile(r"\b(Docker|Kubernetes|Terraform|Ansible|AWS|GCP|Azure|Proxmox)\b", re.IGNORECASE),
]

# Padrão para extrair porta de serviço (ex: ":8080", "port 4300", "porta 3000")
_PORT_PATTERN = re.compile(r"[Pp]ort(?:a)?\s*:?\s*(\d{4,5})\b")

# Padrão para extrair URL (http/https)
_URL_PATTERN = re.compile(r"https?://[^\s\)\]>\"']+")


def _extract_tech_stack(content: str) -> list[str]:
    """Extrai tecnologias mencionadas no documento."""
    found: set[str] = set()
    for pattern in _TECH_PATTERNS:
        for match in pattern.finditer(content):
            found.add(match.group(0))
    return sorted(found)


def _extract_capabilities(content: str) -> str:
    """Extrai seção de capacidades/capabilities do manifesto."""
    # Busca por seções com keywords de capacidades
    cap_match = re.search(
        r"#{1,3}\s+(?:Capabilit(?:y|ies)|Capabilities|Capacidades|Features|Funcionalidades)\s*\n(.*?)(?=\n#{1,3}\s|\Z)",
        content,
        re.IGNORECASE | re.DOTALL,
    )
    if cap_match:
        raw = cap_match.group(1).strip()
        # Extrair apenas linhas com bullet points
        lines = [
            line.lstrip("- *•").strip()
            for line in raw.splitlines()
            if line.strip().startswith(("-", "*", "•")) and len(line.strip()) > 3
        ]
        return " | ".join(lines[:10])  # Max 10 capacidades
    return ""


def _extract_h1(content: str) -> str:
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# ") and not stripped.startswith("## "):
            return stripped[2:].strip()
    return ""


class AgentsManifestParser(BaseCortexExtractor):
    """
    Parser built-in para manifestos de serviço (AGENTS.md, agents.md, README.md).

    Produz nós (:System:Service) a partir de documentos markdown estruturados.
    Agnóstico ao produto — funciona com qualquer manifesto de serviço.
    """

    extractor_key = "builtin.agents_manifest"

    def can_parse(self, file_path: Path) -> bool:
        return file_path.suffix in (".md", ".markdown")

    def parse(self, file_path: Path, dimension_config: "DimensionConfig") -> ParseResult:
        try:
            content = file_path.read_text(encoding="utf-8")
        except Exception:
            return ParseResult()

        # ID do serviço: prefixo configurável no extra do dimension YAML
        # Fallback: nome do diretório pai
        service_name = file_path.parent.name
        service_id = dimension_config.extra.get("service_id") or f"service-{service_name}"

        name = _extract_h1(content) or service_name
        tech = _extract_tech_stack(content)
        capabilities = _extract_capabilities(content)

        # Porta
        port_match = _PORT_PATTERN.search(content)
        port = int(port_match.group(1)) if port_match else None

        # URL
        url_matches = _URL_PATTERN.findall(content)
        url = url_matches[0] if url_matches else ""

        properties: dict = {
            "id": service_id,
            "name": name,
            "repo": service_name,
            "tech": tech,
            "tech_str": " ".join(tech),
            "capabilities": capabilities,
            "capabilities_str": capabilities,
            "pillar": dimension_config.pillar,
            "file_path": str(file_path),
        }
        if port:
            properties["port"] = port
        if url:
            properties["url"] = url

        node = NodeData(
            node_labels=dimension_config.node_labels,  # ex: ["Service", "System"]
            node_id=service_id,
            properties=properties,
        )

        return ParseResult(nodes=[node], edges=[])
