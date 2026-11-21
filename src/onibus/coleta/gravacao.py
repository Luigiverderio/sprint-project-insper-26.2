"""Gravação de cada ciclo de coleta no bruto: as posições e a linha do ciclo.

Tudo numa transação, com o login do coletor (papel ``onibus_coletor``):

1. as posições válidas entram em ``bruto.posicoes_gps`` com
   ``ON CONFLICT DO NOTHING``, porque a Olho Vivo repete o mesmo ``ta`` em
   consultas seguidas e a chave primária descarta a repetição;
2. a linha de ``bruto.ciclos_coleta`` entra por último, já com quantas posições
   vieram, quantas entraram e quantas o coletor descartou, e com a duração de
   cada etapa. O bruto é só de inserção, então ela não pode ser corrigida
   depois. A FK ``posicoes_gps.ciclo_id`` é deferida e só confere no commit.

Ciclos com falha também são gravados, sem posições, com o tipo do erro em
``status`` e a descrição em ``erro``: são eles que mostram as lacunas da coleta
e a saúde no ``/health``.

Uma falha ao gravar vai para o log e não para a coleta. O ciclo seguinte roda
normalmente, e o ciclo perdido fica sem linha em ``ciclos_coleta``.
"""

import asyncio
import gzip
import logging
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from tempfile import NamedTemporaryFile
from uuid import UUID

import httpx
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from onibus.coleta.coletor import CycleResult, CycleStatus
from onibus.coleta.etapas import Stage, StageTimer, format_durations
from onibus.coleta.olho_vivo import (
    InvalidResponseError,
    InvalidTokenError,
    LinePositions,
    OlhoVivoError,
    PositionsSnapshot,
    SessionExpiredError,
)

logger = logging.getLogger(__name__)

SOURCE = "olhovivo_posicao"

try:
    COLLECTOR_VERSION = version("onibus-backend")
except PackageNotFoundError:  # pragma: no cover - rodando sem instalar o pacote
    COLLECTOR_VERSION = "desconhecida"

DURATION_COLUMNS = {
    Stage.LOGIN: "duracao_login_s",
    Stage.REQUEST: "duracao_requisicao_s",
    Stage.PARSE: "duracao_leitura_s",
    Stage.RETRY_WAIT: "duracao_espera_s",
    Stage.WRITE: "duracao_gravacao_s",
}

# Uma posição por elemento dos arrays: um comando só para a frota inteira.
INSERT_POSITIONS = text(
    """
    INSERT INTO bruto.posicoes_gps
        (horario_gps, prefixo, cod_linha_sentido, sentido_api, acessivel,
         latitude, longitude, ciclo_id)
    SELECT p.*, CAST(:ciclo_id AS uuid)
      FROM unnest(
             CAST(:horario_gps AS timestamptz[]),
             CAST(:prefixo AS integer[]),
             CAST(:cod_linha_sentido AS integer[]),
             CAST(:sentido_api AS smallint[]),
             CAST(:acessivel AS boolean[]),
             CAST(:latitude AS double precision[]),
             CAST(:longitude AS double precision[])
           ) AS p
    ON CONFLICT DO NOTHING
    """
)

UPSERT_LINES = text(
    """
    WITH written AS (
        INSERT INTO referencia.linhas_olhovivo AS known
            (cod_linha_sentido, letreiro, sentido_api, letreiro_destino,
             letreiro_origem, visto_primeiro_em, visto_ultimo_em)
        SELECT line.*, :observed_at, :observed_at
          FROM unnest(
                 CAST(:codes AS integer[]), CAST(:labels AS text[]),
                 CAST(:directions AS smallint[]), CAST(:destinations AS text[]),
                 CAST(:origins AS text[])
               ) AS line(code, label, direction, destination, origin)
         ORDER BY line.code
        ON CONFLICT (cod_linha_sentido) DO UPDATE SET
            letreiro = EXCLUDED.letreiro,
            sentido_api = EXCLUDED.sentido_api,
            letreiro_destino = EXCLUDED.letreiro_destino,
            letreiro_origem = EXCLUDED.letreiro_origem,
            visto_ultimo_em = EXCLUDED.visto_ultimo_em
        WHERE EXCLUDED.visto_ultimo_em >= known.visto_ultimo_em
          AND (
            EXCLUDED.visto_ultimo_em >= known.visto_ultimo_em + interval '1 hour'
            OR (known.letreiro, known.sentido_api,
                known.letreiro_destino, known.letreiro_origem)
               IS DISTINCT FROM
               (EXCLUDED.letreiro, EXCLUDED.sentido_api,
                EXCLUDED.letreiro_destino, EXCLUDED.letreiro_origem)
          )
        RETURNING cod_linha_sentido, letreiro, sentido_api,
                  letreiro_destino, letreiro_origem
    )
    SELECT written.cod_linha_sentido
      FROM written
      JOIN referencia.linhas_olhovivo AS previous USING (cod_linha_sentido)
     WHERE (previous.letreiro, previous.sentido_api,
            previous.letreiro_destino, previous.letreiro_origem)
           IS DISTINCT FROM
           (written.letreiro, written.sentido_api,
            written.letreiro_destino, written.letreiro_origem)
    """
)

INSERT_CYCLE = text(
    f"""
    INSERT INTO bruto.ciclos_coleta
        (id, fonte, iniciado_em, concluido_em, status, http_status, hora_api,
         n_recebidos, n_inseridos, n_rejeitados, versao_coletor, erro, arquivo_bruto,
         {", ".join(DURATION_COLUMNS.values())})
    VALUES
        (:id, :fonte, :iniciado_em, :concluido_em, :status, :http_status,
         :hora_api, :n_recebidos, :n_inseridos, :n_rejeitados, :versao_coletor,
         :erro, :arquivo_bruto,
         {", ".join(f":{column}" for column in DURATION_COLUMNS.values())})
    """
)


@dataclass
class PositionBatch:
    """Posições válidas de um ciclo, uma lista por coluna de ``posicoes_gps``."""

    horario_gps: list[datetime] = field(default_factory=list)
    prefixo: list[int] = field(default_factory=list)
    cod_linha_sentido: list[int] = field(default_factory=list)
    sentido_api: list[int] = field(default_factory=list)
    acessivel: list[bool | None] = field(default_factory=list)
    latitude: list[float] = field(default_factory=list)
    longitude: list[float] = field(default_factory=list)
    rejected: int = 0

    def __len__(self) -> int:
        return len(self.prefixo)


def build_batch(snapshot: PositionsSnapshot) -> PositionBatch:
    """Separa as posições que os CHECKs do bruto aceitam e conta as demais.

    Um CHECK violado derrubaria o lote inteiro, então o coletor descarta antes
    a posição com coordenada fora da faixa (ou não finita) ou sentido que não
    seja 1 ou 2.
    """
    batch = PositionBatch(rejected=snapshot.rejected_count)
    for line in snapshot.lines:
        for vehicle in line.vehicles:
            if not (
                line.direction in (1, 2)
                and -90 <= vehicle.latitude <= 90
                and -180 <= vehicle.longitude <= 180
            ):
                batch.rejected += 1
                continue
            reported_at = vehicle.reported_at
            if reported_at.tzinfo is None:  # "ta" vem em UTC
                reported_at = reported_at.replace(tzinfo=UTC)
            batch.horario_gps.append(reported_at)
            batch.prefixo.append(vehicle.prefix)
            batch.cod_linha_sentido.append(line.line_id)
            batch.sentido_api.append(line.direction)
            batch.acessivel.append(vehicle.accessible)
            batch.latitude.append(vehicle.latitude)
            batch.longitude.append(vehicle.longitude)
    return batch


def classify(result: CycleResult) -> str:
    """Valor de ``ciclos_coleta.status`` para o resultado do ciclo."""
    if result.status is CycleStatus.SUCCESS:
        return "ok"
    error = result.error
    if isinstance(error, InvalidTokenError | SessionExpiredError):
        return "erro_autenticacao"
    if isinstance(error, InvalidResponseError):
        return "resposta_invalida"
    if isinstance(error, OlhoVivoError):
        return "erro_http"
    # httpx.TransportError (rede fora, timeout de uma requisição) ou
    # CycleDeadlineError (prazo do ciclo estourado).
    return "erro_rede"


def http_status(result: CycleResult) -> int | None:
    """Código HTTP da última resposta do ciclo, quando houve resposta."""
    if result.status is CycleStatus.SUCCESS:
        return httpx.codes.OK
    if isinstance(result.error, OlhoVivoError):
        return result.error.status_code
    return None


@dataclass(frozen=True)
class RecordedCycle:
    """O que foi gravado: a linha do ciclo e as contagens das posições."""

    cycle_id: UUID
    status: str
    received: int | None
    inserted: int | None
    rejected: int | None
    stage_durations: dict[Stage, float]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _write_archive(path: Path, raw_body: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = NamedTemporaryFile(dir=path.parent, suffix=".tmp", delete=False)
    temporary_path = Path(temporary.name)
    try:
        with (
            temporary,
            gzip.GzipFile(
                filename="", fileobj=temporary, mode="wb", compresslevel=6, mtime=0
            ) as compressed,
        ):
            compressed.write(raw_body)
        temporary_path.replace(path)
    finally:
        with suppress(OSError):
            temporary_path.unlink(missing_ok=True)


class CycleRecorder:
    """Grava cada ciclo no bruto; use :meth:`record` como ``on_cycle``."""

    def __init__(
        self,
        engine: AsyncEngine,
        *,
        archive_dir: Path | None = None,
        collector_version: str = COLLECTOR_VERSION,
        clock: Callable[[], datetime] = _utc_now,
        stage_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._engine = engine
        self._archive_dir = archive_dir
        self._version = collector_version
        self._clock = clock
        self._stage_clock = stage_clock

    async def record(self, result: CycleResult) -> None:
        """Grava o ciclo; se o banco falhar, registra no log e segue."""
        try:
            recorded = await self.write(result)
        except (SQLAlchemyError, OSError, TimeoutError) as exc:
            logger.error(
                "Could not record collection cycle started at %s: %s: %s",
                result.started_at.isoformat(),
                type(exc).__name__,
                exc,
            )
            return
        logger.info(
            "Recorded cycle %s (%s): %s of %s position(s) inserted, %s rejected (%s)",
            recorded.cycle_id,
            recorded.status,
            recorded.inserted,
            recorded.received,
            recorded.rejected,
            format_durations(recorded.stage_durations),
        )

    async def write(self, result: CycleResult) -> RecordedCycle:
        """Grava o ciclo numa transação. Erros do banco sobem para quem chamou."""
        durations = dict(result.stage_durations)
        received: int | None = None
        inserted: int | None = None
        rejected: int | None = None
        changed_lines: list[int] = []
        async with self._engine.begin() as connection:
            cycle_id = (
                await connection.execute(text("SELECT public.uuid_v7()"))
            ).scalar_one()
            snapshot = result.snapshot
            raw_body = None
            if snapshot is not None:
                batch = build_batch(snapshot)
                timer = StageTimer(self._stage_clock)
                with timer.measure(Stage.WRITE):
                    changed_lines = await self._upsert_lines(
                        connection, snapshot, result.started_at
                    )
                    inserted = await self._insert_positions(connection, cycle_id, batch)
                durations.update(timer.durations())
                received = snapshot.vehicle_count
                rejected = batch.rejected
                raw_body = snapshot.raw_body
            elif isinstance(result.error, InvalidResponseError):
                raw_body = result.error.raw_body
            archive = await self._archive(cycle_id, result.started_at, raw_body)
            status = classify(result)
            await connection.execute(
                INSERT_CYCLE,
                {
                    "id": cycle_id,
                    "fonte": SOURCE,
                    "iniciado_em": result.started_at,
                    # Depois de gravar as posições: a duração do ciclo inclui a
                    # gravação. Nunca antes do fim da coleta.
                    "concluido_em": max(result.finished_at, self._clock()),
                    "status": status,
                    "http_status": http_status(result),
                    "hora_api": snapshot.reference_time
                    if snapshot is not None
                    else None,
                    "n_recebidos": received,
                    "n_inseridos": inserted,
                    "n_rejeitados": rejected,
                    "versao_coletor": self._version,
                    "erro": result.failure_cause if status != "ok" else None,
                    "arquivo_bruto": archive,
                }
                | {
                    column: durations.get(stage)
                    for stage, column in DURATION_COLUMNS.items()
                },
            )
        if changed_lines:
            logger.info(
                "Olho Vivo catalogue metadata changed for cl: %s", changed_lines
            )
        return RecordedCycle(
            cycle_id=cycle_id,
            status=status,
            received=received,
            inserted=inserted,
            rejected=rejected,
            stage_durations=durations,
        )

    async def _archive(
        self, cycle_id: UUID, started_at: datetime, raw_body: bytes | None
    ) -> str | None:
        if self._archive_dir is None or raw_body is None:
            return None
        day = started_at.astimezone(UTC)
        relative = Path(f"{day:%Y/%m/%d}/{cycle_id}.json.gz")
        try:
            await asyncio.to_thread(
                _write_archive, self._archive_dir / relative, raw_body
            )
        except OSError as exc:
            logger.warning("Could not archive Olho Vivo cycle %s: %s", cycle_id, exc)
            return None
        return relative.as_posix()

    @staticmethod
    async def _upsert_lines(
        connection: AsyncConnection,
        snapshot: PositionsSnapshot,
        observed_at: datetime,
    ) -> list[int]:
        lines: dict[int, LinePositions] = {
            line.line_id: line
            for line in snapshot.lines
            if line.direction in (1, 2) and line.code
        }
        if not lines:
            return []
        result = await connection.execute(
            UPSERT_LINES,
            {
                "observed_at": observed_at,
                "codes": list(lines),
                "labels": [line.code for line in lines.values()],
                "directions": [line.direction for line in lines.values()],
                "destinations": [line.destination for line in lines.values()],
                "origins": [line.origin for line in lines.values()],
            },
        )
        return list(result.scalars())

    @staticmethod
    async def _insert_positions(
        connection: AsyncConnection, cycle_id: UUID, batch: PositionBatch
    ) -> int:
        if not batch:
            return 0
        result = await connection.execute(
            INSERT_POSITIONS,
            {
                "ciclo_id": cycle_id,
                "horario_gps": batch.horario_gps,
                "prefixo": batch.prefixo,
                "cod_linha_sentido": batch.cod_linha_sentido,
                "sentido_api": batch.sentido_api,
                "acessivel": batch.acessivel,
                "latitude": batch.latitude,
                "longitude": batch.longitude,
            },
        )
        return result.rowcount
