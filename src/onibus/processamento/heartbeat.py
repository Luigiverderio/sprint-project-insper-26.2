"""Batimento do worker, lido pelo healthcheck do ``docker-compose.yml``.

O worker não tem porta HTTP. Ele atualiza um arquivo a cada
``HEARTBEAT_INTERVAL_S`` segundos, e o healthcheck
(``python -m onibus.processamento.heartbeat``) só passa se o arquivo for recente.
Assim um laço travado também fica ``unhealthy``, e não só um processo morto.
"""

import asyncio
import contextlib
import sys
import tempfile
import time
from pathlib import Path

HEARTBEAT_FILE = Path(tempfile.gettempdir()) / "onibus-worker.heartbeat"
HEARTBEAT_INTERVAL_S = 10.0
# Três batimentos perdidos seguidos: o worker está parado.
HEARTBEAT_MAX_AGE_S = 3 * HEARTBEAT_INTERVAL_S


async def beat(path: Path, stop: asyncio.Event, interval_s: float) -> None:
    """Atualiza ``path`` a cada ``interval_s`` segundos até ``stop``.

    Ao parar, apaga o arquivo: um worker encerrado não parece vivo.
    """
    try:
        while not stop.is_set():
            path.touch()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(stop.wait(), timeout=interval_s)
    finally:
        path.unlink(missing_ok=True)


def is_fresh(path: Path, max_age_s: float) -> bool:
    """Diz se ``path`` existe e foi atualizado há no máximo ``max_age_s`` segundos."""
    try:
        age_s = time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return False
    return age_s <= max_age_s


def main(path: Path = HEARTBEAT_FILE) -> int:
    """Código de saída do healthcheck: 0 se o worker bateu há pouco, 1 se não."""
    return 0 if is_fresh(path, HEARTBEAT_MAX_AGE_S) else 1


if __name__ == "__main__":
    sys.exit(main())
