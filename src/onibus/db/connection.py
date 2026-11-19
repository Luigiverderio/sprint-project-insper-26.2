"""Conexão assíncrona da API com o PostgreSQL."""

from datetime import datetime
from typing import cast

from sqlalchemy import URL, text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from onibus.config import Settings


def create_read_engine(settings: Settings) -> AsyncEngine | None:
    """Cria o pool de leitura quando o login da API está configurado."""
    password = settings.database_password
    if password is None or not password.get_secret_value():
        return None

    url = URL.create(
        "postgresql+asyncpg",
        username=settings.database_user,
        password=password.get_secret_value(),
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
    )
    return create_async_engine(
        url,
        connect_args={"timeout": 3, "command_timeout": 3},
        pool_pre_ping=True,
        pool_timeout=3,
    )


async def get_last_collection_at(engine: AsyncEngine) -> datetime | None:
    """Consulta a saúde da coleta de posições com as permissões da API."""
    async with engine.connect() as connection:
        result = await connection.execute(
            text(
                """
                SELECT GREATEST(ultimo_ok, ultimo_erro)
                FROM operacao.saude_coleta
                WHERE fonte = 'olhovivo_posicao'
                """
            )
        )
        return cast("datetime | None", result.scalar_one_or_none())
