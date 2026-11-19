"""Dados de teste do banco local: ``make seed``.

Aplica os ``.sql`` de ``db/seeds/`` em ordem de nome, com a conexão do
``make migrate`` (o dono do banco, pelo ``.env``). Cada arquivo roda numa
transação: se um falhar, nada dele fica aplicado e os seguintes não rodam.
Convenções dos arquivos: ``db/seeds/README.md``.
"""

import asyncio
import logging
import sys
from pathlib import Path

import asyncpg
from sqlalchemy.engine import URL

from onibus.config import MigrationSettings, Settings

logger = logging.getLogger(__name__)

# Relativo à raiz do repositório, de onde o make roda (como o alembic.ini).
SEEDS_DIR = Path("db/seeds")


def seed_files(directory: Path) -> list[Path]:
    """Os ``.sql`` de ``directory``, em ordem de nome (``0001_...``, ``0002_...``)."""
    return sorted(directory.glob("*.sql"))


async def apply_seeds(url: URL, files: list[Path]) -> None:
    """Aplica cada arquivo numa transação própria, na ordem dada."""
    conn = await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )
    try:
        for file in files:
            logger.info("Applying seed file=%s", file.name)
            async with conn.transaction():
                # Sem argumentos, o asyncpg usa o protocolo simples, que aceita
                # vários comandos num texto só (um prepared statement não aceita).
                await conn.execute(file.read_text(encoding="utf-8"))
    finally:
        await conn.close()


def main(directory: Path = SEEDS_DIR) -> int:
    """Aplica os seeds de ``directory``; devolve o código de saída do comando."""
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if Settings().environment == "production":
        logger.error("Seeds are for local databases only (APP_ENVIRONMENT=production)")
        return 1
    files = seed_files(directory)
    if not files:
        logger.info("No seed files in %s", directory)
        return 0
    try:
        asyncio.run(apply_seeds(MigrationSettings().database_url(), files))
    except asyncpg.PostgresError as error:
        logger.error("Seed failed, rolled back: %s", error)
        return 1
    logger.info("Applied %d seed file(s)", len(files))
    return 0


if __name__ == "__main__":
    sys.exit(main())
