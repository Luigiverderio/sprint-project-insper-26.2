"""Testes da gravação dos ciclos de coleta no bruto (card T12).

Os de banco sobem um PostgreSQL descartável com testcontainers, na imagem do
``make up``, aplicam as migrações e gravam com o login do coletor
(``onibus_coletor``), como a coleta contínua faz. Sem Docker, são pulados.
"""

import gzip
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from alembic import command
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine
from tenacity import wait_none
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import ExecConfig

from onibus.coleta.coletor import (
    CycleDeadlineError,
    CycleResult,
    CycleStatus,
    PositionCollector,
)
from onibus.coleta.etapas import Stage
from onibus.coleta.gravacao import (
    CycleRecorder,
    build_batch,
    classify,
    http_status,
)
from onibus.coleta.olho_vivo import (
    InvalidResponseError,
    InvalidTokenError,
    OlhoVivoClient,
    OlhoVivoError,
    PositionsSnapshot,
    SessionExpiredError,
    UnavailableError,
)
from onibus.config import Settings
from onibus.db.connection import create_collector_engine

DB_NAME = "onibus"
ADMIN = "postgres"
COLETOR = "coletor_app"
PASSWORDS = {ADMIN: "senha-admin-teste", COLETOR: "senha-coletor-teste"}
LOGIN = (
    f"CREATE ROLE {COLETOR} LOGIN PASSWORD '{PASSWORDS[COLETOR]}'"
    " IN ROLE onibus_coletor;"
)
STARTED_AT = datetime(2026, 11, 19, 15, 0, tzinfo=UTC)


def _vehicle(prefix: int, latitude: float = -23.5, **extra: Any) -> dict[str, Any]:
    return {
        "p": prefix,
        "a": True,
        "ta": "2026-11-19T14:59:50Z",
        "py": latitude,
        "px": -46.6,
    } | extra


def _positions(base: int) -> dict[str, Any]:
    """Resposta de ``/Posicao``: 4 veículos, dos quais 2 são descartados.

    Prefixos a partir de ``base``, para cada teste usar veículos próprios no
    banco compartilhado pelo módulo.
    """
    return {
        "hr": "11:59",
        "l": [
            {
                "c": "701A-10",
                "cl": 1273,
                "sl": 1,
                "lt0": "METRÔ SANTANA",
                "lt1": "JD. PERI",
                "qv": 3,
                "vs": [
                    _vehicle(base),
                    _vehicle(base + 1, ta="2026-11-19T15:00:05Z"),
                    _vehicle(base + 2, latitude=-123.0),  # fora da faixa
                ],
            },
            {
                "c": "8000-10",
                "cl": 33000,
                "sl": 3,  # sentido que o bruto não aceita
                "lt0": "LAPA",
                "lt1": "PÇA. RAMOS",
                "qv": 1,
                "vs": [_vehicle(base + 3)],
            },
        ],
    }


def _snapshot(base: int) -> PositionsSnapshot:
    return PositionsSnapshot.model_validate(_positions(base))


def _result(
    status: CycleStatus = CycleStatus.FAILURE,
    error: BaseException | None = None,
    snapshot: PositionsSnapshot | None = None,
) -> CycleResult:
    return CycleResult(
        started_at=STARTED_AT,
        finished_at=STARTED_AT + timedelta(seconds=2),
        status=status,
        attempts=1,
        record_count=snapshot.vehicle_count if snapshot else 0,
        failure_cause=f"{type(error).__name__}: {error}" if error else None,
        error=error,
        snapshot=snapshot,
        stage_durations={Stage.REQUEST: 1.5},
    )


# --- Sem banco ---------------------------------------------------------------


def test_batch_keeps_valid_positions_and_counts_the_rest() -> None:
    batch = build_batch(_snapshot(100))

    assert len(batch) == 2
    assert batch.rejected == 2
    assert batch.prefixo == [100, 101]
    assert batch.cod_linha_sentido == [1273, 1273]
    assert batch.sentido_api == [1, 1]
    assert batch.horario_gps == [
        datetime(2026, 11, 19, 14, 59, 50, tzinfo=UTC),
        datetime(2026, 11, 19, 15, 0, 5, tzinfo=UTC),
    ]


def test_batch_reads_time_without_zone_as_utc() -> None:
    positions = _positions(100)
    positions["l"][0]["vs"][0]["ta"] = "2026-11-19T14:59:50"

    batch = build_batch(PositionsSnapshot.model_validate(positions))

    assert batch.horario_gps[0] == datetime(2026, 11, 19, 14, 59, 50, tzinfo=UTC)


@pytest.mark.parametrize(
    ("error", "status", "code"),
    [
        (InvalidTokenError("recusado", status_code=200), "erro_autenticacao", 200),
        (SessionExpiredError("401", status_code=401), "erro_autenticacao", 401),
        (InvalidResponseError("html", status_code=200), "resposta_invalida", 200),
        (UnavailableError("503", status_code=503), "erro_http", 503),
        (OlhoVivoError("404", status_code=404), "erro_http", 404),
        (httpx.ConnectError("sem rede"), "erro_rede", None),
        (CycleDeadlineError("passou de 25 s"), "erro_rede", None),
    ],
)
def test_failed_cycle_is_classified_by_its_error(
    error: BaseException, status: str, code: int | None
) -> None:
    result = _result(error=error)

    assert classify(result) == status
    assert http_status(result) == code


def test_successful_cycle_is_ok_with_http_200() -> None:
    result = _result(CycleStatus.SUCCESS, snapshot=_snapshot(100))

    assert classify(result) == "ok"
    assert http_status(result) == 200


# --- Com banco ---------------------------------------------------------------


def run_sql(db: PostgresContainer, sql: str) -> str:
    """Roda ``sql`` como dono do banco e devolve a saída (``|`` separa colunas)."""
    result = db.exec(
        ExecConfig(
            command=["psql", "-h", "127.0.0.1", "-U", ADMIN, "-d", DB_NAME]
            + ["-v", "ON_ERROR_STOP=1", "-tAc", sql],
            environment={"PGPASSWORD": PASSWORDS[ADMIN]},
        )
    )
    output: str = result.output.decode()
    assert result.exit_code == 0, output
    return output.strip()


@pytest.fixture(scope="module")
def db(
    db_image: str, alembic_config: Callable[[PostgresContainer, str], Config]
) -> Iterator[PostgresContainer]:
    """Banco migrado com o login do coletor."""
    container = PostgresContainer(
        db_image,
        username=ADMIN,
        password=PASSWORDS[ADMIN],
        dbname=DB_NAME,
        driver=None,
    )
    with container:
        command.upgrade(alembic_config(container, DB_NAME), "head")
        run_sql(container, LOGIN)
        yield container


def _settings(db: PostgresContainer, password: str = PASSWORDS[COLETOR]) -> Settings:
    return Settings(
        database_host=db.get_container_host_ip(),
        database_port=int(db.get_exposed_port(5432)),
        database_name=DB_NAME,
        coletor_database_user=COLETOR,
        coletor_database_password=SecretStr(password),
    )


@pytest.fixture
async def engine(db: PostgresContainer) -> AsyncIterator[AsyncEngine]:
    engine = create_collector_engine(_settings(db))
    assert engine is not None
    yield engine
    await engine.dispose()


def _cycle_row(db: PostgresContainer, cycle_id: object, columns: str) -> list[str]:
    return run_sql(
        db, f"SELECT {columns} FROM bruto.ciclos_coleta WHERE id = '{cycle_id}'"
    ).split("|")


async def test_collected_cycle_is_recorded_with_its_positions(
    db: PostgresContainer, engine: AsyncEngine
) -> None:
    async def olho_vivo(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login/Autenticar"):
            return httpx.Response(200, json=True)
        return httpx.Response(200, json=_positions(1000))

    http = httpx.AsyncClient(transport=httpx.MockTransport(olho_vivo))
    async with OlhoVivoClient("ok", base_url="https://ov.test", http_client=http) as c:
        result = await PositionCollector(c, wait=wait_none()).run_cycle()

    recorded = await CycleRecorder(engine, collector_version="teste").write(result)

    assert (recorded.received, recorded.inserted, recorded.rejected) == (4, 2, 2)
    row = _cycle_row(
        db,
        recorded.cycle_id,
        "fonte, status, http_status, hora_api, n_recebidos, n_inseridos,"
        " n_rejeitados, versao_coletor, erro IS NULL,"
        " concluido_em >= iniciado_em,"
        " duracao_login_s >= 0, duracao_requisicao_s >= 0,"
        " duracao_leitura_s >= 0, duracao_gravacao_s >= 0,"
        " duracao_espera_s IS NULL",
    )
    assert (
        row
        == ["olhovivo_posicao", "ok", "200", "11:59", "4", "2", "2", "teste"]
        + ["t"] * 7
    )
    positions = run_sql(
        db,
        "SELECT prefixo, cod_linha_sentido, sentido_api, acessivel, latitude,"
        " longitude, horario_gps AT TIME ZONE 'UTC'"
        f" FROM bruto.posicoes_gps WHERE ciclo_id = '{recorded.cycle_id}'"
        " ORDER BY prefixo",
    )
    assert positions.splitlines() == [
        "1000|1273|1|t|-23.5|-46.6|2026-11-19 14:59:50",
        "1001|1273|1|t|-23.5|-46.6|2026-11-19 15:00:05",
    ]


async def test_line_catalogue_refreshes_timestamp_hourly_and_metadata_immediately(
    db: PostgresContainer, engine: AsyncEngine, caplog: pytest.LogCaptureFixture
) -> None:
    payload = _positions(6000)
    payload["l"][0]["cl"] = 91273
    recorder = CycleRecorder(engine)
    initial = _result(
        CycleStatus.SUCCESS, snapshot=PositionsSnapshot.model_validate(payload)
    )

    def catalogue() -> list[str]:
        return run_sql(
            db,
            "SELECT letreiro, sentido_api, letreiro_destino, letreiro_origem,"
            " extract(epoch FROM visto_primeiro_em)::bigint,"
            " extract(epoch FROM visto_ultimo_em)::bigint"
            " FROM referencia.linhas_olhovivo WHERE cod_linha_sentido = 91273",
        ).split("|")

    await recorder.write(initial)
    started = int(initial.started_at.timestamp())
    first = catalogue()
    assert first == [
        "701A-10",
        "1",
        "METRÔ SANTANA",
        "JD. PERI",
        str(started),
        str(started),
    ]

    refresh = initial.started_at + timedelta(hours=1)
    await recorder.write(replace(initial, started_at=refresh - timedelta(seconds=1)))
    assert catalogue() == first

    await recorder.write(replace(initial, started_at=refresh))
    assert catalogue() == [*first[:-1], str(started + 3600)]

    payload["l"][0]["c"] = "701A-21"
    payload["l"][0]["lt0"] = "TERMINAL NOVO"
    changed_at = refresh + timedelta(seconds=30)
    changed = replace(
        initial,
        started_at=changed_at,
        snapshot=PositionsSnapshot.model_validate(payload),
    )
    with caplog.at_level(logging.INFO, logger="onibus.coleta.gravacao"):
        await recorder.write(changed)
    assert catalogue() == [
        "701A-21",
        "1",
        "TERMINAL NOVO",
        "JD. PERI",
        str(started),
        str(started + 3600),
    ]
    assert "catalogue metadata changed for cl: [91273]" in caplog.text

    # Repetir o ciclo que renovou o avistamento não reverte o letreiro.
    await recorder.write(replace(initial, started_at=refresh))
    assert catalogue()[0] == "701A-21"

    await recorder.write(replace(changed, started_at=refresh + timedelta(hours=1)))
    assert catalogue()[-2:] == [str(started), str(started + 7200)]


async def test_cycle_archives_original_body_and_keeps_optional_fields_null(
    db: PostgresContainer, engine: AsyncEngine, tmp_path: Path
) -> None:
    now = datetime.now(UTC)
    payload = _positions(7000)
    line = payload["l"][0]
    line["cl"] = 91274
    line["lt0"] = "destino\x00inválido"
    del line["lt1"]
    del line["vs"][0]["a"]
    line["vs"][0]["ta"] = now.isoformat()
    del line["vs"][1]["ta"]
    raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode()

    def olho_vivo(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/Login/Autenticar"):
            return httpx.Response(200, json=True)
        return httpx.Response(200, content=raw)

    http = httpx.AsyncClient(transport=httpx.MockTransport(olho_vivo))
    async with OlhoVivoClient(
        "ok", base_url="https://ov.test", http_client=http
    ) as client:
        result = await PositionCollector(
            client, wait=wait_none(), clock=lambda: now
        ).run_cycle()

    recorded = await CycleRecorder(engine, archive_dir=tmp_path).write(result)

    assert (recorded.received, recorded.inserted, recorded.rejected) == (4, 1, 3)
    [path] = _cycle_row(db, recorded.cycle_id, "arquivo_bruto")
    assert path == f"{now:%Y/%m/%d}/{recorded.cycle_id}.json.gz"
    assert gzip.decompress((tmp_path / path).read_bytes()) == raw
    assert (
        run_sql(
            db,
            "SELECT letreiro_destino IS NULL, letreiro_origem IS NULL"
            " FROM referencia.linhas_olhovivo WHERE cod_linha_sentido = 91274",
        )
        == "t|t"
    )
    assert (
        run_sql(
            db,
            "SELECT codigo_linha, acessivel IS NULL FROM operacao.posicoes_atuais"
            " WHERE prefixo = 7000",
        )
        == "701A-10|t"
    )


async def test_archive_disk_error_does_not_lose_cycle(
    db: PostgresContainer,
    engine: AsyncEngine,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    blocked = tmp_path / "file"
    blocked.write_text("occupied", encoding="utf-8")
    snapshot = _snapshot(8000).model_copy(
        update={"raw_body": b"ignored by failing disk"}
    )
    recorder = CycleRecorder(engine, archive_dir=blocked)

    recorded = await recorder.write(_result(CycleStatus.SUCCESS, snapshot=snapshot))

    assert recorded.inserted == 2
    assert _cycle_row(db, recorded.cycle_id, "status, arquivo_bruto IS NULL") == [
        "ok",
        "t",
    ]
    assert "Could not archive Olho Vivo cycle" in caplog.text


async def test_repeated_positions_are_ignored(
    db: PostgresContainer, engine: AsyncEngine
) -> None:
    recorder = CycleRecorder(engine, collector_version="teste")
    result = _result(CycleStatus.SUCCESS, snapshot=_snapshot(2000))

    first = await recorder.write(result)
    second = await recorder.write(result)

    assert first.inserted == 2
    assert (second.received, second.inserted, second.rejected) == (4, 0, 2)
    owners = run_sql(
        db,
        "SELECT DISTINCT ciclo_id FROM bruto.posicoes_gps"
        " WHERE prefixo BETWEEN 2000 AND 2003",
    )
    assert owners == str(first.cycle_id)


async def test_failed_cycle_is_recorded_without_positions(
    db: PostgresContainer, engine: AsyncEngine
) -> None:
    error = UnavailableError("Olho Vivo returned 503 on /Posicao", status_code=503)

    recorded = await CycleRecorder(engine, collector_version="teste").write(
        _result(error=error)
    )

    row = _cycle_row(
        db,
        recorded.cycle_id,
        "status, http_status, erro, n_recebidos IS NULL, hora_api IS NULL,"
        " duracao_requisicao_s, duracao_gravacao_s IS NULL",
    )
    assert row == [
        "erro_http",
        "503",
        "UnavailableError: Olho Vivo returned 503 on /Posicao",
        "t",
        "t",
        "1.5",
        "t",
    ]
    count = run_sql(
        db,
        "SELECT count(*) FROM bruto.posicoes_gps"
        f" WHERE ciclo_id = '{recorded.cycle_id}'",
    )
    assert count == "0"


@pytest.mark.parametrize("ends_without_response", [False, True])
async def test_failed_cycle_archives_last_position_response(
    db: PostgresContainer,
    engine: AsyncEngine,
    tmp_path: Path,
    ends_without_response: bool,
) -> None:
    raw = b'{\n  "erro": "servico indisponivel"\n}\n'
    response_count = 0

    def olho_vivo(request: httpx.Request) -> httpx.Response:
        nonlocal response_count
        if request.url.path.endswith("/Login/Autenticar"):
            return httpx.Response(200, json=True)
        response_count += 1
        if response_count == 2 and ends_without_response:
            raise httpx.ConnectTimeout("timeout")
        if response_count == 1 and not ends_without_response:
            return httpx.Response(503, content=b'{"erro":"primeira tentativa"}')
        return httpx.Response(503, content=raw)

    http = httpx.AsyncClient(transport=httpx.MockTransport(olho_vivo))
    async with OlhoVivoClient(
        "ok", base_url="https://ov.test", http_client=http
    ) as client:
        result = await PositionCollector(
            client, max_attempts=2, wait=wait_none()
        ).run_cycle()

    recorded = await CycleRecorder(engine, archive_dir=tmp_path).write(result)

    assert result.status is CycleStatus.FAILURE
    assert result.attempts == 2
    status, path = _cycle_row(db, recorded.cycle_id, "status, arquivo_bruto")
    assert status == ("erro_rede" if ends_without_response else "erro_http")
    assert path == f"{result.started_at:%Y/%m/%d}/{recorded.cycle_id}.json.gz"
    assert gzip.decompress((tmp_path / path).read_bytes()) == raw


async def test_recorded_cycles_feed_collection_health(
    db: PostgresContainer, engine: AsyncEngine
) -> None:
    await CycleRecorder(engine, collector_version="teste").write(
        _result(CycleStatus.SUCCESS, snapshot=_snapshot(3000))
    )

    health = run_sql(
        db,
        "SELECT ultimo_ok IS NOT NULL FROM operacao.saude_coleta"
        " WHERE fonte = 'olhovivo_posicao'",
    )
    assert health == "t"


async def test_database_failure_is_logged_and_does_not_raise(
    db: PostgresContainer, caplog: pytest.LogCaptureFixture
) -> None:
    engine = create_collector_engine(_settings(db, password="senha-incorreta"))
    assert engine is not None
    try:
        await CycleRecorder(engine).record(
            _result(CycleStatus.SUCCESS, snapshot=_snapshot(4000))
        )
    finally:
        await engine.dispose()

    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert len(errors) == 1
    assert "Could not record collection cycle" in errors[0].getMessage()
    assert "senha-incorreta" not in caplog.text


def test_collector_engine_needs_password() -> None:
    assert create_collector_engine(Settings()) is None
    assert (
        create_collector_engine(Settings(coletor_database_password=SecretStr("")))
        is None
    )
