"""Agendamento do ciclo de coleta: um ciclo a cada 30 s.

O APScheduler dispara o ciclo num intervalo fixo, sem deixar dois ciclos
rodarem ao mesmo tempo. O coletor limita cada ciclo a 25 s, abaixo do intervalo;
se mesmo assim um ciclo demorar mais que o intervalo, o disparo seguinte é
pulado e fica registrado no log, em vez de acumular ciclos.

Cada ciclo concluído vai para ``on_cycle``, o ponto em que o card T8 grava o
resultado na tabela de ciclos de coleta.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Protocol

from apscheduler.events import (
    EVENT_JOB_MAX_INSTANCES,
    EVENT_JOB_MISSED,
    JobEvent,
)
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from onibus.coleta.coletor import CycleResult

logger = logging.getLogger(__name__)

CYCLE_INTERVAL = timedelta(seconds=30)
# Tempo que o ciclo em andamento tem para terminar quando a coleta é parada.
SHUTDOWN_GRACE = timedelta(seconds=10)
JOB_ID = "collect-positions"

CycleHandler = Callable[[CycleResult], Awaitable[None]]


class CycleRunner(Protocol):
    """Quem executa um ciclo: o :class:`PositionCollector`, ou um simulado."""

    async def run_cycle(self) -> CycleResult: ...


class CollectionScheduler:
    """Roda ciclos de coleta num intervalo fixo até receber o pedido de parada."""

    def __init__(
        self,
        collector: CycleRunner,
        *,
        interval: timedelta = CYCLE_INTERVAL,
        on_cycle: CycleHandler | None = None,
        shutdown_grace: timedelta = SHUTDOWN_GRACE,
    ) -> None:
        self._collector = collector
        self._interval = interval
        self._on_cycle = on_cycle
        self._shutdown_grace = shutdown_grace
        self._idle = asyncio.Event()
        self._idle.set()

    def build_scheduler(self) -> AsyncIOScheduler:
        """Monta o agendador com o job de coleta, sem iniciá-lo."""
        scheduler = AsyncIOScheduler(timezone=UTC)
        scheduler.add_job(
            self._run_once,
            IntervalTrigger(seconds=self._interval.total_seconds()),
            id=JOB_ID,
            max_instances=1,
            coalesce=True,
            next_run_time=datetime.now(UTC),
        )
        scheduler.add_listener(
            self._log_skipped_tick, EVENT_JOB_MAX_INSTANCES | EVENT_JOB_MISSED
        )
        return scheduler

    async def run(self, stop: asyncio.Event) -> None:
        """Coleta a cada intervalo até ``stop``; deixa o ciclo atual terminar."""
        scheduler = self.build_scheduler()
        scheduler.start()
        logger.info("Collection scheduled every %g s", self._interval.total_seconds())
        try:
            await stop.wait()
        finally:
            scheduler.pause()
            await self._wait_for_running_cycle()
            scheduler.shutdown(wait=False)
            logger.info("Collection stopped")

    async def _run_once(self) -> None:
        self._idle.clear()
        try:
            result = await self._collector.run_cycle()
            logger.info(
                "Collection cycle %s: %d attempt(s), %d record(s)",
                result.status.value,
                result.attempts,
                result.record_count,
            )
            if self._on_cycle is not None:
                await self._on_cycle(result)
        finally:
            self._idle.set()

    async def _wait_for_running_cycle(self) -> None:
        if self._idle.is_set():
            return
        grace = self._shutdown_grace.total_seconds()
        try:
            await asyncio.wait_for(self._idle.wait(), grace)
        except TimeoutError:
            logger.warning(
                "Running collection cycle did not finish in %g s; cancelling it",
                grace,
            )

    @staticmethod
    def _log_skipped_tick(event: JobEvent) -> None:
        if event.code == EVENT_JOB_MAX_INSTANCES:
            logger.warning(
                "Previous collection cycle still running; skipping this tick"
            )
        else:
            logger.warning("Collection tick missed; the scheduler was behind")
