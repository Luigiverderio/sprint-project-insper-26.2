"""Testes do ``make seed`` (``onibus.db.seeds``).

O teste de integração sobe um banco descartável com testcontainers, na imagem
do ``make up``. Sem Docker rodando, ele é pulado.
"""

from collections.abc import Iterator
from pathlib import Path

import pytest
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import ExecConfig

from onibus.db.seeds import main, seed_files

DB_NAME = "onibus"
PASSWORD = "senha-seed-teste"


def write(directory: Path, name: str, sql: str) -> Path:
    path = directory / name
    path.write_text(sql, encoding="utf-8")
    return path


def test_seed_files_are_the_sql_files_in_name_order(tmp_path: Path) -> None:
    second = write(tmp_path, "0002_b.sql", "SELECT 2;")
    first = write(tmp_path, "0001_a.sql", "SELECT 1;")
    write(tmp_path, "README.md", "# não é seed")

    assert seed_files(tmp_path) == [first, second]


def test_main_without_seed_files_does_nothing(tmp_path: Path) -> None:
    # Sem arquivos, nem conecta: não há banco nem senha neste teste.
    assert main(tmp_path) == 0


def test_main_refuses_to_run_in_production(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("APP_ENVIRONMENT", "production")
    write(tmp_path, "0001_a.sql", "SELECT 1;")

    assert main(tmp_path) == 1


@pytest.fixture(scope="module")
def db(db_image: str) -> Iterator[PostgresContainer]:
    container = PostgresContainer(
        db_image, username="postgres", password=PASSWORD, dbname=DB_NAME, driver=None
    )
    with container:
        yield container


def query(db: PostgresContainer, sql: str) -> str:
    result = db.exec(
        ExecConfig(command=["psql", "-U", "postgres", "-d", DB_NAME, "-tAc", sql])
    )
    assert result.exit_code == 0, result.output.decode()
    return str(result.output.decode().strip())


def test_main_applies_each_file_in_its_own_transaction(
    db: PostgresContainer, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("POSTGRES_HOST", db.get_container_host_ip())
    monkeypatch.setenv("DB_PORT", str(db.get_exposed_port(5432)))
    monkeypatch.setenv("POSTGRES_DB", DB_NAME)
    monkeypatch.setenv("POSTGRES_PASSWORD", PASSWORD)
    # Vários comandos num arquivo, como um seed de verdade.
    write(
        tmp_path,
        "0001_cria.sql",
        "CREATE TABLE seed_teste (id integer PRIMARY KEY);\n"
        "INSERT INTO seed_teste VALUES (1);\n"
        "INSERT INTO seed_teste VALUES (2);\n",
    )
    # O 3 entra, mas o 1 repetido falha: o arquivo inteiro volta.
    write(
        tmp_path,
        "0002_falha.sql",
        "INSERT INTO seed_teste VALUES (3);\nINSERT INTO seed_teste VALUES (1);\n",
    )
    # Depois de uma falha, os seguintes não rodam.
    write(tmp_path, "0003_depois.sql", "INSERT INTO seed_teste VALUES (4);\n")

    assert main(tmp_path) == 1
    ids = query(db, "SELECT string_agg(id::text, ',' ORDER BY id) FROM seed_teste")
    assert ids == "1,2"
