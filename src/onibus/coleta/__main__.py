"""Roda um ciclo de coleta real: ``python -m onibus.coleta``.

Serve para conferir o token e a conexão com a Olho Vivo. Lê o token de
``APP_OLHO_VIVO_TOKEN`` (no ``.env``) e não grava nada no banco.
"""

import asyncio
import logging
import sys

from onibus.coleta.coletor import CycleStatus, PositionCollector
from onibus.coleta.olho_vivo import OlhoVivoClient
from onibus.config import Settings


async def collect_once(settings: Settings) -> int:
    """Roda um ciclo e devolve o código de saída do processo."""
    if settings.olho_vivo_token is None:
        print("APP_OLHO_VIVO_TOKEN não configurado no .env", file=sys.stderr)
        return 2
    async with OlhoVivoClient(settings.olho_vivo_token.get_secret_value()) as client:
        result = await PositionCollector(client).run_cycle()

    if result.status is CycleStatus.FAILURE or result.snapshot is None:
        print(f"Falhou: {result.failure_cause}", file=sys.stderr)
        return 1
    print(
        f"OK em {result.attempts} tentativa(s): {len(result.snapshot.lines)} linhas, "
        f"{result.record_count} veículos (hr={result.snapshot.reference_time})"
    )
    return 0


def main() -> None:
    """Configura o logging e roda um ciclo."""
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    sys.exit(asyncio.run(collect_once(settings)))


if __name__ == "__main__":
    main()
