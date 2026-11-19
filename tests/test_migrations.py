"""Testes das migrações Alembic (card T6).

Sem banco: a cadeia de revisões segue a convenção de db/migrations/README.md.
Com Docker: num banco limpo, as migrações criam exatamente o modelo validado
no T5 (docs/modelo_banco/esquema.sql), e cada uma volta (downgrade) e é
reaplicada sem erro. Sem Docker rodando, estes últimos são pulados.
"""

import ast
import difflib
import io
import re
import tarfile
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import Script, ScriptDirectory
from testcontainers.community.postgres import PostgresContainer

REPO = Path(__file__).resolve().parents[1]
MODELO = REPO / "docs" / "modelo_banco" / "esquema.sql"
FILE_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.py$")
# Última migração do modelo do T5: até ela, o esquema é o do esquema.sql.
MODEL_REVISION = "0007"
ADMIN_PASSWORD = "senha-admin-teste"
# O mesmo mês que o alembic_config do conftest passa às migrações.
INICIO_PARTICOES = "2026-08-01"

AlembicConfig = Callable[[PostgresContainer, str], Config]


def revisions() -> list[Script]:
    """Revisões da primeira à última."""
    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    return list(reversed(list(script.walk_revisions())))


def test_revisions_form_a_single_numbered_chain() -> None:
    script = ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    chain = revisions()

    assert script.get_heads() == [chain[-1].revision]
    for number, revision in enumerate(chain, start=1):
        assert revision.revision == f"{number:04d}"
        assert revision.down_revision == (f"{number - 1:04d}" if number > 1 else None)


def test_file_names_follow_convention() -> None:
    for revision in revisions():
        name = Path(revision.path).name
        match = FILE_NAME.match(name)

        assert match, f"{name}: use NNNN_descricao_sem_acento.py"
        assert match.group(1) == revision.revision, name


def test_every_migration_has_a_downgrade() -> None:
    for revision in revisions():
        tree = ast.parse(Path(revision.path).read_text(encoding="utf-8"))
        body = next(
            node.body
            for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "downgrade"
        )

        assert not all(isinstance(node, ast.Pass) for node in body), revision.path


def run(server: PostgresContainer, command_: list[str]) -> str:
    """Roda um comando no container (como postgres, pelo socket) e devolve a saída."""
    result = server.exec(command_)
    output = result.output.decode()
    assert result.exit_code == 0, output
    return output


def psql(server: PostgresContainer, database: str, sql: str) -> str:
    return run(
        server,
        [
            "psql",
            "-U",
            "postgres",
            "-d",
            database,
            "-v",
            "ON_ERROR_STOP=1",
            "-tAc",
            sql,
        ],
    )


def schema_dump(server: PostgresContainer, database: str) -> list[str]:
    """Linhas do ``pg_dump --schema-only``, sem comentários e em ordem.

    A ordem das linhas não importa: a dos GRANTs de uma tabela, por exemplo,
    depende de quando cada direito foi dado (no fim do esquema.sql ou pelos
    privilégios padrão da 0002), e o resultado é o mesmo.
    """
    output = run(
        server,
        ["pg_dump", "-U", "postgres", "--schema-only"]
        + ["--exclude-table=public.alembic_version", database],
    )
    lines = (line.strip() for line in output.splitlines())
    skip = ("--", "\\restrict", "\\unrestrict", "SET ", "SELECT pg_catalog.set_config")
    return sorted(line for line in lines if line and not line.startswith(skip))


@pytest.fixture(scope="module")
def server(db_image: str) -> Iterator[PostgresContainer]:
    """Servidor descartável; cada teste cria nele os próprios bancos."""
    container = PostgresContainer(
        db_image,
        username="postgres",
        password=ADMIN_PASSWORD,
        dbname="postgres",
        driver=None,
    )
    with container:
        yield container


def test_migrations_reproduce_the_validated_model(
    server: PostgresContainer, alembic_config: AlembicConfig
) -> None:
    psql(server, "postgres", "CREATE DATABASE migrado")
    psql(server, "postgres", "CREATE DATABASE modelo")
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode="w") as tar:
        tar.add(MODELO, arcname=MODELO.name)
    server.get_wrapped_container().put_archive("/tmp", archive.getvalue())

    command.upgrade(alembic_config(server, "migrado"), MODEL_REVISION)
    run(
        server,
        ["psql", "-U", "postgres", "-d", "modelo", "-q", "-v", "ON_ERROR_STOP=1"]
        + ["-v", f"inicio_particoes={INICIO_PARTICOES}", "-f", f"/tmp/{MODELO.name}"],
    )

    diff = list(
        difflib.unified_diff(
            schema_dump(server, "modelo"),
            schema_dump(server, "migrado"),
            "esquema.sql",
            "migrações",
            lineterm="",
        )
    )
    assert not diff, "\n".join(diff)


def test_each_migration_goes_down_and_up_again(
    server: PostgresContainer, alembic_config: AlembicConfig
) -> None:
    psql(server, "postgres", "CREATE DATABASE escada")
    config = alembic_config(server, "escada")

    for revision in revisions():
        command.upgrade(config, revision.revision)
        command.downgrade(config, "-1")
        command.upgrade(config, revision.revision)

    assert psql(
        server, "escada", "SELECT version_num FROM alembic_version"
    ).strip() == (revisions()[-1].revision)


def test_downgrade_to_base_leaves_the_database_clean(
    server: PostgresContainer, alembic_config: AlembicConfig
) -> None:
    psql(server, "postgres", "CREATE DATABASE limpo")
    config = alembic_config(server, "limpo")
    command.upgrade(config, "head")

    command.downgrade(config, "base")

    leftovers = psql(
        server,
        "limpo",
        """
SELECT (SELECT string_agg(nspname, ',' ORDER BY nspname) FROM pg_namespace
         WHERE nspname NOT LIKE 'pg\\_%' AND nspname <> 'information_schema'),
       (SELECT string_agg(extname, ',' ORDER BY extname) FROM pg_extension),
       (SELECT string_agg(relname, ',' ORDER BY relname) FROM pg_class c
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE nspname = 'public' AND relname NOT LIKE 'alembic\\_version%'),
       (SELECT count(*) FROM pg_proc WHERE proname = 'uuid_v7'),
       (SELECT count(*) FROM pg_default_acl),
       (SELECT datacl::text LIKE '%onibus%' FROM pg_database
         WHERE datname = current_database())
""",
    )
    assert leftovers.strip() == "public|plpgsql||0|0|f"
    # E dá para subir tudo de novo.
    command.upgrade(config, "head")
