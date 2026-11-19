"""Testes de integração do banco (card T5): extensões, schemas e papéis.

Sobem um PostgreSQL descartável com testcontainers, na mesma imagem do
``make up`` (``db/Dockerfile``), e aplicam as migrações como o
``make migrate``. Sem Docker rodando, os testes são pulados.
"""

from collections.abc import Callable, Iterator
from datetime import datetime

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pydantic import SecretStr
from testcontainers.community.postgres import PostgresContainer
from testcontainers.core.container import ExecConfig

from onibus.api.main import create_app
from onibus.config import Settings

DB_NAME = "onibus"
ADMIN = "postgres"
COLETOR = "coletor_app"
WORKER = "worker_app"
API = "api_app"
INTRUSO = "intruso"
PASSWORDS = {
    ADMIN: "senha-admin-teste",
    COLETOR: "senha-coletor-teste",
    WORKER: "senha-worker-teste",
    API: "senha-api-teste",
    INTRUSO: "senha-intruso-teste",
}
LAYERS = ("bruto", "referencia", "operacao", "indicadores")
GROUPS = ("onibus_coletor", "onibus_worker", "onibus_leitura")

# Logins por serviço, como o README do modelo manda criar em cada ambiente.
LOGINS = f"""
CREATE ROLE {COLETOR} LOGIN PASSWORD '{PASSWORDS[COLETOR]}' IN ROLE onibus_coletor;
CREATE ROLE {WORKER} LOGIN PASSWORD '{PASSWORDS[WORKER]}' IN ROLE onibus_worker;
CREATE ROLE {API} LOGIN PASSWORD '{PASSWORDS[API]}' IN ROLE onibus_leitura;
ALTER ROLE {API} SET statement_timeout = '10s';
ALTER ROLE {API} SET default_transaction_read_only = on;
CREATE ROLE {INTRUSO} LOGIN PASSWORD '{PASSWORDS[INTRUSO]}';
"""

# SQLSTATE dos erros esperados (psql com VERBOSITY=verbose mostra o código).
SEM_PERMISSAO = "42501"  # insufficient_privilege: GRANT ausente ou trigger do bruto
SO_LEITURA = "25006"  # read_only_sql_transaction


def run_sql(db: PostgresContainer, role: str, sql: str) -> tuple[int | None, str]:
    """Roda ``sql`` como ``role``, por TCP e com senha, como a aplicação fará."""
    result = db.exec(
        ExecConfig(
            command=["psql", "-h", "127.0.0.1", "-U", role, "-d", DB_NAME]
            + ["-v", "ON_ERROR_STOP=1", "-v", "VERBOSITY=verbose", "-tAc", sql],
            environment={"PGPASSWORD": PASSWORDS[role]},
        )
    )
    return result.exit_code, result.output.decode()


@pytest.fixture(scope="module")
def db(
    db_image: str, alembic_config: Callable[[PostgresContainer, str], Config]
) -> Iterator[PostgresContainer]:
    """Banco migrado, com os logins dos serviços e uma tabela ``teste``.

    As tabelas ``teste`` são criadas pelo dono do banco, como uma migração
    faria, para conferir que as permissões padrão valem para tabelas futuras.
    A de ``bruto`` liga o trigger de só inserção, como as tabelas do modelo, e
    já tem uma linha, para o trigger de linha ter o que bloquear.
    """
    container = PostgresContainer(
        db_image,
        username=ADMIN,
        password=PASSWORDS[ADMIN],
        dbname=DB_NAME,
        driver=None,
    )
    with container:
        command.upgrade(alembic_config(container, DB_NAME), "head")
        setup = LOGINS + " ".join(
            f"CREATE TABLE {schema}.teste"
            " (id uuid DEFAULT gen_random_uuid() PRIMARY KEY, valor text);"
            for schema in LAYERS
        )
        setup += (
            "CREATE TRIGGER teste_so_insercao BEFORE UPDATE OR DELETE ON bruto.teste"
            " FOR EACH ROW EXECUTE FUNCTION bruto.bloquear_alteracao();"
            "INSERT INTO bruto.teste (valor) VALUES ('original');"
        )
        exit_code, output = run_sql(container, ADMIN, setup)
        assert exit_code == 0, output
        yield container


def test_extensions_are_installed(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, ADMIN, "SELECT extname FROM pg_extension")

    assert exit_code == 0, output
    assert {"postgis", "pg_partman"} <= set(output.split())


def test_server_is_postgres_16(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, ADMIN, "SHOW server_version_num")

    assert exit_code == 0, output
    assert output.strip().startswith("16")


def test_layer_schemas_exist(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, ADMIN, "SELECT nspname FROM pg_namespace")

    assert exit_code == 0, output
    assert set(LAYERS) <= set(output.split())


@pytest.mark.parametrize("group", GROUPS)
def test_groups_exist_without_login(db: PostgresContainer, group: str) -> None:
    sql = f"SELECT rolcanlogin FROM pg_roles WHERE rolname = '{group}'"
    exit_code, output = run_sql(db, ADMIN, sql)

    assert exit_code == 0, output
    assert output.strip() == "f"


@pytest.mark.parametrize(
    ("role", "sql"),
    [
        (COLETOR, "INSERT INTO bruto.teste (valor) VALUES ('x')"),
        (COLETOR, "INSERT INTO referencia.teste (valor) VALUES ('x')"),
        (COLETOR, "UPDATE referencia.teste SET valor = 'y'"),
        (WORKER, "SELECT count(*) FROM bruto.posicoes_gps"),
        (WORKER, "SELECT count(*) FROM referencia.linhas"),
        (WORKER, "INSERT INTO operacao.teste (valor) VALUES ('x')"),
        (WORKER, "UPDATE operacao.teste SET valor = 'y'"),
        (WORKER, "DELETE FROM indicadores.teste"),
        (API, "SELECT count(*) FROM referencia.linhas"),
        (API, "SELECT count(*) FROM operacao.viagens"),
        (API, "SELECT count(*) FROM indicadores.saidas_auditadas"),
        (API, "SELECT count(*) FROM indicadores.ipp_mensal"),
    ],
)
def test_roles_can_do_their_job(db: PostgresContainer, role: str, sql: str) -> None:
    exit_code, output = run_sql(db, role, sql)

    assert exit_code == 0, output


@pytest.mark.parametrize(
    ("role", "sql", "error"),
    [
        # Critério de aceite do T5: a API não escreve (nem lê) o bruto.
        (API, "INSERT INTO bruto.ciclos_coleta DEFAULT VALUES", SEM_PERMISSAO),
        (API, "INSERT INTO bruto.teste (valor) VALUES ('x')", SEM_PERMISSAO),
        (API, "SELECT count(*) FROM bruto.posicoes_gps", SEM_PERMISSAO),
        (API, "INSERT INTO indicadores.teste (valor) VALUES ('x')", SO_LEITURA),
        (COLETOR, "UPDATE bruto.teste SET valor = 'y'", SEM_PERMISSAO),
        (COLETOR, "DELETE FROM bruto.teste", SEM_PERMISSAO),
        (COLETOR, "TRUNCATE bruto.posicoes_gps", SEM_PERMISSAO),
        (COLETOR, "SELECT count(*) FROM operacao.viagens", SEM_PERMISSAO),
        (WORKER, "INSERT INTO bruto.teste (valor) VALUES ('x')", SEM_PERMISSAO),
        (WORKER, "CREATE TABLE operacao.outra (id int)", SEM_PERMISSAO),
        (WORKER, "CREATE TABLE public.outra (id int)", SEM_PERMISSAO),
        # Nem o dono altera o bruto: o trigger barra.
        (ADMIN, "UPDATE bruto.teste SET valor = 'y'", SEM_PERMISSAO),
        (ADMIN, "DELETE FROM bruto.teste", SEM_PERMISSAO),
        (ADMIN, "TRUNCATE bruto.posicoes_gps", SEM_PERMISSAO),
    ],
)
def test_roles_are_denied_outside_their_job(
    db: PostgresContainer, role: str, sql: str, error: str
) -> None:
    exit_code, output = run_sql(db, role, sql)

    assert exit_code != 0
    assert f"ERROR:  {error}" in output, output


def test_role_without_group_cannot_connect(db: PostgresContainer) -> None:
    exit_code, output = run_sql(db, INTRUSO, "SELECT 1")

    assert exit_code != 0
    assert "permission denied for database" in output, output


def test_health_reads_last_olho_vivo_cycle_as_api_login(db: PostgresContainer) -> None:
    settings = Settings(
        database_host=db.get_container_host_ip(),
        database_port=int(db.get_exposed_port(5432)),
        database_name=DB_NAME,
        api_database_user=API,
        api_database_password=SecretStr(PASSWORDS[API]),
    )

    with TestClient(create_app(settings)) as client:
        empty = client.get("/health")
        assert empty.status_code == 200
        assert empty.json() == {
            "status": "ok",
            "last_collection_at": None,
            "last_successful_collection_at": None,
        }

        exit_code, output = run_sql(
            db,
            COLETOR,
            """
            INSERT INTO bruto.ciclos_coleta
                (fonte, iniciado_em, concluido_em, status, versao_coletor, erro)
            VALUES
                ('olhovivo_posicao', '2026-11-19 11:00Z', '2026-11-19 11:01Z',
                 'ok', 'teste', NULL),
                ('olhovivo_posicao', '2026-11-19 12:00Z', '2026-11-19 12:01Z',
                 'erro_rede', 'teste', 'erro simulado'),
                ('gtfs', '2026-11-19 13:00Z', '2026-11-19 13:01Z',
                 'ok', 'teste', NULL)
            """,
        )
        assert exit_code == 0, output
        response = client.get("/health")
        assert response.status_code == 200
        body = response.json()
        # O último ciclo falhou: os dois horários mostram que o coletor roda,
        # mas não grava com sucesso desde 11:01.
        assert datetime.fromisoformat(
            body["last_collection_at"]
        ) == datetime.fromisoformat("2026-11-19T12:01:00+00:00")
        assert datetime.fromisoformat(
            body["last_successful_collection_at"]
        ) == datetime.fromisoformat("2026-11-19T11:01:00+00:00")

    settings.api_database_password = SecretStr("senha-incorreta")
    with TestClient(create_app(settings)) as client:
        unavailable = client.get("/health")
        assert unavailable.status_code == 503
        assert unavailable.json()["error"]["code"] == "service_unavailable"
