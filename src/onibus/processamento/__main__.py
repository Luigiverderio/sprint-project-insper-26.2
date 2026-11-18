"""Ponto de entrada do worker: ``python -m onibus.processamento``."""

import asyncio
import logging
import signal

from onibus.config import Settings

logger = logging.getLogger(__name__)


async def run(stop: asyncio.Event, settings: Settings) -> None:
    """Mantém o worker ativo até ``stop`` ser sinalizado.

    As rotinas de processamento (map-matching, viagens e indicadores) entram
    nos cards seguintes, dentro deste laço.
    """
    logger.info("Worker started environment=%s", settings.environment)
    await stop.wait()
    logger.info("Worker stopped")


async def _serve(settings: Settings) -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            # Windows não aceita handlers de sinal no loop; Ctrl+C ainda encerra.
            pass
    await run(stop, settings)


def main() -> None:
    """Configura o logging e roda o worker até receber SIGINT ou SIGTERM."""
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_serve(settings))


if __name__ == "__main__":
    main()
