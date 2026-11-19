"""Ambiente do Alembic: aplica as migrações de ``db/migrations/versions``.

A conexão é a do dono do banco: ``config.attributes["url"]`` (testes) ou o
``.env`` (``MigrationSettings``). Rode sempre com o mesmo usuário: os
``ALTER DEFAULT PRIVILEGES`` da 0002 só valem para tabelas criadas por ele.

Todas as migrações pendentes rodam numa transação só: se uma falhar, nada fica
aplicado.
"""

import asyncio
import re
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import pool
from sqlalchemy.engine import URL, Connection
from sqlalchemy.ext.asyncio import create_async_engine

from onibus.config import MigrationSettings

config = context.config

if config.config_file_name is not None and config.attributes.get(
    "configure_logger", True
):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Sem modelos ORM ainda: o autogenerate fica desligado. Quem criar os modelos
# (ADR-001, P1) aponta aqui o MetaData deles. Um MetaData vazio faria o
# autogenerate propor apagar todas as tabelas.
target_metadata = None

# Schemas do sistema. O autogenerate ignora o resto: public (PostGIS,
# alembic_version) e partman.
SCHEMAS = ("bruto", "referencia", "operacao", "indicadores")
# Partições criadas pelo pg_partman, que não existem no modelo.
PARTITION = re.compile(r"^posicoes_gps_(p\d{8}|default)$")


def include_name(name: str | None, type_: str, parent_names: object) -> bool:
    """Deixa o autogenerate só com os schemas do sistema, sem as partições."""
    if type_ == "schema":
        return name in SCHEMAS
    if type_ == "table":
        return name is None or not PARTITION.match(name)
    return True


def _configure(**kwargs: object) -> None:
    context.configure(
        target_metadata=target_metadata,
        include_schemas=True,
        include_name=include_name,
        # GeoAlchemy2: tipos e índices espaciais certos no autogenerate.
        include_object=alembic_helpers.include_object,
        process_revision_directives=alembic_helpers.writer,
        render_item=alembic_helpers.render_item,
        **kwargs,
    )


def run_migrations_offline() -> None:
    """Gera o SQL sem conectar (``alembic upgrade head --sql``)."""
    _configure(
        dialect_name="postgresql",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection: Connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async(url: URL) -> None:
    engine = create_async_engine(url, poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run_sync)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    """Conecta como o dono do banco e aplica as migrações."""
    url = config.attributes.get("url") or MigrationSettings().database_url()
    asyncio.run(_run_async(url))


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
