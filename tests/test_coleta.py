import asyncio
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tenacity import wait_fixed, wait_none

from onibus.coleta.__main__ import collect_forever, collect_once
from onibus.coleta.coletor import CycleStatus, PositionCollector
from onibus.coleta.etapas import Stage, StageTimer, format_durations
from onibus.coleta.olho_vivo import (
    InvalidResponseError,
    InvalidTokenError,
    OlhoVivoClient,
    OlhoVivoError,
    SessionExpiredError,
    UnavailableError,
)
from onibus.config import Settings

BASE_URL = "https://olhovivo.test/v2.1"

POSITIONS: dict[str, Any] = {
    "hr": "15:30",
    "l": [
        {
            "c": "701A-10",
            "cl": 1273,
            "sl": 1,
            "lt0": "METRÔ SANTANA",
            "lt1": "JD. PERI",
            "qv": 2,
            "vs": [
                {
                    "p": 11104,
                    "a": True,
                    "ta": "2026-11-18T18:29:58Z",
                    "py": -23.47,
                    "px": -46.62,
                },
                {
                    "p": 11221,
                    "a": False,
                    "ta": "2026-11-18T18:30:05Z",
                    "py": -23.48,
                    "px": -46.63,
                },
            ],
        }
    ],
}

# Roteiro de /Posicao: código HTTP, "invalid" (corpo que não é JSON), "hang"
# (nunca responde) ou exceção.
Reply = int | str | Exception


class FakeOlhoVivo:
    """API simulada: o login aceita um token e ``/Posicao`` segue um roteiro."""

    def __init__(self, positions: list[Reply] | None = None, token: str = "ok") -> None:
        self.positions = list(positions or [])
        self.token = token
        self.logins = 0
        self.position_calls = 0

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login/Autenticar"):
            self.logins += 1
            return httpx.Response(200, json=request.url.params["token"] == self.token)
        if request.url.path.endswith("/Posicao"):
            self.position_calls += 1
            reply = self.positions.pop(0) if self.positions else 200
            if isinstance(reply, Exception):
                raise reply
            if reply == "invalid":
                return httpx.Response(200, text="<html>erro</html>")
            if reply == "hang":
                await asyncio.sleep(3600)
            assert isinstance(reply, int)
            return httpx.Response(reply, json=POSITIONS if reply == 200 else None)
        return httpx.Response(404)

    def client(self, token: str = "ok") -> OlhoVivoClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return OlhoVivoClient(token, base_url=BASE_URL, http_client=http)


def _collector(
    client: OlhoVivoClient, clock: Callable[[], datetime] | None = None
) -> PositionCollector:
    if clock is None:
        return PositionCollector(client, wait=wait_none())
    return PositionCollector(client, wait=wait_none(), clock=clock)


class FakeClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


async def test_client_authenticates_and_parses_positions() -> None:
    api = FakeOlhoVivo()
    async with api.client() as client:
        snapshot = await client.get_positions()

    assert api.logins == 1
    assert snapshot.reference_time == "15:30"
    line = snapshot.lines[0]
    assert (line.code, line.line_id, line.direction) == ("701A-10", 1273, 1)
    vehicle = line.vehicles[0]
    assert vehicle.prefix == 11104
    assert vehicle.accessible is True
    assert vehicle.reported_at == datetime(2026, 11, 18, 18, 29, 58, tzinfo=UTC)
    assert (vehicle.latitude, vehicle.longitude) == (-23.47, -46.62)
    assert snapshot.vehicle_count == 2


async def test_token_is_not_logged(caplog: pytest.LogCaptureFixture) -> None:
    api = FakeOlhoVivo(token="secret-token")
    with caplog.at_level(logging.DEBUG):
        async with api.client(token="secret-token") as client:
            await client.get_positions()

    assert "Login/Autenticar?token=***" in caplog.text
    assert "secret-token" not in caplog.text


async def test_client_reuses_session_between_calls() -> None:
    api = FakeOlhoVivo()
    async with api.client() as client:
        await client.get_positions()
        await client.get_positions()

    assert api.logins == 1


async def test_client_rejects_invalid_token() -> None:
    api = FakeOlhoVivo()
    async with api.client(token="wrong") as client:
        with pytest.raises(InvalidTokenError):
            await client.get_positions()


async def test_client_flags_expired_session() -> None:
    api = FakeOlhoVivo([401])
    async with api.client() as client:
        with pytest.raises(SessionExpiredError):
            await client.get_positions()
        assert not client.authenticated


@pytest.mark.parametrize(
    ("reply", "error", "status_code"),
    [
        (401, SessionExpiredError, 401),
        (503, UnavailableError, 503),
        (404, OlhoVivoError, 404),
        ("invalid", InvalidResponseError, 200),
    ],
)
async def test_client_errors_carry_http_status(
    reply: Reply, error: type[OlhoVivoError], status_code: int
) -> None:
    api = FakeOlhoVivo([reply])
    async with api.client() as client:
        with pytest.raises(error) as raised:
            await client.get_positions()

    assert type(raised.value) is error
    assert raised.value.status_code == status_code


async def test_cycle_success() -> None:
    api = FakeOlhoVivo()
    async with api.client() as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.SUCCESS
    assert result.attempts == 1
    assert result.record_count == 2
    assert result.failure_cause is None
    assert result.snapshot is not None


@pytest.mark.parametrize(
    "transient",
    [503, httpx.ConnectTimeout("timeout"), "invalid"],
    ids=["server-error", "timeout", "invalid-body"],
)
async def test_cycle_retries_transient_failure(transient: Reply) -> None:
    api = FakeOlhoVivo([transient, transient])
    async with api.client() as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.SUCCESS
    assert result.attempts == 3


async def test_cycle_reauthenticates_when_session_expires() -> None:
    api = FakeOlhoVivo([401])
    async with api.client() as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.SUCCESS
    assert result.attempts == 2
    assert api.logins == 2


async def test_cycle_gives_up_after_five_attempts() -> None:
    api = FakeOlhoVivo([500] * 10)
    async with api.client() as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.FAILURE
    assert result.attempts == 5
    assert api.position_calls == 5
    assert result.record_count == 0
    assert result.failure_cause is not None
    assert "UnavailableError" in result.failure_cause


async def test_cycle_stops_at_deadline_when_api_hangs() -> None:
    api = FakeOlhoVivo([500, "hang"])
    async with api.client() as client:
        collector = PositionCollector(
            client, wait=wait_none(), deadline=timedelta(seconds=0.2)
        )
        started = time.monotonic()
        result = await collector.run_cycle()
        elapsed = time.monotonic() - started

        assert result.status is CycleStatus.FAILURE
        assert result.attempts == 2
        assert result.failure_cause is not None
        assert "CycleDeadlineError" in result.failure_cause
        assert "last error: UnavailableError" in result.failure_cause
        assert elapsed < 1
        # A chamada cortada conta até o corte: é ali que o prazo foi gasto.
        assert result.stage_durations[Stage.REQUEST] >= 0.15

        # A requisição cancelada não estraga o cliente: o próximo ciclo funciona.
        assert (await collector.run_cycle()).status is CycleStatus.SUCCESS


async def test_cycle_skips_retry_whose_wait_passes_deadline() -> None:
    api = FakeOlhoVivo([500] * 10)
    async with api.client() as client:
        collector = PositionCollector(client, wait=wait_fixed(30))
        started = time.monotonic()
        result = await collector.run_cycle()

    assert time.monotonic() - started < 1
    assert result.status is CycleStatus.FAILURE
    assert result.attempts == 1
    assert api.position_calls == 1
    assert result.failure_cause is not None
    assert "UnavailableError" in result.failure_cause


async def test_cycle_records_duration_of_each_stage() -> None:
    api = FakeOlhoVivo([503])
    async with api.client() as client:
        collector = PositionCollector(client, wait=wait_fixed(0.05))
        result = await collector.run_cycle()

    assert result.status is CycleStatus.SUCCESS
    durations = result.stage_durations
    assert set(durations) == {
        Stage.LOGIN,
        Stage.REQUEST,
        Stage.PARSE,
        Stage.RETRY_WAIT,
    }
    assert all(seconds >= 0 for seconds in durations.values())
    assert durations[Stage.RETRY_WAIT] >= 0.05


async def test_failed_cycle_keeps_stage_durations() -> None:
    api = FakeOlhoVivo([500] * 10)
    async with api.client() as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.FAILURE
    assert {Stage.LOGIN, Stage.REQUEST} <= set(result.stage_durations)
    # Erro 500 não chega a ler o corpo.
    assert Stage.PARSE not in result.stage_durations


def test_stage_timer_sums_attempts_and_counts_failed_blocks() -> None:
    ticks = iter([0.0, 1.0, 5.0, 7.5])
    timer = StageTimer(clock=lambda: next(ticks))

    with timer.measure(Stage.REQUEST):
        pass
    with pytest.raises(RuntimeError), timer.measure(Stage.REQUEST):
        raise RuntimeError("falhou no meio")

    assert timer.durations() == {Stage.REQUEST: 3.5}
    assert format_durations(timer.durations()) == "request=3.50s"
    assert format_durations({}) == "no stages"


async def test_cycle_does_not_retry_invalid_token() -> None:
    api = FakeOlhoVivo()
    async with api.client(token="wrong") as client:
        result = await _collector(client).run_cycle()

    assert result.status is CycleStatus.FAILURE
    assert result.attempts == 1
    assert api.position_calls == 0
    assert result.failure_cause is not None
    assert "InvalidTokenError" in result.failure_cause


async def test_long_failure_alerts_once_and_recovery_is_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeOlhoVivo([500] * 1000)
    clock = FakeClock()
    async with api.client() as client:
        collector = _collector(client, clock)
        with caplog.at_level(logging.INFO):
            # 20 ciclos de 30 s = 9,5 min de falha: ainda sem alerta.
            for _ in range(20):
                await collector.run_cycle()
                clock.advance(30)
            assert collector.failing_since == datetime(2026, 11, 18, 12, 0, tzinfo=UTC)
            assert not _critical(caplog)

            # 10 min de falha: alerta uma vez, mesmo com mais ciclos falhando.
            for _ in range(5):
                await collector.run_cycle()
                clock.advance(30)
            assert len(_critical(caplog)) == 1
            assert "failing for 10 min" in _critical(caplog)[0].getMessage()

            api.positions.clear()
            result = await collector.run_cycle()

    assert result.status is CycleStatus.SUCCESS
    assert collector.failing_since is None
    assert "collection recovered" in caplog.text


async def test_alert_fires_again_after_recovery_and_new_long_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    api = FakeOlhoVivo([500] * 50)
    clock = FakeClock()
    async with api.client() as client:
        collector = _collector(client, clock)
        with caplog.at_level(logging.CRITICAL):
            await collector.run_cycle()
            clock.advance(600)
            await collector.run_cycle()
            api.positions.clear()
            await collector.run_cycle()
            api.positions.extend([500] * 50)
            await collector.run_cycle()
            clock.advance(600)
            await collector.run_cycle()

    assert len(_critical(caplog)) == 2


async def test_collect_once_requires_token(capsys: pytest.CaptureFixture[str]) -> None:
    assert await collect_once(Settings()) == 2
    assert "APP_OLHO_VIVO_TOKEN" in capsys.readouterr().err


async def test_schedule_requires_collector_database_login(
    capsys: pytest.CaptureFixture[str],
) -> None:
    settings = Settings(olho_vivo_token=SecretStr("token"))

    assert await collect_forever(settings, asyncio.Event()) == 2
    assert "APP_COLETOR_DATABASE_PASSWORD" in capsys.readouterr().err


def _critical(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.levelno == logging.CRITICAL]
