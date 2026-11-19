from argparse import Namespace
from collections.abc import Callable
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy.engine import URL
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.docker_client import DockerClient
from testcontainers.core.image import DockerImage

from onibus.config import MigrationSettings, Settings

REPO = Path(__file__).resolve().parents[1]
# Mês da primeira partição nos bancos de teste (o mesmo do teste.sql do modelo).
INICIO_PARTICOES = "2026-08-01"


@pytest.fixture(autouse=True)
def isolate_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Impede que os testes carreguem configurações locais ou segredos."""
    monkeypatch.setitem(Settings.model_config, "env_file", None)
    monkeypatch.setitem(MigrationSettings.model_config, "env_file", None)
    for name in (
        "APP_NAME",
        "APP_ENVIRONMENT",
        "APP_LOG_LEVEL",
        "APP_OLHO_VIVO_TOKEN",
        "POSTGRES_HOST",
        "POSTGRES_DB",
        "POSTGRES_USER",
        "POSTGRES_PASSWORD",
        "DB_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(scope="session")
def db_image() -> str:
    """Imagem do banco do ``make up`` (``db/Dockerfile``), ainda sem esquema.

    Sem Docker rodando, os testes que usam o banco são pulados.
    """
    try:
        DockerClient().client.ping()
    except Exception:
        pytest.skip("Docker indisponível: os testes do banco precisam dele rodando")
    image = DockerImage(path=str(REPO / "db"), tag="onibus-db:test", clean_up=False)
    return str(image.build())


@pytest.fixture(scope="session")
def alembic_config() -> Callable[[PostgresContainer, str], Config]:
    """Configuração do Alembic do repositório apontada para um banco de teste.

    Usa o ``alembic.ini`` e as migrações de verdade, como o ``make migrate``,
    mas conecta no container (e não no ``.env``) e não mexe no logging.
    """

    def make(container: PostgresContainer, database: str) -> Config:
        config = Config(
            str(REPO / "alembic.ini"),
            cmd_opts=Namespace(x=[f"inicio_particoes={INICIO_PARTICOES}"]),
        )
        config.attributes["url"] = URL.create(
            "postgresql+asyncpg",
            username=container.username,
            password=container.password,
            host=container.get_container_host_ip(),
            port=int(container.get_exposed_port(5432)),
            database=database,
        )
        config.attributes["configure_logger"] = False
        return config

    return make
