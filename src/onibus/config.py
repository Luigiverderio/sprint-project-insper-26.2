"""Configuração da aplicação por variáveis de ambiente."""

from typing import Literal

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import URL


class Settings(BaseSettings):
    """Configuração compartilhada pela API, coleta e processamento."""

    model_config = SettingsConfigDict(
        env_prefix="APP_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    name: str = "Ônibus Backend"
    environment: Literal["development", "test", "production"] = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    olho_vivo_token: SecretStr | None = None


class MigrationSettings(BaseSettings):
    """Conexão das migrações (Alembic): o dono do banco, que cria tabelas e papéis.

    Lê as mesmas variáveis do ``docker-compose.yml`` (``POSTGRES_DB``,
    ``POSTGRES_PASSWORD`` e ``DB_PORT``), sem prefixo, para a senha não se
    repetir no ``.env``. O usuário é sempre ``postgres``, como no compose (o
    pg_partman_bgw precisa dele). Os serviços não usam esta conexão: cada um
    terá o próprio login (ADR-001, P7).
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    postgres_host: str = "127.0.0.1"
    db_port: int = 5432
    postgres_db: str = "onibus"
    postgres_password: SecretStr | None = None

    def database_url(self) -> URL:
        """URL do SQLAlchemy com o driver asyncpg; a senha vai escapada."""
        if self.postgres_password is None:
            raise ValueError("defina POSTGRES_PASSWORD no .env (a senha do banco)")
        return URL.create(
            "postgresql+asyncpg",
            username="postgres",
            password=self.postgres_password.get_secret_value(),
            host=self.postgres_host,
            port=self.db_port,
            database=self.postgres_db,
        )
