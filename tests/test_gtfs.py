"""Testes da carga do GTFS (card T8).

A parte pura (``read_feed``) roda sem banco. A carga (``load_gtfs``) sobe um
PostgreSQL descartável, na imagem do ``make up``, com as migrações aplicadas.
Sem Docker rodando, os testes do banco são pulados.
"""

import zipfile
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, date, datetime
from pathlib import Path

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

from onibus.coleta.gtfs import (
    AlreadyLoadedError,
    GtfsError,
    load_gtfs,
    parse_time,
    read_feed,
)

DB_NAME = "onibus"
PASSWORD = "senha-admin-teste"
CAPTURED = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)

FILES = {
    "agency.txt": """agency_id,agency_name,agency_url,agency_timezone,agency_lang
1,SPTRANS,http://www.sptrans.com.br,America/Sao_Paulo,pt
""",
    "routes.txt": """route_id,agency_id,route_short_name,route_long_name,route_type,route_color,route_text_color
701A-10,1,701A-10,Pq. Edu Chaves - Metrô Vl. Madalena,3,002F6C,FFFFFF
8000-10,1,8000-10,Term. A - Term. B,3,002F6C,FFFFFF
""",  # noqa: E501
    "stops.txt": """stop_id,stop_name,stop_desc,stop_lat,stop_lon
301790,R. Um,Esq. da Rua X,-23.5500,-46.6300
301764,R. Dois,,-23.5510,-46.6310
301724,R. Três,,-23.5520,-46.6320
""",
    # A forma 80407 vem fora de ordem: a sequência é que manda.
    "shapes.txt": """shape_id,shape_pt_lat,shape_pt_lon,shape_pt_sequence,shape_dist_traveled
80407,-23.5520,-46.6320,3,0.3
80407,-23.5500,-46.6300,1,0.0
80407,-23.5510,-46.6310,2,0.15
80408,-23.5520,-46.6320,1,0.0
80408,-23.5510,-46.6310,2,0.15
80408,-23.5500,-46.6300,3,0.3
90001,-23.5500,-46.6300,1,0.0
90001,-23.5510,-46.6310,2,0.15
""",  # noqa: E501
    "trips.txt": """route_id,service_id,trip_id,trip_headsign,direction_id,shape_id
701A-10,USD,701A-10-0,Metrô Vl. Madalena,0,80407
701A-10,USD,701A-10-1,Pq. Edu Chaves,1,80408
8000-10,USD,8000-10-0,Term. B,0,90001
""",
    # A base de cada viagem é qualquer (07:00, 14:00, 23:58): só as diferenças valem.
    # 701A-10-1 sai da parada 2 três segundos depois de chegar; 8000-10-0 passa da
    # meia-noite (24:05:30).
    "stop_times.txt": """trip_id,arrival_time,departure_time,stop_id,stop_sequence
701A-10-0,07:00:00,07:00:00,301790,1
701A-10-0,07:01:20,07:01:20,301764,2
701A-10-0,07:02:40,07:02:40,301724,3
701A-10-1,14:00:00,14:00:00,301724,1
701A-10-1,14:01:52,14:01:55,301764,2
8000-10-0,23:58:00,23:58:00,301790,1
8000-10-0,24:05:30,24:05:30,301764,2
""",
    "frequencies.txt": """trip_id,start_time,end_time,headway_secs
701A-10-0,04:00:00,04:59:00,1200
701A-10-0,05:00:00,05:59:00,900
701A-10-1,04:00:00,04:59:00,1800
8000-10-0,00:00:00,00:59:00,1200
""",
    "calendar.txt": """service_id,monday,tuesday,wednesday,thursday,friday,saturday,sunday,start_date,end_date
USD,1,1,1,1,1,1,1,20231001,20270601
_SD,0,0,0,0,0,1,1,20231001,20270601
""",  # noqa: E501
}


def make_gtfs(path: Path, **changes: str | None) -> Path:
    """Escreve um GTFS pequeno em ``path``. ``changes`` troca ou remove arquivos."""
    files = {**FILES, **changes}
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in files.items():
            if content is not None:
                archive.writestr(name, content)
    return path


def second_version(path: Path) -> Path:
    """Outra versão do feed: mesmas linhas e viagens, intervalo e calendário novos."""
    return make_gtfs(
        path,
        **{
            "frequencies.txt": FILES["frequencies.txt"].replace(",1200", ",600"),
            "calendar.txt": FILES["calendar.txt"].replace("20270601", "20271201"),
        },
    )


# --- Parte pura: lê, valida e normaliza, sem banco ---------------------------


@pytest.mark.parametrize(
    ("text", "seconds"),
    [
        ("00:00:00", 0),
        ("07:00:00", 25200),
        ("14:01:52", 50512),
        ("24:05:30", 86730),
        ("26:00:00", 93600),
        (" 07:00:00 ", 25200),
    ],
)
def test_parse_time(text: str, seconds: int) -> None:
    assert parse_time(text) == seconds


@pytest.mark.parametrize("text", ["", "7", "07:00", "07:60:00", "aa:bb:cc", "-1:00:00"])
def test_parse_time_rejects_invalid(text: str) -> None:
    with pytest.raises(GtfsError):
        parse_time(text)


def test_read_feed_normalizes_stop_times_to_the_first_stop(tmp_path: Path) -> None:
    feed = read_feed(make_gtfs(tmp_path / "gtfs.zip"))

    offsets = {
        (row.trip_id, row.sequence): (row.arrival_s, row.departure_s)
        for row in feed.stop_times
    }
    assert offsets[("701A-10-0", 1)] == (0, 0)
    assert offsets[("701A-10-0", 3)] == (160, 160)
    # Base 14:00:00; sai da parada 2 três segundos depois de chegar.
    assert offsets[("701A-10-1", 2)] == (112, 115)
    # Passa da meia-noite: 24:05:30 menos 23:58:00.
    assert offsets[("8000-10-0", 2)] == (450, 450)


def test_read_feed_counts_stop_times_where_departure_differs(tmp_path: Path) -> None:
    feed = read_feed(make_gtfs(tmp_path / "gtfs.zip"))

    assert feed.exceptions["departure_differs_from_arrival"] == 1
    assert feed.exceptions["offsets_not_increasing"] == 0


def test_read_feed_counts_trips_whose_offsets_go_back(tmp_path: Path) -> None:
    broken = FILES["stop_times.txt"].replace("07:02:40", "07:00:30")

    feed = read_feed(make_gtfs(tmp_path / "gtfs.zip", **{"stop_times.txt": broken}))

    assert feed.exceptions["offsets_not_increasing"] == 1


def test_read_feed_builds_shapes_in_sequence_order(tmp_path: Path) -> None:
    feed = read_feed(make_gtfs(tmp_path / "gtfs.zip"))

    shapes = {shape.shape_id: shape.wkt for shape in feed.shapes}
    assert shapes["80407"] == (
        "LINESTRING(-46.6300 -23.5500, -46.6310 -23.5510, -46.6320 -23.5520)"
    )


def test_read_feed_converts_frequencies_and_calendar(tmp_path: Path) -> None:
    feed = read_feed(make_gtfs(tmp_path / "gtfs.zip"))

    first = feed.frequencies[0]
    assert (first.trip_id, first.start_s, first.end_s, first.headway_s) == (
        "701A-10-0",
        4 * 3600,
        4 * 3600 + 59 * 60,
        1200,
    )
    weekdays = next(row for row in feed.calendar if row.service_id == "_SD")
    assert (weekdays.saturday, weekdays.sunday, weekdays.monday) == (True, True, False)
    assert weekdays.start_date == date(2023, 10, 1)
    assert weekdays.end_date == date(2027, 6, 1)


def test_hash_identifies_the_file_content(tmp_path: Path) -> None:
    first = read_feed(make_gtfs(tmp_path / "a.zip"))
    copy = read_feed(make_gtfs(tmp_path / "b.zip"))
    changed = read_feed(second_version(tmp_path / "c.zip"))

    assert first.sha256 == read_feed(tmp_path / "a.zip").sha256
    assert len(first.sha256) == 64
    assert changed.sha256 != first.sha256
    assert copy.source_name == "b.zip"


def test_read_feed_rejects_missing_files(tmp_path: Path) -> None:
    path = make_gtfs(tmp_path / "gtfs.zip", **{"frequencies.txt": None})

    with pytest.raises(GtfsError, match="frequencies.txt"):
        read_feed(path)


def test_read_feed_rejects_other_timezones(tmp_path: Path) -> None:
    agency = FILES["agency.txt"].replace("America/Sao_Paulo", "America/New_York")

    with pytest.raises(GtfsError, match="America/Sao_Paulo"):
        read_feed(make_gtfs(tmp_path / "gtfs.zip", **{"agency.txt": agency}))


def test_read_feed_rejects_a_shape_that_is_not_a_line(tmp_path: Path) -> None:
    shapes = FILES["shapes.txt"].replace(
        "90001,-23.5510,-46.6310,2,0.15", "90001,-23.5500,-46.6300,2,0.15"
    )

    with pytest.raises(GtfsError, match="90001"):
        read_feed(make_gtfs(tmp_path / "gtfs.zip", **{"shapes.txt": shapes}))


def test_read_feed_rejects_a_file_that_is_not_a_zip(tmp_path: Path) -> None:
    path = tmp_path / "nao.zip"
    path.write_text("isto não é um zip", encoding="utf-8")

    with pytest.raises(GtfsError, match="zip"):
        read_feed(path)


# --- Carga no banco ------------------------------------------------------------


@pytest.fixture(scope="module")
def db(
    db_image: str, alembic_config: Callable[[PostgresContainer, str], Config]
) -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        db_image, username="postgres", password=PASSWORD, dbname=DB_NAME, driver=None
    )
    with container:
        command.upgrade(alembic_config(container, DB_NAME), "head")
        yield container


@pytest_asyncio.fixture
async def conn(db: PostgresContainer) -> AsyncIterator[asyncpg.Connection]:
    """Conexão do dono do banco. Cada teste começa sem nenhuma versão do GTFS."""
    connection = await asyncpg.connect(
        host=db.get_container_host_ip(),
        port=int(db.get_exposed_port(5432)),
        user="postgres",
        password=PASSWORD,
        database=DB_NAME,
    )
    try:
        yield connection
    finally:
        await connection.execute("DELETE FROM referencia.gtfs_versoes")
        await connection.close()


async def count(conn: asyncpg.Connection, table: str, version_id: int) -> int:
    return int(
        await conn.fetchval(
            f"SELECT count(*) FROM referencia.{table} WHERE gtfs_versao_id = $1",
            version_id,
        )
    )


async def test_load_fills_every_table_linked_to_the_version(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    result = await load_gtfs(
        conn, make_gtfs(tmp_path / "gtfs.zip"), captured_at=CAPTURED
    )

    assert result.name == "20261119"
    assert result.counts == {
        "rotas": 2,
        "calendario": 2,
        "tracados": 3,
        "paradas": 3,
        "viagens": 3,
        "horarios_paradas": 7,
        "frequencias": 4,
    }
    version = await conn.fetchrow(
        "SELECT nome, hash_sha256, capturado_em, arquivo_origem"
        " FROM referencia.gtfs_versoes WHERE id = $1",
        result.version_id,
    )
    assert version is not None
    assert version["hash_sha256"] == read_feed(tmp_path / "gtfs.zip").sha256
    assert version["capturado_em"] == CAPTURED
    assert version["arquivo_origem"] == "gtfs.zip"
    for table, expected in (
        ("gtfs_rotas", 2),
        ("gtfs_calendario", 2),
        ("gtfs_tracados", 3),
        ("gtfs_paradas", 3),
        ("gtfs_viagens", 3),
        ("gtfs_horarios_paradas", 7),
        ("gtfs_frequencias", 4),
    ):
        assert await count(conn, table, result.version_id) == expected


async def test_load_stores_offsets_geometry_and_directions(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    result = await load_gtfs(conn, make_gtfs(tmp_path / "gtfs.zip"))

    offsets = await conn.fetch(
        "SELECT sequencia, chegada_s, partida_s FROM referencia.gtfs_horarios_paradas"
        " WHERE gtfs_versao_id = $1 AND trip_id = '701A-10-1' ORDER BY sequencia",
        result.version_id,
    )
    assert [tuple(row) for row in offsets] == [(1, 0, 0), (2, 112, 115)]
    shape = await conn.fetchrow(
        "SELECT ST_AsText(geom) AS wkt, ST_SRID(geom) AS srid, comprimento_m"
        " FROM referencia.gtfs_tracados"
        " WHERE gtfs_versao_id = $1 AND shape_id = '80407'",
        result.version_id,
    )
    assert shape is not None
    assert shape["srid"] == 4326
    assert shape["wkt"].startswith("LINESTRING(-46.63 -23.55,")
    # 0,002° de latitude (~222 m) e de longitude (~204 m em SP): diagonal de ~302 m.
    assert 295 < shape["comprimento_m"] < 310
    directions = await conn.fetch(
        "SELECT trip_id, sentido FROM referencia.gtfs_viagens"
        " WHERE gtfs_versao_id = $1 ORDER BY trip_id",
        result.version_id,
    )
    assert [tuple(row) for row in directions] == [
        ("701A-10-0", 0),
        ("701A-10-1", 1),
        ("8000-10-0", 0),
    ]
    stop = await conn.fetchrow(
        "SELECT ST_X(geom) AS lon, ST_Y(geom) AS lat FROM referencia.gtfs_paradas"
        " WHERE gtfs_versao_id = $1 AND stop_id = '301790'",
        result.version_id,
    )
    assert stop is not None
    assert (stop["lon"], stop["lat"]) == (-46.63, -23.55)


async def test_two_versions_of_the_feed_coexist(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    first = await load_gtfs(conn, make_gtfs(tmp_path / "a.zip"), captured_at=CAPTURED)
    second = await load_gtfs(
        conn, second_version(tmp_path / "b.zip"), captured_at=CAPTURED
    )

    assert first.version_id != second.version_id
    # Mesmas linhas, viagens e paradas nas duas versões, sem conflito de chave.
    for table, expected in (
        ("gtfs_rotas", 2),
        ("gtfs_viagens", 3),
        ("gtfs_paradas", 3),
    ):
        assert await count(conn, table, first.version_id) == expected
        assert await count(conn, table, second.version_id) == expected
    headway = (
        "SELECT intervalo_s FROM referencia.gtfs_frequencias WHERE gtfs_versao_id = $1"
        " AND trip_id = '701A-10-0' AND inicio_s = 14400"
    )
    assert await conn.fetchval(headway, first.version_id) == 1200
    assert await conn.fetchval(headway, second.version_id) == 600
    end_date = (
        "SELECT data_fim FROM referencia.gtfs_calendario"
        " WHERE gtfs_versao_id = $1 AND service_id = 'USD'"
    )
    assert await conn.fetchval(end_date, first.version_id) == date(2027, 6, 1)
    assert await conn.fetchval(end_date, second.version_id) == date(2027, 12, 1)


async def test_deleting_one_version_keeps_the_other(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    first = await load_gtfs(conn, make_gtfs(tmp_path / "a.zip"))
    second = await load_gtfs(conn, second_version(tmp_path / "b.zip"))

    await conn.execute(
        "DELETE FROM referencia.gtfs_versoes WHERE id = $1", first.version_id
    )

    assert await count(conn, "gtfs_horarios_paradas", first.version_id) == 0
    assert await count(conn, "gtfs_horarios_paradas", second.version_id) == 7


async def test_default_names_do_not_collide_on_the_same_day(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    first = await load_gtfs(conn, make_gtfs(tmp_path / "a.zip"), captured_at=CAPTURED)
    second = await load_gtfs(
        conn, second_version(tmp_path / "b.zip"), captured_at=CAPTURED
    )

    assert (first.name, second.name) == ("20261119", "20261119-2")


async def test_same_file_twice_is_rejected_and_changes_nothing(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    path = make_gtfs(tmp_path / "gtfs.zip")
    first = await load_gtfs(conn, path)

    with pytest.raises(AlreadyLoadedError) as error:
        await load_gtfs(conn, path)

    assert error.value.version_id == first.version_id
    assert await conn.fetchval("SELECT count(*) FROM referencia.gtfs_versoes") == 1
    assert await count(conn, "gtfs_horarios_paradas", first.version_id) == 7


async def test_explicit_name_already_taken_is_an_error(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    await load_gtfs(conn, make_gtfs(tmp_path / "a.zip"), name="feed-novembro")

    with pytest.raises(GtfsError, match="feed-novembro"):
        await load_gtfs(conn, second_version(tmp_path / "b.zip"), name="feed-novembro")

    assert await conn.fetchval("SELECT count(*) FROM referencia.gtfs_versoes") == 1


async def test_a_failure_in_the_middle_leaves_nothing_behind(
    conn: asyncpg.Connection, tmp_path: Path
) -> None:
    # stop_times aponta para uma parada que não existe: o banco recusa só no fim.
    broken = FILES["stop_times.txt"] + "8000-10-0,24:10:00,24:10:00,999999,3\n"
    path = make_gtfs(tmp_path / "gtfs.zip", **{"stop_times.txt": broken})

    with pytest.raises(GtfsError, match="999999|chave estrangeira|foreign key"):
        await load_gtfs(conn, path)

    assert await conn.fetchval("SELECT count(*) FROM referencia.gtfs_versoes") == 0
    for table in ("gtfs_rotas", "gtfs_paradas", "gtfs_tracados", "gtfs_viagens"):
        assert await conn.fetchval(f"SELECT count(*) FROM referencia.{table}") == 0
    # E a mesma carga, corrigida, funciona: o hash do arquivo ruim não ficou registrado.
    result = await load_gtfs(conn, make_gtfs(tmp_path / "ok.zip"))
    assert result.counts["horarios_paradas"] == 7
