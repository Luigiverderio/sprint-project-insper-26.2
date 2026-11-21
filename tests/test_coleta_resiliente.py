"""Falhas simuladas da coleta (T15): o que se perde em cada cenário.

Cada cenário roda alguns ciclos com o coletor de verdade
(:class:`PositionCollector`) contra uma Olho Vivo simulada e conta o que
chegaria ao banco. Os testes descrevem o comportamento de hoje, inclusive as
perdas, para o estudo ``docs/coleta-resiliente.md`` comparar com as melhorias.
Se uma melhoria mudar o resultado, o teste do cenário muda junto.

Não usam Docker: o "banco fora" é uma porta fechada em ``127.0.0.1``.
"""

import asyncio
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from tenacity import wait_none

from onibus.coleta.coletor import CycleResult, PositionCollector
from onibus.coleta.gravacao import CycleRecorder, build_batch, classify
from onibus.coleta.olho_vivo import OlhoVivoClient
from onibus.config import Settings
from onibus.db.connection import create_collector_engine

BASE_URL = "https://olhovivo.test/v2.1"
CYCLES = 4
VEHICLES_PER_CYCLE = 2
# Prazo curto para o cenário da API lenta não segurar o teste por 25 s.
DEADLINE = timedelta(milliseconds=300)


def _positions(lines: list[dict[str, Any]]) -> dict[str, Any]:
    return {"hr": "15:30", "l": lines}


FLEET = _positions(
    [
        {
            "c": "701A-10",
            "cl": 1273,
            "sl": 1,
            "lt0": "METRÔ SANTANA",
            "lt1": "JD. PERI",
            "qv": VEHICLES_PER_CYCLE,
            "vs": [
                {
                    "p": 11104 + n,
                    "a": True,
                    "ta": "2026-11-18T18:29:58Z",
                    "py": -23.47,
                    "px": -46.62,
                }
                for n in range(VEHICLES_PER_CYCLE)
            ],
        }
    ]
)
EMPTY = _positions([])


class ScriptedOlhoVivo:
    """O login sempre aceita; ``/Posicao`` responde conforme ``mode``.

    ``ok``: a frota; ``down``: erro de conexão; ``slow``: nunca responde;
    ``empty``: 200 sem nenhuma linha.
    """

    def __init__(self, mode: str) -> None:
        self.mode = mode

    async def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login/Autenticar"):
            return httpx.Response(200, json=True)
        if self.mode == "down":
            raise httpx.ConnectError("simulated: API unreachable", request=request)
        if self.mode == "slow":
            await asyncio.sleep(3600)
        return httpx.Response(200, json=EMPTY if self.mode == "empty" else FLEET)

    def client(self) -> OlhoVivoClient:
        http = httpx.AsyncClient(transport=httpx.MockTransport(self.handler))
        return OlhoVivoClient("token", base_url=BASE_URL, http_client=http)


@dataclass
class Outcome:
    """O que sobrou de ``CYCLES`` ciclos, do ponto de vista do banco."""

    received: int = 0  # posições que a API entregou ao coletor
    stored: int = 0  # posições que chegariam a bruto.posicoes_gps
    ok_rows: int = 0  # ciclos gravados com status 'ok'
    failure_rows: int = 0  # ciclos gravados com status de erro
    missing_rows: int = 0  # ciclos que não deixaram nenhuma linha

    @property
    def lost(self) -> int:
        """Posições que a API mandaria num ciclo normal e não foram gravadas."""
        return CYCLES * VEHICLES_PER_CYCLE - self.stored


class MemoryRecorder:
    """Faz o que o :class:`CycleRecorder` gravaria, sem banco."""

    def __init__(self) -> None:
        self.outcome = Outcome()

    async def record(self, result: CycleResult) -> None:
        if result.snapshot is not None:
            self.outcome.received += result.snapshot.vehicle_count
            self.outcome.stored += len(build_batch(result.snapshot))
        if classify(result) == "ok":
            self.outcome.ok_rows += 1
        else:
            self.outcome.failure_rows += 1


async def _run(mode: str) -> tuple[list[CycleResult], Outcome]:
    recorder = MemoryRecorder()
    results = []
    async with ScriptedOlhoVivo(mode).client() as client:
        collector = PositionCollector(client, wait=wait_none(), deadline=DEADLINE)
        for _ in range(CYCLES):
            result = await collector.run_cycle()
            results.append(result)
            await recorder.record(result)
    return results, recorder.outcome


def _statuses(results: Sequence[CycleResult]) -> set[str]:
    return {classify(result) for result in results}


async def test_normal_operation_loses_nothing() -> None:
    results, outcome = await _run("ok")

    assert _statuses(results) == {"ok"}
    assert outcome == Outcome(received=8, stored=8, ok_rows=CYCLES)
    assert outcome.lost == 0


async def test_api_down_loses_every_cycle_but_records_the_gap() -> None:
    results, outcome = await _run("down")

    # Cada ciclo gasta as 5 tentativas e termina como erro de rede. As
    # posições daquele momento não voltam, mas a falha fica no bruto e a
    # lacuna aparece em operacao.lacunas_coleta.
    assert _statuses(results) == {"erro_rede"}
    assert {result.attempts for result in results} == {5}
    assert outcome == Outcome(failure_rows=CYCLES)
    assert outcome.lost == CYCLES * VEHICLES_PER_CYCLE


async def test_slow_api_is_cut_by_the_deadline_and_recorded() -> None:
    results, outcome = await _run("slow")

    # O prazo corta a requisição presa: o ciclo não invade o seguinte e é
    # gravado como erro de rede (CycleDeadlineError).
    assert _statuses(results) == {"erro_rede"}
    assert all("exceeded" in (result.failure_cause or "") for result in results)
    assert all(
        result.finished_at - result.started_at < DEADLINE * 2 for result in results
    )
    assert outcome == Outcome(failure_rows=CYCLES)


async def test_empty_response_is_recorded_as_ok_and_hides_the_gap() -> None:
    results, outcome = await _run("empty")

    # Perda silenciosa: zero veículos entra como ciclo 'ok', então a view de
    # lacunas, que só olha ciclos 'ok', não vê buraco nenhum.
    assert _statuses(results) == {"ok"}
    assert outcome == Outcome(ok_rows=CYCLES)
    assert outcome.lost == CYCLES * VEHICLES_PER_CYCLE


async def test_database_down_loses_positions_and_cycle_rows(
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = Settings(
        database_host="127.0.0.1",
        database_port=1,  # nada escuta aqui: conexão recusada
        coletor_database_password=SecretStr("qualquer"),
    )
    engine = create_collector_engine(settings)
    assert engine is not None
    outcome = Outcome()
    try:
        async with ScriptedOlhoVivo("ok").client() as client:
            collector = PositionCollector(client, wait=wait_none(), deadline=DEADLINE)
            recorder = CycleRecorder(engine)
            for _ in range(CYCLES):
                result = await collector.run_cycle()
                assert result.snapshot is not None
                outcome.received += result.snapshot.vehicle_count
                with caplog.at_level(logging.ERROR, "onibus.coleta.gravacao"):
                    await recorder.record(result)
    finally:
        await engine.dispose()

    # A coleta deu certo, mas nada foi gravado e nenhum ciclo deixou linha: a
    # perda só aparece no log. É a pior falha, porque ninguém vê pelo banco.
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    outcome.missing_rows = len(errors)
    assert all("Could not record" in r.getMessage() for r in errors)
    assert outcome == Outcome(received=8, missing_rows=CYCLES)
    assert outcome.lost == CYCLES * VEHICLES_PER_CYCLE
