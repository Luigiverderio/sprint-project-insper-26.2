"""Conexão assíncrona dos serviços com o PostgreSQL, cada um com o seu login."""

from dataclasses import dataclass
from datetime import datetime

from pydantic import SecretStr
from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from onibus.config import Settings


@dataclass(frozen=True)
class CollectionHealth:
    """Últimos ciclos de coleta de posições da Olho Vivo."""

    last_collection_at: datetime | None
    last_successful_collection_at: datetime | None


def _service_url(
    settings: Settings, user: str, password: SecretStr | None
) -> URL | None:
    if password is None or not password.get_secret_value():
        return None
    return URL.create(
        "postgresql+asyncpg",
        username=user,
        password=password.get_secret_value(),
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    )


def create_read_engine(settings: Settings) -> AsyncEngine | None:
    """Cria o pool de leitura quando o login da API está configurado."""
    url = _service_url(
        settings, settings.api_database_user, settings.api_database_password
    )
    if url is None:
        return None
    return create_async_engine(
        url,
        connect_args={"timeout": 3, "command_timeout": 3},
        pool_pre_ping=True,
        pool_timeout=3,
    )


def create_collector_engine(settings: Settings) -> AsyncEngine | None:
    """Cria a conexão do coletor quando o login dele está configurado.

    Um ciclo grava de uma vez as posições de toda a frota (milhares de linhas),
    então cada comando tem mais folga que na API. Um banco travado ainda é
    cortado antes do próximo ciclo, 30 s depois.
    """
    url = _service_url(
        settings, settings.coletor_database_user, settings.coletor_database_password
    )
    if url is None:
        return None
    return create_async_engine(
        url,
        connect_args={"timeout": 3, "command_timeout": 5},
        pool_pre_ping=True,
        pool_size=1,
        max_overflow=0,
        pool_timeout=3,
    )


async def get_collection_health(engine: AsyncEngine) -> CollectionHealth:
    """Consulta a saúde da coleta de posições com as permissões da API."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT GREATEST(ultimo_ok, ultimo_erro), ultimo_ok
                FROM operacao.saude_coleta
                WHERE fonte = 'olhovivo_posicao'
                """
            )
        )
        row = result.one_or_none()
    if row is None:
        return CollectionHealth(None, None)
    return CollectionHealth(
        last_collection_at=row[0], last_successful_collection_at=row[1]
    )
