"""Carga do GTFS da SPTrans em ``referencia.gtfs_*``: ``python -m onibus.coleta.gtfs``.

Cada ``.zip`` vira uma versão (``referencia.gtfs_versoes``, identificada pelo
hash do arquivo), e todas as linhas dela levam o ``gtfs_versao_id``. Versões
diferentes convivem; o mesmo arquivo duas vezes não é carregado de novo.

O horário do ``stop_times.txt`` não é hora do relógio: é relativo ao início da
viagem, com uma base qualquer por viagem (ADR-001, P6). A carga guarda só o
deslocamento em segundos até a primeira parada, e não converte para UTC. Os
horários reais de partida vêm do ``frequencies.txt``.

A carga roda numa transação só: se algo falhar, nada fica gravado.
"""

import argparse
import asyncio
import csv
import hashlib
import io
import logging
import re
import sys
import zipfile
from collections import Counter, defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import asyncpg

from onibus.config import MigrationSettings, Settings

logger = logging.getLogger(__name__)

TIMEZONE = "America/Sao_Paulo"
REQUIRED_FILES = (
    "routes.txt",
    "stops.txt",
    "shapes.txt",
    "trips.txt",
    "stop_times.txt",
    "frequencies.txt",
    "calendar.txt",
)
_TIME = re.compile(r"^(\d+):([0-5]\d):([0-5]\d)$")


class GtfsError(Exception):
    """O arquivo não é um GTFS utilizável, ou o banco recusou a carga."""


class AlreadyLoadedError(GtfsError):
    """Um arquivo com o mesmo conteúdo (hash) já foi carregado."""

    def __init__(self, version_id: int, name: str) -> None:
        super().__init__(f"GTFS already loaded as version {name} (id {version_id})")
        self.version_id = version_id
        self.name = name


def parse_time(text: str) -> int:
    """``HH:MM:SS`` em segundos. A hora pode passar de 24 (``24:05:30``)."""
    match = _TIME.match(text.strip())
    if match is None:
        raise GtfsError(f"horário inválido no GTFS: {text!r}")
    hours, minutes, seconds = (int(part) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True)
class RouteRow:
    route_id: str
    agency_id: str | None
    short_name: str | None
    long_name: str | None
    route_type: int | None
    color: str | None
    text_color: str | None


@dataclass(frozen=True)
class CalendarRow:
    service_id: str
    monday: bool
    tuesday: bool
    wednesday: bool
    thursday: bool
    friday: bool
    saturday: bool
    sunday: bool
    start_date: date
    end_date: date


@dataclass(frozen=True)
class ShapeRow:
    shape_id: str
    wkt: str


@dataclass(frozen=True)
class StopRow:
    stop_id: str
    name: str | None
    description: str | None
    lat: float
    lon: float


@dataclass(frozen=True)
class TripRow:
    trip_id: str
    route_id: str
    service_id: str | None
    headsign: str | None
    direction: int
    shape_id: str | None


@dataclass(frozen=True)
class StopTimeRow:
    """Deslocamento, em segundos, até a primeira parada da viagem."""

    trip_id: str
    sequence: int
    stop_id: str
    arrival_s: int
    departure_s: int


@dataclass(frozen=True)
class FrequencyRow:
    trip_id: str
    start_s: int
    end_s: int
    headway_s: int


@dataclass
class Feed:
    """Um GTFS lido e normalizado, pronto para gravar."""

    sha256: str
    source_name: str
    routes: list[RouteRow] = field(default_factory=list)
    calendar: list[CalendarRow] = field(default_factory=list)
    shapes: list[ShapeRow] = field(default_factory=list)
    stops: list[StopRow] = field(default_factory=list)
    trips: list[TripRow] = field(default_factory=list)
    stop_times: list[StopTimeRow] = field(default_factory=list)
    frequencies: list[FrequencyRow] = field(default_factory=list)
    # Casos fora do padrão que a carga aceita mas registra (ADR-001, P6).
    exceptions: Counter[str] = field(default_factory=Counter)


@dataclass(frozen=True)
class LoadResult:
    version_id: int
    name: str
    counts: dict[str, int]
    exceptions: dict[str, int]


def _rows(archive: zipfile.ZipFile, name: str) -> Iterator[dict[str, str]]:
    with archive.open(name) as raw:
        reader = csv.DictReader(io.TextIOWrapper(raw, encoding="utf-8-sig"))
        for row in reader:
            yield {key: (value or "").strip() for key, value in row.items() if key}


def _text(value: str) -> str | None:
    return value or None


def _int(value: str, what: str) -> int:
    try:
        return int(value)
    except ValueError:
        raise GtfsError(f"{what} inválido no GTFS: {value!r}") from None


def _number(value: str, what: str) -> float:
    try:
        return float(value)
    except ValueError:
        raise GtfsError(f"{what} inválido no GTFS: {value!r}") from None


def _date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError:
        raise GtfsError(f"data inválida no GTFS: {value!r}") from None


def read_feed(path: Path | str) -> Feed:
    """Lê e valida o ``.zip`` e normaliza os horários. Não usa o banco."""
    path = Path(path)
    data = path.read_bytes()
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise GtfsError(f"{path.name} não é um arquivo zip") from None

    with archive:
        names = set(archive.namelist())
        missing = [name for name in REQUIRED_FILES if name not in names]
        if missing:
            raise GtfsError(f"faltam arquivos no GTFS: {', '.join(missing)}")

        feed = Feed(sha256=hashlib.sha256(data).hexdigest(), source_name=path.name)
        _check_timezone(archive, names)
        feed.routes = [
            RouteRow(
                route_id=row["route_id"],
                agency_id=_text(row.get("agency_id", "")),
                short_name=_text(row.get("route_short_name", "")),
                long_name=_text(row.get("route_long_name", "")),
                route_type=(
                    _int(row["route_type"], "route_type")
                    if row.get("route_type")
                    else None
                ),
                color=_text(row.get("route_color", "")),
                text_color=_text(row.get("route_text_color", "")),
            )
            for row in _rows(archive, "routes.txt")
        ]
        feed.calendar = [
            CalendarRow(
                service_id=row["service_id"],
                monday=row["monday"] == "1",
                tuesday=row["tuesday"] == "1",
                wednesday=row["wednesday"] == "1",
                thursday=row["thursday"] == "1",
                friday=row["friday"] == "1",
                saturday=row["saturday"] == "1",
                sunday=row["sunday"] == "1",
                start_date=_date(row["start_date"]),
                end_date=_date(row["end_date"]),
            )
            for row in _rows(archive, "calendar.txt")
        ]
        feed.stops = [
            StopRow(
                stop_id=row["stop_id"],
                name=_text(row.get("stop_name", "")),
                description=_text(row.get("stop_desc", "")),
                lat=_number(row["stop_lat"], "stop_lat"),
                lon=_number(row["stop_lon"], "stop_lon"),
            )
            for row in _rows(archive, "stops.txt")
        ]
        feed.shapes = _read_shapes(archive)
        feed.trips = [_trip(row) for row in _rows(archive, "trips.txt")]
        feed.frequencies = [
            FrequencyRow(
                trip_id=row["trip_id"],
                start_s=parse_time(row["start_time"]),
                end_s=parse_time(row["end_time"]),
                headway_s=_int(row["headway_secs"], "headway_secs"),
            )
            for row in _rows(archive, "frequencies.txt")
        ]
        feed.stop_times = _read_stop_times(archive, feed.exceptions)

    for reason, total in feed.exceptions.items():
        if total:
            logger.warning("GTFS %s: %s = %d", path.name, reason, total)
    return feed


def _check_timezone(archive: zipfile.ZipFile, names: set[str]) -> None:
    """O feed precisa estar no fuso de São Paulo: as janelas são em hora local."""
    if "agency.txt" not in names:
        return
    zones = {row.get("agency_timezone", "") for row in _rows(archive, "agency.txt")}
    zones.discard("")
    if zones - {TIMEZONE}:
        raise GtfsError(
            f"o fuso do GTFS deve ser {TIMEZONE}, mas o agency.txt traz: "
            f"{', '.join(sorted(zones))}"
        )


def _read_shapes(archive: zipfile.ZipFile) -> list[ShapeRow]:
    """Um traçado por ``shape_id``, com os pontos na ordem da sequência."""
    points: dict[str, list[tuple[int, str, str]]] = defaultdict(list)
    for row in _rows(archive, "shapes.txt"):
        _number(row["shape_pt_lat"], "shape_pt_lat")
        _number(row["shape_pt_lon"], "shape_pt_lon")
        sequence = _int(row["shape_pt_sequence"], "shape_pt_sequence")
        points[row["shape_id"]].append(
            (sequence, row["shape_pt_lon"], row["shape_pt_lat"])
        )

    shapes = []
    for shape_id, shape_points in points.items():
        shape_points.sort(key=lambda point: point[0])
        if len({(lon, lat) for _, lon, lat in shape_points}) < 2:
            raise GtfsError(
                f"o traçado {shape_id} não é uma linha: precisa de 2 pontos distintos"
            )
        wkt = ", ".join(f"{lon} {lat}" for _, lon, lat in shape_points)
        shapes.append(ShapeRow(shape_id=shape_id, wkt=f"LINESTRING({wkt})"))
    return shapes


def _trip(row: dict[str, str]) -> TripRow:
    direction = _int(row["direction_id"], "direction_id")
    if direction not in (0, 1):
        raise GtfsError(f"direction_id deve ser 0 ou 1, mas veio {direction}")
    return TripRow(
        trip_id=row["trip_id"],
        route_id=row["route_id"],
        service_id=_text(row.get("service_id", "")),
        headsign=_text(row.get("trip_headsign", "")),
        direction=direction,
        shape_id=_text(row.get("shape_id", "")),
    )


def _read_stop_times(
    archive: zipfile.ZipFile, exceptions: Counter[str]
) -> list[StopTimeRow]:
    """Normaliza cada viagem: horário menos o da primeira parada (P6)."""
    by_trip: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)
    for row in _rows(archive, "stop_times.txt"):
        if not row["arrival_time"] or not row["departure_time"]:
            raise GtfsError(
                f"stop_times sem horário na viagem {row['trip_id']}, "
                f"parada {row['stop_sequence']}"
            )
        by_trip[row["trip_id"]].append(
            (
                _int(row["stop_sequence"], "stop_sequence"),
                row["stop_id"],
                parse_time(row["arrival_time"]),
                parse_time(row["departure_time"]),
            )
        )

    stop_times = []
    for trip_id, stops in by_trip.items():
        stops.sort(key=lambda stop: stop[0])
        base = stops[0][2]
        previous = -1
        going_back = False
        for sequence, stop_id, arrival, departure in stops:
            arrival_s, departure_s = arrival - base, departure - base
            if departure != arrival:
                exceptions["departure_differs_from_arrival"] += 1
            going_back = going_back or arrival_s < previous
            previous = arrival_s
            stop_times.append(
                StopTimeRow(trip_id, sequence, stop_id, arrival_s, departure_s)
            )
        if going_back:
            exceptions["offsets_not_increasing"] += 1
    exceptions.setdefault("offsets_not_increasing", 0)
    return stop_times


async def _free_name(
    conn: asyncpg.Connection, name: str | None, captured_at: datetime
) -> str:
    """O nome dado, se livre, ou ``AAAAMMDD`` (com ``-2``, ``-3``... se repetir).

    A data é a da captura em São Paulo: em UTC, uma captura depois das 21h
    levaria o nome do dia seguinte.
    """
    if name is not None:
        taken = await conn.fetchval(
            "SELECT 1 FROM referencia.gtfs_versoes WHERE nome = $1", name
        )
        if taken:
            raise GtfsError(f"já existe uma versão do GTFS chamada {name}")
        return name

    base = captured_at.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y%m%d")
    rows = await conn.fetch(
        "SELECT nome FROM referencia.gtfs_versoes WHERE nome = $1 OR nome LIKE $2",
        base,
        f"{base}-%",
    )
    existing = {row["nome"] for row in rows}
    candidate, number = base, 2
    while candidate in existing:
        candidate = f"{base}-{number}"
        number += 1
    return candidate


async def store_feed(
    conn: asyncpg.Connection,
    feed: Feed,
    *,
    name: str | None = None,
    captured_at: datetime | None = None,
) -> LoadResult:
    """Grava o feed como uma versão nova, numa transação só."""
    captured_at = captured_at or datetime.now(UTC)
    try:
        async with conn.transaction():
            existing = await conn.fetchrow(
                "SELECT id, nome FROM referencia.gtfs_versoes WHERE hash_sha256 = $1",
                feed.sha256,
            )
            if existing is not None:
                raise AlreadyLoadedError(existing["id"], existing["nome"])

            version_name = await _free_name(conn, name, captured_at)
            version_id = await conn.fetchval(
                "INSERT INTO referencia.gtfs_versoes"
                " (nome, hash_sha256, capturado_em, arquivo_origem)"
                " VALUES ($1, $2, $3, $4) RETURNING id",
                version_name,
                feed.sha256,
                captured_at,
                feed.source_name,
            )
            counts = await _copy_feed(conn, version_id, feed)
    except asyncpg.PostgresError as error:
        detail = f" ({error.detail})" if getattr(error, "detail", None) else ""
        raise GtfsError(f"o banco recusou a carga do GTFS: {error}{detail}") from error

    logger.info(
        "GTFS %s loaded as version %s (id %d)",
        feed.source_name,
        version_name,
        version_id,
    )
    return LoadResult(version_id, version_name, counts, dict(feed.exceptions))


async def _copy_feed(
    conn: asyncpg.Connection, version_id: int, feed: Feed
) -> dict[str, int]:
    async def copy(
        table: str, columns: tuple[str, ...], records: list[tuple[object, ...]]
    ) -> int:
        await conn.copy_records_to_table(
            table, records=records, columns=columns, schema_name="referencia"
        )
        return len(records)

    counts: dict[str, int] = {}
    counts["rotas"] = await copy(
        "gtfs_rotas",
        (
            "gtfs_versao_id",
            "route_id",
            "agency_id",
            "nome_curto",
            "nome_longo",
            "tipo_rota",
            "cor",
            "cor_texto",
        ),
        [
            (
                version_id,
                r.route_id,
                r.agency_id,
                r.short_name,
                r.long_name,
                r.route_type,
                r.color,
                r.text_color,
            )
            for r in feed.routes
        ],
    )
    counts["calendario"] = await copy(
        "gtfs_calendario",
        (
            "gtfs_versao_id",
            "service_id",
            "segunda",
            "terca",
            "quarta",
            "quinta",
            "sexta",
            "sabado",
            "domingo",
            "data_inicio",
            "data_fim",
        ),
        [
            (
                version_id,
                c.service_id,
                c.monday,
                c.tuesday,
                c.wednesday,
                c.thursday,
                c.friday,
                c.saturday,
                c.sunday,
                c.start_date,
                c.end_date,
            )
            for c in feed.calendar
        ],
    )

    # Geometrias: o texto (WKT) vai para uma tabela temporária e o PostGIS converte.
    await conn.execute(
        "CREATE TEMP TABLE _tracados (shape_id text, wkt text) ON COMMIT DROP"
    )
    await conn.copy_records_to_table(
        "_tracados",
        records=[(s.shape_id, s.wkt) for s in feed.shapes],
        columns=("shape_id", "wkt"),
    )
    await conn.execute(
        "INSERT INTO referencia.gtfs_tracados (gtfs_versao_id, shape_id, geom)"
        " SELECT $1, shape_id, ST_GeomFromText(wkt, 4326) FROM _tracados",
        version_id,
    )
    counts["tracados"] = len(feed.shapes)

    await conn.execute(
        "CREATE TEMP TABLE _paradas (stop_id text, nome text, descricao text,"
        " lat double precision, lon double precision) ON COMMIT DROP"
    )
    await conn.copy_records_to_table(
        "_paradas",
        records=[(s.stop_id, s.name, s.description, s.lat, s.lon) for s in feed.stops],
        columns=("stop_id", "nome", "descricao", "lat", "lon"),
    )
    await conn.execute(
        "INSERT INTO referencia.gtfs_paradas"
        " (gtfs_versao_id, stop_id, nome, descricao, geom)"
        " SELECT $1, stop_id, nome, descricao,"
        " ST_SetSRID(ST_MakePoint(lon, lat), 4326) FROM _paradas",
        version_id,
    )
    counts["paradas"] = len(feed.stops)

    counts["viagens"] = await copy(
        "gtfs_viagens",
        (
            "gtfs_versao_id",
            "trip_id",
            "route_id",
            "service_id",
            "letreiro",
            "sentido",
            "shape_id",
        ),
        [
            (
                version_id,
                t.trip_id,
                t.route_id,
                t.service_id,
                t.headsign,
                t.direction,
                t.shape_id,
            )
            for t in feed.trips
        ],
    )
    counts["horarios_paradas"] = await copy(
        "gtfs_horarios_paradas",
        ("gtfs_versao_id", "trip_id", "sequencia", "stop_id", "chegada_s", "partida_s"),
        [
            (version_id, s.trip_id, s.sequence, s.stop_id, s.arrival_s, s.departure_s)
            for s in feed.stop_times
        ],
    )
    counts["frequencias"] = await copy(
        "gtfs_frequencias",
        ("gtfs_versao_id", "trip_id", "inicio_s", "fim_s", "intervalo_s"),
        [
            (version_id, f.trip_id, f.start_s, f.end_s, f.headway_s)
            for f in feed.frequencies
        ],
    )
    return counts


async def load_gtfs(
    conn: asyncpg.Connection,
    path: Path | str,
    *,
    name: str | None = None,
    captured_at: datetime | None = None,
) -> LoadResult:
    """Lê o ``.zip`` e grava como uma versão nova do GTFS."""
    feed = await asyncio.to_thread(read_feed, path)
    return await store_feed(conn, feed, name=name, captured_at=captured_at)


async def connect(settings: MigrationSettings) -> asyncpg.Connection:
    """Conexão do dono do banco, com os dados do ``.env`` (como as migrações)."""
    url = settings.database_url()
    return await asyncpg.connect(
        host=url.host,
        port=url.port,
        user=url.username,
        password=url.password,
        database=url.database,
    )


def _datetime(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"data inválida: {text!r}") from None
    return value if value.tzinfo else value.replace(tzinfo=UTC)


async def _run(args: argparse.Namespace) -> int:
    conn = await connect(MigrationSettings())
    try:
        result = await load_gtfs(
            conn, args.file, name=args.name, captured_at=args.captured_at
        )
    except AlreadyLoadedError as error:
        print(
            f"Nada a fazer: este arquivo já está carregado como a versão "
            f"{error.name} (id {error.version_id})."
        )
        return 0
    except GtfsError as error:
        print(f"Falhou: {error}", file=sys.stderr)
        return 1
    finally:
        await conn.close()

    summary = ", ".join(f"{total} {table}" for table, total in result.counts.items())
    print(
        f"GTFS carregado como a versão {result.name} "
        f"(id {result.version_id}): {summary}"
    )
    for reason, total in result.exceptions.items():
        if total:
            print(f"Atenção: {reason} = {total}", file=sys.stderr)
    return 0


def main() -> None:
    """Carrega um GTFS no banco do ``.env``."""
    parser = argparse.ArgumentParser(prog="python -m onibus.coleta.gtfs")
    parser.add_argument("file", type=Path, help="o .zip do GTFS da SPTrans")
    parser.add_argument("--name", help="nome da versão (padrão: AAAAMMDD da captura)")
    parser.add_argument(
        "--captured-at",
        type=_datetime,
        help="quando o arquivo foi capturado, em ISO 8601 (padrão: agora)",
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=Settings().log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        sys.exit(asyncio.run(_run(args)))
    except (ValueError, OSError) as error:
        print(f"Falhou: {error}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
