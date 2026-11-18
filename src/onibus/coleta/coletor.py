"""Ciclo de coleta de posições com retry e alerta de falha longa.

Um ciclo faz até ``max_attempts`` tentativas de ``Posicao`` com espera
crescente entre elas. Sessão expirada conta como tentativa: na seguinte, o
cliente autentica de novo. Token recusado encerra o ciclo na hora.

O resultado de cada ciclo (:class:`CycleResult`) é devolvido em memória.
Gravar o ciclo no banco fica para o card T8 (tabela de ciclos de coleta) e o
agendamento a cada 30 s, para a H1.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

import httpx
from tenacity import (
    AsyncRetrying,
    RetryCallState,
    RetryError,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from tenacity.wait import wait_base

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
# Esperas de 1, 2, 4 e 8 s: as 5 tentativas cabem num ciclo de 30 s.
DEFAULT_WAIT = wait_exponential(multiplier=1, min=1, max=8)

RETRYABLE_ERRORS = (httpx.TransportError, SessionExpiredError, UnavailableError)


class CycleStatus(StrEnum):
    """Resultado de um ciclo de coleta."""

    SUCCESS = "success"
    FAILURE = "failure"


@dataclass(frozen=True)
class CycleResult:
    """O que aconteceu num ciclo; base do registro de lacunas (T8, H13)."""

    started_at: datetime
    finished_at: datetime
    status: CycleStatus
    attempts: int
    record_count: int
    failure_cause: str | None = None
    snapshot: PositionsSnapshot | None = None


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
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._client = client
        self._max_attempts = max_attempts
        self._wait = wait
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
        attempts = 0
        try:
            async for attempt in AsyncRetrying(
                stop=stop_after_attempt(self._max_attempts),
                wait=self._wait,
                retry=retry_if_exception_type(RETRYABLE_ERRORS),
                before_sleep=self._log_retry,
            ):
                with attempt:
                    attempts = attempt.retry_state.attempt_number
                    snapshot = await self._client.get_positions()
        except RetryError as exc:
            cause = exc.last_attempt.exception()
            return self._failure(started_at, attempts, cause)
        except OlhoVivoError as exc:
            return self._failure(started_at, attempts, exc)

        return self._success(started_at, attempts, snapshot)

    def _success(
        self, started_at: datetime, attempts: int, snapshot: PositionsSnapshot
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
        )

    def _failure(
        self, started_at: datetime, attempts: int, cause: BaseException | None
    ) -> CycleResult:
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
        )

    @staticmethod
    def _log_retry(retry_state: RetryCallState) -> None:
        outcome = retry_state.outcome
        error = outcome.exception() if outcome is not None else None
        logger.warning(
            "Olho Vivo attempt %d failed, retrying: %s",
            retry_state.attempt_number,
            _describe(error),
        )


def _describe(error: BaseException | None) -> str:
    if error is None:
        return "unknown error"
    return f"{type(error).__name__}: {error}"
