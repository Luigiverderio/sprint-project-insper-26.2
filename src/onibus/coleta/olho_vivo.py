"""Cliente da API Olho Vivo (SPTrans), versão 2.1.

A API autentica por cookie: ``POST /Login/Autenticar?token=...`` devolve
``true`` e grava o cookie de sessão, que o ``httpx.AsyncClient`` reenvia
sozinho. Quando a sessão expira, as consultas respondem 401.

Cada método faz uma única tentativa. O retry fica no coletor
(:mod:`onibus.coleta.coletor`), que conta as tentativas por ciclo.
"""

from datetime import datetime
from types import TracebackType
from typing import Self

import httpx
from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASE_URL = "https://api.olhovivo.sptrans.com.br/v2.1"
DEFAULT_TIMEOUT = httpx.Timeout(10.0)


class OlhoVivoError(Exception):
    """Falha ao conversar com a API Olho Vivo."""


class InvalidTokenError(OlhoVivoError):
    """O token foi recusado no login. Tentar de novo não resolve."""


class SessionExpiredError(OlhoVivoError):
    """A API respondeu 401: a sessão expirou e é preciso autenticar de novo."""


class UnavailableError(OlhoVivoError):
    """A API respondeu com erro do servidor (5xx) ou uma resposta inválida."""


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

    async def authenticate(self) -> None:
        """Abre a sessão com o token. Recusa do token gera ``InvalidTokenError``."""
        response = await self._http.post(
            f"{self._base_url}/Login/Autenticar", params={"token": self._token}
        )
        _raise_for_server_error(response)
        try:
            accepted = response.json() is True
        except ValueError as exc:
            raise UnavailableError("Invalid response from /Login/Autenticar") from exc
        if response.status_code != httpx.codes.OK or not accepted:
            self._authenticated = False
            raise InvalidTokenError("Olho Vivo rejected the token")
        self._authenticated = True

    async def get_positions(self) -> PositionsSnapshot:
        """Consulta ``Posicao``: posição de todos os veículos em operação."""
        response = await self._get("/Posicao")
        try:
            return PositionsSnapshot.model_validate(response.json())
        except ValueError as exc:
            raise UnavailableError("Invalid response from /Posicao") from exc

    async def _get(self, path: str) -> httpx.Response:
        if not self._authenticated:
            await self.authenticate()
        response = await self._http.get(f"{self._base_url}{path}")
        if response.status_code == httpx.codes.UNAUTHORIZED:
            self._authenticated = False
            raise SessionExpiredError(f"Session expired on {path}")
        _raise_for_server_error(response)
        if response.status_code != httpx.codes.OK:
            raise OlhoVivoError(f"Unexpected status {response.status_code} on {path}")
        return response


def _raise_for_server_error(response: httpx.Response) -> None:
    if response.is_server_error:
        raise UnavailableError(
            f"Olho Vivo returned {response.status_code} on {response.url.path}"
        )
