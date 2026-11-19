import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from apscheduler.triggers.interval import IntervalTrigger

from onibus.coleta.__main__ import collect_forever
from onibus.coleta.coletor import CycleResult, CycleStatus
from onibus.coleta.scheduler import CYCLE_INTERVAL, CollectionScheduler
from onibus.config import Settings

FAST = timedelta(milliseconds=10)
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
RESULT = CycleResult(
    started_at=NOW,
    finished_at=NOW,
    status=CycleStatus.SUCCESS,
    attempts=1,
    record_count=2,
)


class FakeRunner:
    """Ciclo simulado. Com ``gate``, o ciclo fica preso até ``gate.set()``."""

    def __init__(self, gate: asyncio.Event | None = None) -> None:
        self.gate = gate
        self.calls = 0
        self.running = 0
        self.max_running = 0

    async def run_cycle(self) -> CycleResult:
        self.calls += 1
        self.running += 1
        self.max_running = max(self.max_running, self.running)
        try:
            if self.gate is not None:
                await self.gate.wait()
        finally:
            self.running -= 1
        return RESULT


async def _until(condition: Callable[[], bool], timeout: float = 2.0) -> None:
    async def poll() -> None:
        while not condition():
            await asyncio.sleep(0.005)

    await asyncio.wait_for(poll(), timeout)


def test_default_job_runs_every_30_seconds_without_overlap() -> None:
    assert CYCLE_INTERVAL == timedelta(seconds=30)

    jobs = CollectionScheduler(FakeRunner()).build_scheduler().get_jobs()

    assert len(jobs) == 1
    job = jobs[0]
    assert isinstance(job.trigger, IntervalTrigger)
    assert job.trigger.interval == timedelta(seconds=30)
    assert job.max_instances == 1
    assert job.coalesce is True


async def test_runs_a_cycle_right_away_and_repeats() -> None:
    runner = FakeRunner()
    handled: list[CycleResult] = []

    async def on_cycle(result: CycleResult) -> None:
        handled.append(result)

    scheduler = CollectionScheduler(runner, interval=FAST, on_cycle=on_cycle)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))

    await _until(lambda: len(handled) >= 3)
    stop.set()
    await asyncio.wait_for(task, 2)

    assert handled[0] is RESULT
    assert runner.calls >= 3


async def test_never_overlaps_cycles_and_logs_the_skipped_tick(
    caplog: pytest.LogCaptureFixture,
) -> None:
    gate = asyncio.Event()
    runner = FakeRunner(gate)
    scheduler = CollectionScheduler(runner, interval=FAST)
    stop = asyncio.Event()

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(scheduler.run(stop))
        await _until(lambda: "still running" in caplog.text)
        gate.set()
        stop.set()
        await asyncio.wait_for(task, 2)

    assert runner.max_running == 1


async def test_stop_waits_for_the_running_cycle() -> None:
    gate = asyncio.Event()
    runner = FakeRunner(gate)
    handled: list[CycleResult] = []

    async def on_cycle(result: CycleResult) -> None:
        handled.append(result)

    scheduler = CollectionScheduler(runner, interval=FAST, on_cycle=on_cycle)
    stop = asyncio.Event()
    task = asyncio.create_task(scheduler.run(stop))

    await _until(lambda: runner.running == 1)
    stop.set()
    await asyncio.sleep(0.05)
    assert not task.done()

    gate.set()
    await asyncio.wait_for(task, 2)

    assert handled == [RESULT]


async def test_stop_gives_up_on_a_cycle_that_does_not_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner(asyncio.Event())
    scheduler = CollectionScheduler(
        runner, interval=FAST, shutdown_grace=timedelta(milliseconds=50)
    )
    stop = asyncio.Event()

    with caplog.at_level(logging.WARNING):
        task = asyncio.create_task(scheduler.run(stop))
        await _until(lambda: runner.running == 1)
        stop.set()
        await asyncio.wait_for(task, 2)

    assert "did not finish" in caplog.text
    await _until(lambda: runner.running == 0)


async def test_handler_error_does_not_stop_the_schedule(
    caplog: pytest.LogCaptureFixture,
) -> None:
    runner = FakeRunner()
    calls = 0

    async def on_cycle(result: CycleResult) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("falha ao gravar o ciclo")

    scheduler = CollectionScheduler(runner, interval=FAST, on_cycle=on_cycle)
    stop = asyncio.Event()

    with caplog.at_level(logging.ERROR):
        task = asyncio.create_task(scheduler.run(stop))
        await _until(lambda: calls >= 3)
        stop.set()
        await asyncio.wait_for(task, 2)

    assert "falha ao gravar o ciclo" in caplog.text


async def test_collect_forever_requires_token(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert await collect_forever(Settings(), asyncio.Event()) == 2
    assert "APP_OLHO_VIVO_TOKEN" in capsys.readouterr().err
