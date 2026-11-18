"""Testes de integração do banco (card T5): extensões, schemas e papéis.

Constroem a imagem de ``db/`` e sobem um PostgreSQL descartável com
testcontainers, que roda os mesmos scripts de ``db/init/`` do ``make up``.
Sem Docker rodando, os testes são pulados.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import ExecConfig
from testcontainers.core.docker_client import DockerClient
from testcontainers.core.image import DockerImage

DB_DIR = Path(__file__).resolve().parents[1] / "db"
DB_NAME = "onibus"
ADMIN = "onibus_admin"
WRITER = "onibus_escrita"
READER = "onibus_leitura"
PASSWORDS = {
    ADMIN: "senha-admin-teste",
    WRITER: "senha-escrita-teste",
    READER: "senha-leitura-teste",
}
LAYERS = ("bruto", "referencia", "operacao", "indicadores")


def run_sql(db: PostgresContainer, role: str, sql: str) -> tuple[int | None, str]:
    """Roda ``sql`` como ``role``, por TCP e com senha, como a aplicação fará."""
    result = db.exec(
        ExecConfig(
            command=["psql", "-h", "127.0.0.1", "-U", role, "-d", DB_NAME]
            + ["-v", "ON_ERROR_STOP=1", "-tAc", sql],
            environment={"PGPASSWORD": PASSWORDS[role]},
        )
    )
    return result.exit_code, result.output.decode()


@pytest.fixture(scope="module")
def db() -> Iterator[PostgresContainer]:
    """Banco novo com uma tabela ``teste`` em cada schema.

    As tabelas são criadas pelo administrador, como uma migração faria, para
    conferir que as permissões padrão valem para tabelas futuras.
    """
    try:
        DockerClient().client.ping()
    except Exception:
        pytest.skip("Docker indisponível: os testes do banco precisam dele rodando")

    image = DockerImage(path=DB_DIR, tag="onibus-db:test", clean_up=False).build()
    container = PostgresContainer(
        str(image),
        username=ADMIN,
        password=PASSWORDS[ADMIN],
        dbname=DB_NAME,
        driver=None,
    ).with_envs(
        DB_WRITER_PASSWORD=PASSWORDS[WRITER],
        DB_READER_PASSWORD=PASSWORDS[READER],
    )
    with container:
        tables = " ".join(
            f"CREATE TABLE {schema}.teste (id bigserial PRIMARY KEY, valor text);"
            for schema in LAYERS
        )
        exit_code, output = run_sql(container, ADMIN, tables)
        assert exit_code == 0, output
        yield container


def test_extensions_are_installed(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, ADMIN, "SELECT extname FROM pg_extension")

    assert exit_code == 0, output
    assert {"postgis", "pg_partman"} <= set(output.split())


def test_layer_schemas_exist(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, ADMIN, "SELECT nspname FROM pg_namespace")

    assert exit_code == 0, output
    assert set(LAYERS) <= set(output.split())


@pytest.mark.parametrize(
    ("role", "sql"),
    [
        (WRITER, "INSERT INTO bruto.teste (valor) VALUES ('x')"),
        (WRITER, "SELECT count(*) FROM bruto.teste"),
        (WRITER, "UPDATE operacao.teste SET valor = 'y'"),
        (WRITER, "DELETE FROM indicadores.teste"),
        (READER, "SELECT count(*) FROM bruto.teste"),
        (READER, "SELECT count(*) FROM referencia.teste"),
    ],
)
def test_roles_can_do_their_job(db: PostgresContainer, role: str, sql: str) -> None:
    exit_code, output = run_sql(db, role, sql)

    assert exit_code == 0, output


@pytest.mark.parametrize(
    ("role", "sql"),
    [
        (READER, "INSERT INTO bruto.teste (valor) VALUES ('x')"),
        (READER, "CREATE TABLE bruto.outra (id int)"),
        (READER, "UPDATE operacao.teste SET valor = 'y'"),
        (WRITER, "UPDATE bruto.teste SET valor = 'y'"),
        (WRITER, "DELETE FROM bruto.teste"),
        (WRITER, "TRUNCATE bruto.teste"),
        (WRITER, "CREATE TABLE operacao.outra (id int)"),
    ],
)
def test_roles_are_denied_outside_their_job(
    db: PostgresContainer, role: str, sql: str
) -> None:
    exit_code, output = run_sql(db, role, sql)

    assert exit_code != 0
    assert "permission denied" in output
