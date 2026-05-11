from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # Neo4j
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: str = "cortex-secret"

    # API
    cortex_api_token: str = "dev-token"
    cortex_port: int = 8082
    cortex_host: str = "0.0.0.0"

    # Ingestor
    specs_dir: str = "/specs"
    # Token GitHub para fetch de AGENTS.md em repos privados
    # Se não configurado, usa fetch anônimo (só funciona em repos públicos)
    github_token: str = ""

    # App
    app_env: str = "development"
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
