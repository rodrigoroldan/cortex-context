"""
plugins/loader.py — Auto-descoberta de parsers customizados.

Qualquer arquivo Python em `plugins/` que:
  1. Importa BaseCortexExtractor
  2. Define uma subclasse com extractor_key preenchido
  3. Implementa can_parse() e parse()

... será registrado automaticamente.

Exemplo de plugin customizado (plugins/my_plugin.py):

    from app.core.parsers.base import BaseCortexExtractor, NodeData, ParseResult

    class MyCustomParser(BaseCortexExtractor):
        extractor_key = "my.custom_parser"

        def can_parse(self, file_path):
            return file_path.suffix == ".json"

        def parse(self, file_path, dimension_config):
            ...
"""
from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

from app.core.parsers.base import BaseCortexExtractor

logger = logging.getLogger(__name__)


def discover_plugins(plugins_dir: Path) -> dict[str, type[BaseCortexExtractor]]:
    """
    Descobre e registra parsers customizados em plugins_dir.

    Args:
        plugins_dir: Diretório onde ficam os arquivos .py dos plugins.

    Returns:
        Dict mapeando extractor_key → classe do extractor.
    """
    found: dict[str, type[BaseCortexExtractor]] = {}

    if not plugins_dir.exists():
        logger.debug("Diretório de plugins não encontrado: %s (ignorado)", plugins_dir)
        return found

    for py_file in sorted(plugins_dir.glob("*.py")):
        if py_file.stem.startswith("_"):
            continue  # Ignorar __init__.py, __pycache__, etc.

        module_name = f"cortex_plugin_{py_file.stem}"
        try:
            spec = importlib.util.spec_from_file_location(module_name, py_file)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            sys.modules[module_name] = module
            spec.loader.exec_module(module)  # type: ignore[union-attr]
        except Exception as e:
            logger.warning("Erro ao carregar plugin %s: %s", py_file, e)
            continue

        # Varrer atributos do módulo em busca de subclasses de BaseCortexExtractor
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseCortexExtractor)
                and attr is not BaseCortexExtractor
                and attr.extractor_key
            ):
                found[attr.extractor_key] = attr
                logger.info("Plugin registrado: %s → %s", attr.extractor_key, py_file.name)

    return found
