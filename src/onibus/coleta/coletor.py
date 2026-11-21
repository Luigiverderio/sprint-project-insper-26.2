"""Ciclo de coleta de posições com retry e alerta de falha longa.

Um ciclo faz até ``max_attempts`` tentativas de ``Posicao`` com espera
crescente entre elas. Sessão expirada conta como tentativa: na seguinte, o
cliente autentica de novo. Token recusado encerra o ciclo na hora.

O ciclo inteiro tem um prazo (``deadline``, 25 s por padrão), menor que o
intervalo de 30 s do agendador. Ao estourar, a requisição em andamento é
cancelada e o ciclo termina como falha; nenhuma tentativa nova começa se a
espera antes dela já passaria do prazo. Assim um ciclo nunca invade o
seguinte, e a lacuna fica registrada como falha em vez de virar um ciclo pulado.

O resultado de cada ciclo (:class:`CycleResult`) é devolvido em memória, com
a duração de cada etapa (:mod:`onibus.coleta.etapas`) e o erro que encerrou o
ciclo. :mod:`onibus.coleta.gravacao` grava o ciclo e as posições no banco.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    stop_before_delay,
    wait_exponential,
)
from tenacity.wait import wait_base

from onibus.coleta.etapas import Stage, StageTimer
from onibus.coleta.olho_vivo import (
    OlhoVivoClient,
    OlhoVivoError,
    PositionsSnapshot,
    SessionExpiredError,
    UnavailableError,
)

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
FAILURE_ALERT_AFTER = timedelta(minutes=10)
# Esperas de 1, 2, 4 e 8 s entre as tentativas.
DEFAULT_WAIT = wait_exponential(multiplier=1, min=1, max=8)
# Com timeout de 10 s por requisição, 5 tentativas mais as esperas podem passar
# de 65 s. O prazo corta o ciclo antes do próximo disparo (a cada 30 s).
CYCLE_DEADLINE = timedelta(seconds=25)

RETRYABLE_ERRORS = (httpx.TransportError, SessionExpiredError, UnavailableError)


class CycleStatus(StrEnum):
    """Resultado de um ciclo de coleta."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class CycleResult:
    """O que aconteceu num ciclo; base do registro de lacunas (T8, H13).

    ``error`` é a exceção que encerrou um ciclo com falha, e ``failure_cause``,
    a descrição dela para o log e para a coluna ``erro``.
    ``raw_body`` guarda a última resposta de ``/Posicao`` em ciclos com falha.
    """

    started_at: datetime
    finished_at: datetime
    status: CycleStatus
    attempts: int
    record_count: int
    failure_cause: str | None = None
    error: BaseException | None = None
    snapshot: PositionsSnapshot | None = None
    stage_durations: dict[Stage, float] = field(default_factory=dict)
    raw_body: bytes | None = field(default=None, repr=False)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PositionCollector:
    """Roda ciclos de coleta e acompanha há quanto tempo a coleta falha."""

    def __init__(
        self,
        client: OlhoVivoClient,
        *,
        max_attempts: int = MAX_ATTEMPTS,
        wait: wait_base = DEFAULT_WAIT,
        alert_after: timedelta = FAILURE_ALERT_AFTER,
        deadline: timedelta = CYCLE_DEADLINE,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._max_attempts = max_attempts
        self._wait = wait
        self._deadline = deadline
        self._alert_after = alert_after
        self._clock = clock
        self._failing_since: datetime | None = None
        self._alert_sent = False

    @property
    def failing_since(self) -> datetime | None:
        """Início da sequência atual de ciclos com falha, ou ``None``."""
        return self._failing_since

    async def run_cycle(self) -> CycleResult:
        """Executa um ciclo de coleta. Nunca levanta exceção da API."""
        started_at = self._clock()
        deadline = self._deadline.total_seconds()
        timer = StageTimer()
        attempts = 0
        last_error: BaseException | None = None
        last_response_body: bytes | None = None

        async def sleep(seconds: float) -> None:
            with timer.measure(Stage.RETRY_WAIT):
                await asyncio.sleep(seconds)

        def log_retry(retry_state: RetryCallState) -> None:
            nonlocal last_error, last_response_body
            last_error = self._log_retry(retry_state)
            if (
                isinstance(last_error, OlhoVivoError)
                and last_error.raw_body is not None
            ):
                last_response_body = last_error.raw_body

        try:
            async with asyncio.timeout(deadline):
                async for attempt in AsyncRetrying(
                    stop=(
                        stop_after_attempt(self._max_attempts)
                        | stop_before_delay(deadline)
                    ),
                    wait=self._wait,
                    sleep=sleep,
                    retry=retry_if_exception_type(RETRYABLE_ERRORS),
                    before_sleep=log_retry,
                ):
                    with attempt:
                        attempts = attempt.retry_state.attempt_number
                        snapshot = await self._client.get_positions(timer)
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            return self._failure(started_at, attempts, timer, cause, last_response_body)
        except OlhoVivoError as exc:
            return self._failure(started_at, attempts, timer, exc, last_response_body)
        except TimeoutError:
            timeout = CycleDeadlineError(
                f"cycle exceeded {deadline:g} s during attempt {attempts}"
            )
            timeout.__cause__ = last_error
            return self._failure(
                started_at, attempts, timer, timeout, last_response_body
            )

        return self._success(started_at, attempts, timer, snapshot)

    def _success(
        self,
        started_at: datetime,
        attempts: int,
        timer: StageTimer,
        snapshot: PositionsSnapshot,
    ) -> CycleResult:
        if self._failing_since is not None:
            logger.info(
                "Olho Vivo collection recovered after failing since %s",
                self._failing_since.isoformat(),
            )
        self._failing_since = None
        self._alert_sent = False
        return CycleResult(
            started_at=started_at,
            finished_at=self._clock(),
            status=CycleStatus.SUCCESS,
            attempts=attempts,
            record_count=snapshot.vehicle_count,
            snapshot=snapshot,
            stage_durations=timer.durations(),
        )

    def _failure(
        self,
        started_at: datetime,
        attempts: int,
        timer: StageTimer,
        cause: BaseException | None,
        raw_body: bytes | None,
    ) -> CycleResult:
        if isinstance(cause, OlhoVivoError) and cause.raw_body is not None:
            raw_body = cause.raw_body
        description = _describe(cause)
        logger.error(
            "Olho Vivo collection cycle failed after %d attempt(s): %s",
            attempts,
            description,
        )
        if self._failing_since is None:
            self._failing_since = started_at
        finished_at = self._clock()
        failing_for = finished_at - self._failing_since
        if failing_for >= self._alert_after and not self._alert_sent:
            logger.critical(
                "Olho Vivo collection failing for %d min (since %s): %s",
                failing_for // timedelta(minutes=1),
                self._failing_since.isoformat(),
                description,
            )
            self._alert_sent = True
        return CycleResult(
            started_at=started_at,
            finished_at=finished_at,
            status=CycleStatus.FAILURE,
            attempts=attempts,
            record_count=0,
            failure_cause=description,
            error=cause,
            stage_durations=timer.durations(),
            raw_body=raw_body,
        )

    @staticmethod
    def _log_retry(retry_state: RetryCallState) -> BaseException | None:
        outcome = retry_state.outcome
        error = outcome.exception() if outcome is not None else None
        logger.warning(
            "Olho Vivo attempt %d failed, retrying: %s",
            retry_state.attempt_number,
            _describe(error),
        )
        return error


class CycleDeadlineError(Exception):
    """O ciclo passou do prazo; ``__cause__`` guarda o último erro da API."""


def _describe(error: BaseException | None) -> str:
    if error is None:
        return "unknown error"
    description = f"{type(error).__name__}: {error}"
    if isinstance(error, CycleDeadlineError) and error.__cause__ is not None:
        description += f" (last error: {_describe(error.__cause__)})"
    return description
