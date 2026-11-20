"""Testes do particionamento de ``bruto.posicoes_gps`` (card T7).

Conferem o "Pronto quando" do card contra as migrações de verdade: a posição
cai na partição do mês certo, a repetida é rejeitada e uma consulta de 1 dia
lê só uma partição. A primeira partição é a de ``INICIO_PARTICOES``
(``conftest.py``), então os meses usados aqui sempre existem.

O ``bruto`` só aceita ``INSERT``: cada teste grava numa transação que é
desfeita no fim, e o banco continua vazio para o teste seguinte.
"""

import json
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

import asyncpg
import pytest
import pytest_asyncio
from alembic import command
from alembic.config import Config
from testcontainers.community.postgres import PostgresContainer

DB_NAME = "onibus"
PASSWORD = "senha-admin-teste"
CICLO_ID = "01a1f9f5-0000-7000-8000-000000000001"
SAO_PAULO = ZoneInfo("America/Sao_Paulo")
SETEMBRO = datetime(2026, 9, 15, 12, 0, tzinfo=UTC)

INSERT_CICLO = """
INSERT INTO bruto.ciclos_coleta (id, fonte, iniciado_em, concluido_em, status,
                                 versao_coletor)
VALUES ($1, 'olhovivo_posicao', now(), now(), 'ok', 'teste')
"""
INSERT_POSICAO = """
INSERT INTO bruto.posicoes_gps (horario_gps, prefixo, cod_linha_sentido,
                                sentido_api, latitude, longitude, ciclo_id)
VALUES ($1, $2, 1, 1, -23.55, -46.63, $3)
"""


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
    """Conexão dentro de uma transação desfeita no fim do teste."""
    connection = await asyncpg.connect(
        host=db.get_container_host_ip(),
        port=int(db.get_exposed_port(5432)),
        user="postgres",
        password=PASSWORD,
        database=DB_NAME,
    )
    transaction = connection.transaction()
    await transaction.start()
    try:
        await connection.execute(INSERT_CICLO, CICLO_ID)
        yield connection
    finally:
        await transaction.rollback()
        await connection.close()


@pytest.mark.parametrize(
    ("horario_gps", "particao"),
    [
        (SETEMBRO, "bruto.posicoes_gps_p20260901"),
        # Fronteira em UTC: 31/10 às 23:30 em São Paulo já é novembro.
        (
            datetime(2026, 10, 31, 23, 30, tzinfo=SAO_PAULO),
            "bruto.posicoes_gps_p20261101",
        ),
        (datetime(2026, 8, 1, tzinfo=UTC), "bruto.posicoes_gps_p20260801"),
        # Relógio do GPS errado: fora das faixas, vai para a default.
        (datetime(2040, 1, 1, tzinfo=UTC), "bruto.posicoes_gps_default"),
    ],
)
async def test_position_goes_to_the_partition_of_its_month(
    conn: asyncpg.Connection, horario_gps: datetime, particao: str
) -> None:
    await conn.execute(INSERT_POSICAO, horario_gps, 11001, CICLO_ID)

    stored = await conn.fetchval(
        "SELECT tableoid::regclass::text FROM bruto.posicoes_gps WHERE prefixo = $1",
        11001,
    )

    assert stored == particao


async def test_repeated_position_is_rejected(conn: asyncpg.Connection) -> None:
    await conn.execute(INSERT_POSICAO, SETEMBRO, 11001, CICLO_ID)

    with pytest.raises(asyncpg.UniqueViolationError):
        async with conn.transaction():  # savepoint: o erro desfaz só esta inserção
            await conn.execute(INSERT_POSICAO, SETEMBRO, 11001, CICLO_ID)

    assert await conn.fetchval("SELECT count(*) FROM bruto.posicoes_gps") == 1


async def test_repeated_position_is_ignored_with_on_conflict(
    conn: asyncpg.Connection,
) -> None:
    """Como o coletor grava: a repetida não entra e não derruba o lote."""
    await conn.execute(INSERT_POSICAO, SETEMBRO, 11001, CICLO_ID)

    status = await conn.execute(
        INSERT_POSICAO + " ON CONFLICT DO NOTHING",
        SETEMBRO,
        11001,
        CICLO_ID,
    )

    assert status == "INSERT 0 0"
    assert await conn.fetchval("SELECT count(*) FROM bruto.posicoes_gps") == 1


def _relations(plan: dict[str, Any]) -> set[str]:
    """As tabelas que o plano lê, em todos os nós."""
    found = {plan["Relation Name"]} if "Relation Name" in plan else set()
    for child in plan.get("Plans", []):
        found |= _relations(child)
    return found


@pytest.mark.parametrize(
    ("inicio", "fim"),
    [
        ("2026-09-15 00:00:00+00", "2026-09-16 00:00:00+00"),
        # O dia de São Paulo (UTC-3), como o app vai consultar.
        ("2026-09-15 00:00:00-03", "2026-09-16 00:00:00-03"),
    ],
)
async def test_one_day_query_reads_a_single_partition(
    conn: asyncpg.Connection, inicio: str, fim: str
) -> None:
    explain = await conn.fetchval(
        "EXPLAIN (FORMAT JSON) SELECT * FROM bruto.posicoes_gps"
        f" WHERE horario_gps >= '{inicio}' AND horario_gps < '{fim}'"
    )

    plan = json.loads(explain)[0]["Plan"]
    assert _relations(plan) == {"posicoes_gps_p20260901"}
