"""
parser_registry.py — Registro dinâmico de parsers (built-in + plugins).

Built-in parsers (prefixo "builtin.*"):
  builtin.markdown_frontmatter → MarkdownFrontmatterParser
  builtin.agents_manifest      → AgentsManifestParser

Plugins customizados (qualquer chave):
  São auto-descobertos via plugins/loader.py a partir do diretório plugins/.
  Qualquer subclasse de BaseCortexExtractor com extractor_key preenchido é registrada.

Para referenciar no dimension YAML:
  parser: builtin.markdown_frontmatter   # built-in
  parser: my.custom_parser               # plugin
"""
from __future__ import annotations

import logging
from pathlib import Path

from app.core.parsers.base import BaseCortexExtractor

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Built-in parsers — importados diretamente
# ------------------------------------------------------------------
from app.core.parsers.builtin.markdown_frontmatter import MarkdownFrontmatterParser
from app.core.parsers.builtin.agents_manifest import AgentsManifestParser

_BUILTIN_REGISTRY: dict[str, type[BaseCortexExtractor]] = {
    MarkdownFrontmatterParser.extractor_key: MarkdownFrontmatterParser,
    AgentsManifestParser.extractor_key: AgentsManifestParser,
    # Aliases de compatibilidade com versões anteriores
    "spec_md": MarkdownFrontmatterParser,
    "agents_md": AgentsManifestParser,
}

# Instâncias singleton (lazy cache para evitar re-instanciar)
_INSTANCES: dict[str, BaseCortexExtractor] = {}


def get_parser(parser_key: str, plugins_dir: Path | None = None) -> BaseCortexExtractor | None:
    """
    Retorna o parser para a chave dada.

    Ordem de busca:
      1. Cache de instâncias (singleton)
      2. Built-in registry
      3. Plugins customizados em plugins_dir (se fornecido)

    Args:
        parser_key: Chave do parser (ex: "builtin.markdown_frontmatter", "my.plugin")
        plugins_dir: Diretório de plugins para auto-descoberta (opcional)

    Returns:
        Instância do parser, ou None se não encontrado.
    """
    # 1. Cache
    if parser_key in _INSTANCES:
        return _INSTANCES[parser_key]

    # 2. Built-in
    if parser_key in _BUILTIN_REGISTRY:
        instance = _BUILTIN_REGISTRY[parser_key]()
        _INSTANCES[parser_key] = instance
        return instance

    # 3. Plugins
    if plugins_dir:
        from app.plugins.loader import discover_plugins
        plugin_classes = discover_plugins(plugins_dir)
        if parser_key in plugin_classes:
            instance = plugin_classes[parser_key]()
            _INSTANCES[parser_key] = instance
            return instance

    logger.warning("Parser não encontrado para chave: '%s'", parser_key)
    return None


def list_parsers(plugins_dir: Path | None = None) -> dict[str, str]:
    """
    Lista todos os parsers disponíveis (built-in + plugins).

    Returns:
        Dict: {extractor_key: class_name}
    """
    parsers = {k: v.__name__ for k, v in _BUILTIN_REGISTRY.items()}
    if plugins_dir:
        from app.plugins.loader import discover_plugins
        plugin_classes = discover_plugins(plugins_dir)
        parsers.update({k: v.__name__ for k, v in plugin_classes.items()})
    return parsers


# Alias de compatibilidade com código legado que importava REGISTRY/get_parser
DimensionParser = BaseCortexExtractor
