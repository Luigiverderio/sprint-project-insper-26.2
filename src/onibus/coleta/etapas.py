"""Duração de cada etapa de um ciclo de coleta, para saber onde o tempo vai.

Etapas medidas hoje:

- ``login``: ``POST /Login/Autenticar``;
- ``request``: ``GET /Posicao`` até o corpo inteiro chegar (o httpx baixa a
  resposta antes de devolver);
- ``parse``: leitura do JSON e validação no modelo;
- ``retry_wait``: espera entre as tentativas.

Cada etapa soma o tempo de todas as tentativas do ciclo. Uma etapa cortada no
meio (prazo do ciclo estourado) também conta, até o corte. A gravação no banco
e o registro do ciclo entram aqui quando o ciclo for gravado na tabela do T8.
"""

import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from enum import StrEnum


class Stage(StrEnum):
    """Etapa de um ciclo de coleta."""

    LOGIN = "login"
    REQUEST = "request"
    PARSE = "parse"
    RETRY_WAIT = "retry_wait"


class StageTimer:
    """Soma, em segundos, quanto tempo o ciclo passou em cada etapa."""

    def __init__(self, clock: Callable[[], float] = time.perf_counter) -> None:
        self._clock = clock
        self._durations: dict[Stage, float] = {}

    @contextmanager
    def measure(self, stage: Stage) -> Iterator[None]:
        """Mede o bloco e soma o tempo à etapa, mesmo se o bloco falhar."""
        start = self._clock()
        try:
            yield
        finally:
            elapsed = self._clock() - start
            self._durations[stage] = self._durations.get(stage, 0.0) + elapsed

    def durations(self) -> dict[Stage, float]:
        """Cópia das durações medidas até agora, só das etapas que ocorreram."""
        return dict(self._durations)


def format_durations(durations: dict[Stage, float]) -> str:
    """Texto curto para log, por exemplo ``login=0.21s request=1.80s``."""
    if not durations:
        return "no stages"
    return " ".join(f"{stage}={seconds:.2f}s" for stage, seconds in durations.items())
