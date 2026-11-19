"""Cliente da API Olho Vivo (SPTrans), versão 2.1.

A API autentica por cookie: ``POST /Login/Autenticar?token=...`` devolve
``true`` e grava o cookie de sessão, que o ``httpx.AsyncClient`` reenvia
sozinho. Quando a sessão expira, as consultas respondem 401.

Cada método faz uma única tentativa. O retry fica no coletor
(:mod:`onibus.coleta.coletor`), que conta as tentativas por ciclo. Com um
:class:`StageTimer`, os métodos medem login, chamada e leitura da resposta.
"""

import logging
import re
from datetime import datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

from onibus.coleta.etapas import Stage, StageTimer

DEFAULT_BASE_URL = "https://api.olhovivo.sptrans.com.br/v2.1"
DEFAULT_TIMEOUT = httpx.Timeout(10.0)

_TOKEN_IN_URL = re.compile(r"(token=)[^&\s\"']+")


class RedactTokenFilter(logging.Filter):
    """Troca o token por ``***`` nos logs do httpx.

    O login exige o token na query string, e o httpx registra a URL de toda
    requisição em nível INFO. Sem este filtro o token iria para os logs.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _TOKEN_IN_URL.sub(r"\1***", message)
        if redacted != message:
            record.msg = redacted
            record.args = None
        return True


_httpx_logger = logging.getLogger("httpx")
if not any(isinstance(f, RedactTokenFilter) for f in _httpx_logger.filters):
    _httpx_logger.addFilter(RedactTokenFilter())


class OlhoVivoError(Exception):
    """Falha ao conversar com a API Olho Vivo.

    ``status_code`` é o código HTTP da resposta que gerou o erro, quando houve
    resposta; vai para ``bruto.ciclos_coleta.http_status``.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class InvalidTokenError(OlhoVivoError):
    """O token foi recusado no login. Tentar de novo não resolve."""


class SessionExpiredError(OlhoVivoError):
    """A API respondeu 401: a sessão expirou e é preciso autenticar de novo."""


class UnavailableError(OlhoVivoError):
    """A API respondeu com erro do servidor (5xx) ou uma resposta inválida."""


class InvalidResponseError(UnavailableError):
    """A resposta não é o JSON esperado. Tenta de novo, como um 5xx."""


class _ApiModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)


class VehiclePosition(_ApiModel):
    """Um veículo em ``Posicao``: campo ``vs`` de cada linha."""

    prefix: int = Field(alias="p")
    accessible: bool = Field(alias="a")
    reported_at: datetime = Field(alias="ta")
    latitude: float = Field(alias="py")
    longitude: float = Field(alias="px")


class LinePositions(_ApiModel):
    """Uma linha em ``Posicao`` com os veículos localizados."""

    code: str = Field(alias="c")
    line_id: int = Field(alias="cl")
    direction: int = Field(alias="sl")
    destination: str = Field(alias="lt0")
    origin: str = Field(alias="lt1")
    vehicle_count: int = Field(alias="qv")
    vehicles: list[VehiclePosition] = Field(alias="vs")


class PositionsSnapshot(_ApiModel):
    """Resposta de ``Posicao``: todas as linhas no horário de referência."""

    reference_time: str = Field(alias="hr")
    lines: list[LinePositions] = Field(alias="l")

    @property
    def vehicle_count(self) -> int:
        """Total de veículos em todas as linhas."""
        return sum(len(line.vehicles) for line in self.lines)


class OlhoVivoClient:
    """Cliente assíncrono da API Olho Vivo.

    Use como gerenciador de contexto para fechar a conexão no fim. Um
    ``http_client`` pode ser injetado (por exemplo, nos testes).
    """

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        self._token = token
        self._http = http_client or httpx.AsyncClient(timeout=DEFAULT_TIMEOUT)
        self._base_url = base_url.rstrip("/")
        self._authenticated = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Fecha a conexão HTTP."""
        await self._http.aclose()

    @property
    def authenticated(self) -> bool:
        """Se há uma sessão aberta (até a API responder 401)."""
        return self._authenticated

    async def authenticate(self, timer: StageTimer | None = None) -> None:
        """Abre a sessão com o token. Recusa do token gera ``InvalidTokenError``."""
        timer = timer or StageTimer()
        with timer.measure(Stage.LOGIN):
            response = await self._http.post(
                f"{self._base_url}/Login/Autenticar", params={"token": self._token}
            )
        _raise_for_server_error(response)
        try:
            accepted = response.json() is True
        except ValueError as exc:
            raise InvalidResponseError(
                "Invalid response from /Login/Autenticar",
                status_code=response.status_code,
            ) from exc
        if response.status_code != httpx.codes.OK or not accepted:
            self._authenticated = False
            raise InvalidTokenError(
                "Olho Vivo rejected the token", status_code=response.status_code
            )
        self._authenticated = True

    async def get_positions(self, timer: StageTimer | None = None) -> PositionsSnapshot:
        """Consulta ``Posicao``: posição de todos os veículos em operação."""
        timer = timer or StageTimer()
        response = await self._get("/Posicao", timer)
        with timer.measure(Stage.PARSE):
            try:
                return PositionsSnapshot.model_validate(response.json())
            except ValueError as exc:
                raise InvalidResponseError(
                    "Invalid response from /Posicao", status_code=response.status_code
                ) from exc

    async def _get(self, path: str, timer: StageTimer) -> httpx.Response:
        if not self._authenticated:
            await self.authenticate(timer)
        with timer.measure(Stage.REQUEST):
            response = await self._http.get(f"{self._base_url}{path}")
        if response.status_code == httpx.codes.UNAUTHORIZED:
            self._authenticated = False
            raise SessionExpiredError(
                f"Session expired on {path}", status_code=response.status_code
            )
        _raise_for_server_error(response)
        if response.status_code != httpx.codes.OK:
            raise OlhoVivoError(
                f"Unexpected status {response.status_code} on {path}",
                status_code=response.status_code,
            )
        return response


def _raise_for_server_error(response: httpx.Response) -> None:
    if response.is_server_error:
        raise UnavailableError(
            f"Olho Vivo returned {response.status_code} on {response.url.path}",
            status_code=response.status_code,
        )
