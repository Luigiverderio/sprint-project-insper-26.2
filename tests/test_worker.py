import asyncio
import logging

import pytest

from onibus.config import Settings
from onibus.processamento.__main__ import run


async def test_worker_runs_until_stop_event(caplog: pytest.LogCaptureFixture) -> None:
    stop = asyncio.Event()
    settings = Settings(environment="test")

    with caplog.at_level(logging.INFO):
        task = asyncio.create_task(run(stop, settings))
        await asyncio.sleep(0)
        assert not task.done()

        stop.set()
        await asyncio.wait_for(task, timeout=1)

    assert "Worker started environment=test" in caplog.text
    assert "Worker stopped" in caplog.text
