"""Roda a coleta da Olho Vivo: ``python -m onibus.coleta``.

Sem argumentos, roda um ciclo só, para conferir o token e a conexão, e não grava
nada no banco. Com ``--schedule``, roda um ciclo a cada 30 s até receber Ctrl+C
ou SIGTERM e grava cada ciclo no bruto (:mod:`onibus.coleta.gravacao`). O token
vem de ``APP_OLHO_VIVO_TOKEN``, e o login do banco, de
``APP_COLETOR_DATABASE_USER`` e ``APP_COLETOR_DATABASE_PASSWORD`` (no ``.env``).
"""

import argparse
import asyncio
import logging
import signal
import sys

from onibus.coleta.coletor import CycleStatus, PositionCollector
from onibus.coleta.etapas import format_durations
from onibus.coleta.gravacao import CycleRecorder
from onibus.coleta.olho_vivo import OlhoVivoClient
from onibus.coleta.scheduler import CollectionScheduler
from onibus.config import Settings
from onibus.db.connection import create_collector_engine

MISSING_TOKEN = "APP_OLHO_VIVO_TOKEN não configurado no .env"
MISSING_DATABASE_LOGIN = (
    "APP_COLETOR_DATABASE_PASSWORD não configurado no .env:"
    " a coleta contínua grava cada ciclo no banco"
)


async def collect_once(settings: Settings) -> int:
    """Roda um ciclo e devolve o código de saída do processo."""
    if settings.olho_vivo_token is None:
        print(MISSING_TOKEN, file=sys.stderr)
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
    print(f"Etapas: {format_durations(result.stage_durations)}")
    return 0


async def collect_forever(settings: Settings, stop: asyncio.Event) -> int:
    """Coleta a cada 30 s até ``stop``, gravando cada ciclo no banco.

    Devolve o código de saída do processo.
    """
    if settings.olho_vivo_token is None:
        print(MISSING_TOKEN, file=sys.stderr)
        return 2
    engine = create_collector_engine(settings)
    if engine is None:
        print(MISSING_DATABASE_LOGIN, file=sys.stderr)
        return 2
    recorder = CycleRecorder(engine)
    try:
        async with OlhoVivoClient(
            settings.olho_vivo_token.get_secret_value()
        ) as client:
            scheduler = CollectionScheduler(
                PositionCollector(client), on_cycle=recorder.record
            )
            await scheduler.run(stop)
    finally:
        await engine.dispose()
    return 0


async def _serve(settings: Settings) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows não aceita handlers de sinal no loop; Ctrl+C ainda encerra.
            pass
    return await collect_forever(settings, stop)


def main() -> None:
    """Configura o logging e roda um ciclo, ou a coleta contínua com --schedule."""
    parser = argparse.ArgumentParser(prog="python -m onibus.coleta")
    parser.add_argument(
        "--schedule",
        action="store_true",
        help="roda um ciclo a cada 30 s até Ctrl+C, em vez de um ciclo só",
    )
    args = parser.parse_args()

    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    if args.schedule:
        try:
            sys.exit(asyncio.run(_serve(settings)))
        except KeyboardInterrupt:
            sys.exit(0)
    sys.exit(asyncio.run(collect_once(settings)))


if __name__ == "__main__":
    main()
