import asyncio
import logging
import os
import time
from pathlib import Path

import pytest

from onibus.config import Settings
from onibus.processamento.__main__ import run
from onibus.processamento.heartbeat import HEARTBEAT_MAX_AGE_S, beat, is_fresh, main


async def test_worker_runs_until_stop_event(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    stop = asyncio.Event()
    settings = Settings(environment="test")

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(run(stop, settings, tmp_path / "heartbeat"))
        await asyncio.sleep(0)
        assert not task.done()

        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert "Worker started environment=test" in caplog.text
    assert "Worker stopped" in caplog.text


async def test_worker_beats_while_running_and_cleans_up(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    stop = asyncio.Event()

    task = asyncio.create_task(run(stop, Settings(environment="test"), heartbeat))
    await asyncio.sleep(0)
    assert main(heartbeat) == 0

    stop.set()
    await asyncio.wait_for(task, timeout=1)
    assert not heartbeat.exists()
    assert main(heartbeat) == 1


async def test_beat_refreshes_the_file_every_interval(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    stop = asyncio.Event()
    task = asyncio.create_task(beat(heartbeat, stop, interval_s=0.01))
    await asyncio.sleep(0)
    os.utime(heartbeat, (0, 0))

    await asyncio.sleep(0.1)
    assert heartbeat.stat().st_mtime > 0

    stop.set()
    await asyncio.wait_for(task, timeout=1)


async def test_beat_removes_the_file_when_cancelled(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    task = asyncio.create_task(beat(heartbeat, asyncio.Event(), interval_s=10))
    await asyncio.sleep(0)
    assert heartbeat.exists()

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not heartbeat.exists()


def test_is_fresh_depends_on_the_file_age(tmp_path: Path) -> None:
    heartbeat = tmp_path / "heartbeat"
    assert not is_fresh(heartbeat, HEARTBEAT_MAX_AGE_S)

    heartbeat.touch()
    assert is_fresh(heartbeat, HEARTBEAT_MAX_AGE_S)

    stale = time.time() - HEARTBEAT_MAX_AGE_S - 1
    os.utime(heartbeat, (stale, stale))
    assert not is_fresh(heartbeat, HEARTBEAT_MAX_AGE_S)
    assert main(heartbeat) == 1
