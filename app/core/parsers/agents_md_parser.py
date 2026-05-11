"""
parsers/agents_md_parser.py — Parser de dimension :Service.

Extrai dados de AGENTS.md / agents.md de cada repositório para criar nós :Service.

Informações extraídas:
  - name: Título H1 (ex: "Backend Rolê Organizado Agent")
  - tech: Tabela "Stack" ou "Stack Completo" — lista de tecnologias
  - capabilities: Seção "## Capabilities" ou "## 🎨 Capabilities" — bullet points
  - port: Primeiro número de porta mencionado (ex: ":8080", "Port 4300")
  - url: Domínio rolds.dev mencionado no texto
  - version: Linha "Version: X.Y.Z" no cabeçalho
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.parsers.base import DimensionParser, NodeData, ParseResult

# Mapeamento repo-name → service-id (sem sufixo -role-organizado)
# Para repos que NÃO seguem o padrão {name}-role-organizado, defina aqui
_REPO_OVERRIDE_IDS: dict[str, str] = {}

# Regex para extrair porta (ex: ":8080", "Port 8080", "port 4300")
_PORT_RE = re.compile(r"(?:port|porta|:\s*)(\d{4,5})\b", re.IGNORECASE)

# Regex para extrair domínio rolds.dev
_URL_RE = re.compile(r"https?://[a-z0-9.\-]+\.rolds\.dev(?:/[^\s)\"']*)?", re.IGNORECASE)

# Regex para extrair versão do cabeçalho
_VERSION_RE = re.compile(r"\*\*Version\*\*[:\s]+([0-9]+\.[0-9]+\.[0-9]+)", re.IGNORECASE)


def _derive_service_id(file_path: Path) -> tuple[str, str]:
    """
    Deriva service_id e repo_name a partir do caminho do arquivo.

    Exemplos:
      /repos/backend-role-organizado/AGENTS.md → ("service-backend", "backend-role-organizado")
      /repos/bff-role-organizado/agents.md     → ("service-bff", "bff-role-organizado")
    """
    # O repo é o diretório pai imediato se estiver em /repos/{repo}/...
    # Senão, sobe até encontrar um padrão reconhecível
    parts = file_path.parts
    repo_name = ""
    for i, part in enumerate(parts):
        if part in ("repos", "Development") and i + 1 < len(parts):
            repo_name = parts[i + 1]
            break

    if not repo_name:
        # Fallback: usa o nome do diretório pai
        repo_name = file_path.parent.name

    if repo_name in _REPO_OVERRIDE_IDS:
        return _REPO_OVERRIDE_IDS[repo_name], repo_name

    # Remove sufixo "-role-organizado" para construir o service_id
    short = repo_name.replace("-role-organizado", "")
    service_id = f"service-{short}"
    return service_id, repo_name


def _extract_name(content: str) -> str:
    """Extrai o título H1 do arquivo."""
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _extract_version(content: str) -> str:
    """Extrai a versão do cabeçalho YAML-like no topo do arquivo."""
    m = _VERSION_RE.search(content[:500])  # busca apenas no cabeçalho
    return m.group(1) if m else ""


def _extract_tech(content: str) -> list[str]:
    """
    Extrai tecnologias da tabela de Stack.

    Procura por seções como "## Stack", "## 🛠️ Stack Completo", "### Stack"
    e parseia a tabela Markdown extraindo a segunda coluna (Technology).
    """
    tech: list[str] = []
    in_stack_section = False
    header_passed = False

    stack_section_re = re.compile(r"^#{1,3}\s+(?:🛠️\s*)?Stack(?:\s+Completo)?", re.IGNORECASE)
    next_section_re = re.compile(r"^#{1,3}\s+")

    for line in content.splitlines():
        stripped = line.strip()

        if stack_section_re.match(stripped):
            in_stack_section = True
            header_passed = False
            continue

        if in_stack_section:
            # Linha de header da tabela (ex: "| Component | Technology | Version |")
            if stripped.startswith("|") and "---" not in stripped and not header_passed:
                header_passed = True
                continue
            # Separador da tabela
            if stripped.startswith("|") and "---" in stripped:
                continue
            # Linha de dados da tabela
            if stripped.startswith("|") and header_passed:
                cols = [c.strip() for c in stripped.split("|") if c.strip()]
                if len(cols) >= 2:
                    tech_value = cols[1].strip()
                    # Remove formatação Markdown (backticks, negrito, links)
                    tech_value = re.sub(r"[`*\[\]]", "", tech_value)
                    tech_value = re.sub(r"\(https?://[^)]+\)", "", tech_value)
                    tech_value = tech_value.strip()
                    if tech_value and tech_value.lower() not in ("technology", "tech", ""):
                        tech.append(tech_value)
                continue
            # Saiu da seção da tabela
            if next_section_re.match(stripped) or (stripped and not stripped.startswith("|")):
                break

    return tech[:15]  # limita a 15 techs


def _extract_capabilities(content: str) -> list[str]:
    """
    Extrai capabilities da seção ## Capabilities ou ## 🎨 Capabilities.

    Retorna os sub-headers (H3/H4) e primeiros bullets como lista de strings.
    """
    capabilities: list[str] = []
    in_caps_section = False

    caps_section_re = re.compile(r"^#{1,3}\s+.*Capabilit", re.IGNORECASE)
    next_h2_re = re.compile(r"^#{2}\s+")
    subheader_re = re.compile(r"^#{3,4}\s+(.*)")
    bullet_re = re.compile(r"^[-*]\s+\*\*(.*?)\*\*")  # bullets com negrito — título da feature

    for line in content.splitlines():
        stripped = line.strip()

        if caps_section_re.match(stripped):
            in_caps_section = True
            continue

        if in_caps_section:
            # Saiu da seção (próximo H2)
            if next_h2_re.match(stripped) and not caps_section_re.match(stripped):
                break
            # Sub-headers H3/H4 viram capabilities
            m = subheader_re.match(stripped)
            if m:
                cap = re.sub(r"[🏗️🔐📊🔧🚀📱✅❌🎨]", "", m.group(1)).strip()
                if cap:
                    capabilities.append(cap)
                continue
            # Bullets com **título** em negrito
            m = bullet_re.match(stripped)
            if m:
                cap = m.group(1).strip()
                if cap:
                    capabilities.append(cap)

    return capabilities[:20]  # limita a 20


def _extract_port(content: str) -> int | None:
    """Extrai o primeiro número de porta mencionado no texto."""
    # Prioriza portas em contexto de "port" ou ":"
    for m in _PORT_RE.finditer(content):
        port = int(m.group(1))
        # Portas válidas de aplicação: 1024 – 65535
        if 1024 <= port <= 65535:
            return port
    return None


def _extract_url(content: str) -> str:
    """Extrai a primeira URL rolds.dev do conteúdo."""
    m = _URL_RE.search(content)
    return m.group(0) if m else ""


class AgentsMdParser(DimensionParser):
    """
    Parseia AGENTS.md / agents.md para criar nós :Service.
    """

    def can_parse(self, file_path: Path) -> bool:
        return file_path.name in ("AGENTS.md", "agents.md")

    def parse(self, file_path: Path) -> ParseResult:
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ParseResult()

        service_id, repo_name = _derive_service_id(file_path)

        name = _extract_name(content)
        if not name:
            # Fallback: usa o service_id humanizado
            name = service_id.replace("service-", "").replace("-", " ").title()

        tech = _extract_tech(content)
        capabilities = _extract_capabilities(content)
        port = _extract_port(content)
        url = _extract_url(content)
        version = _extract_version(content)
        capabilities_str = " ".join(capabilities)

        props: dict = {
            "id": service_id,
            "name": name,
            "repo": repo_name,
            "tech": tech,
            "capabilities": capabilities,
            "capabilities_str": capabilities_str,
        }
        if port is not None:
            props["port"] = port
        if url:
            props["url"] = url
        if version:
            props["version"] = version

        return ParseResult(
            nodes=[
                NodeData(
                    node_label="Service",
                    node_id=service_id,
                    properties=props,
                )
            ],
            edges=[],
        )
