"""cria papeis e permissoes

Bloco "T5: papéis e permissões" de docs/modelo_banco/esquema.sql. Vem antes
das tabelas: os ALTER DEFAULT PRIVILEGES dão os direitos a toda tabela, view e
sequência criada depois pelo mesmo usuário (o dono do banco), inclusive nas
migrações futuras e nas partições do pg_partman.

Papéis de grupo (NOLOGIN), um por serviço, cada um com o mínimo:
  onibus_coletor  grava o bruto (só INSERT) e mantém catálogos e programação;
  onibus_worker   lê bruto e referência, escreve operacao e indicadores;
  onibus_leitura  a API: só SELECT, e nada do bruto (o mapa usa a view).
Os usuários de login são criados por ambiente (docs/modelo_banco/README.md).

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLES = ("onibus_coletor", "onibus_worker", "onibus_leitura")

# (schema, papéis com USAGE)
SCHEMA_USAGE = (
    ("bruto", "onibus_coletor, onibus_worker"),
    ("referencia", "onibus_coletor, onibus_worker, onibus_leitura"),
    ("operacao", "onibus_worker, onibus_leitura"),
    ("indicadores", "onibus_worker, onibus_leitura"),
)

# (schema, privilégios, tipo de objeto, papéis)
DEFAULT_PRIVILEGES = (
    ("bruto", "SELECT, INSERT", "TABLES", "onibus_coletor"),
    ("bruto", "SELECT", "TABLES", "onibus_worker"),
    ("referencia", "SELECT, INSERT, UPDATE", "TABLES", "onibus_coletor"),
    ("referencia", "USAGE", "SEQUENCES", "onibus_coletor"),
    ("referencia", "SELECT", "TABLES", "onibus_worker, onibus_leitura"),
    ("operacao", "SELECT, INSERT, UPDATE, DELETE", "TABLES", "onibus_worker"),
    ("indicadores", "SELECT, INSERT, UPDATE, DELETE", "TABLES", "onibus_worker"),
    ("operacao", "SELECT", "TABLES", "onibus_leitura"),
    ("indicadores", "SELECT", "TABLES", "onibus_leitura"),
)


def upgrade() -> None:
    # Papéis valem para o servidor inteiro: podem já existir (outro banco).
    for role in ROLES:
        op.execute(
            f"""
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = '{role}') THEN
    CREATE ROLE {role} NOLOGIN;
  END IF;
END $$
"""
        )

    # Ninguém cria objetos no public nem conecta sem ter recebido o direito.
    # (O banco deve ser só deste sistema: isto tira o CONNECT de quem não for
    # um dos papéis acima.)
    op.execute("REVOKE CREATE ON SCHEMA public FROM PUBLIC")
    op.execute(
        """
DO $$ BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I
                  TO onibus_coletor, onibus_worker', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO onibus_leitura',
                 current_database());
END $$
"""
    )

    for schema, roles in SCHEMA_USAGE:
        op.execute(f"GRANT USAGE ON SCHEMA {schema} TO {roles}")
    for schema, privileges, objects, roles in DEFAULT_PRIVILEGES:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema}"
            f" GRANT {privileges} ON {objects} TO {roles}"
        )


def downgrade() -> None:
    for schema, privileges, objects, roles in DEFAULT_PRIVILEGES:
        op.execute(
            f"ALTER DEFAULT PRIVILEGES IN SCHEMA {schema}"
            f" REVOKE {privileges} ON {objects} FROM {roles}"
        )
    for schema, roles in SCHEMA_USAGE:
        op.execute(f"REVOKE USAGE ON SCHEMA {schema} FROM {roles}")
    # Volta ao padrão do Postgres: PUBLIC conecta e cria tabelas temporárias.
    # O CREATE no public não volta: no Postgres 15+ o PUBLIC já não o tem.
    op.execute(
        """
DO $$ BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I
                  FROM onibus_coletor, onibus_worker, onibus_leitura',
                 current_database());
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO PUBLIC',
                 current_database());
END $$
"""
    )
    # Os papéis ficam: são do servidor, e outro banco (ou os logins api_app,
    # coletor_app e worker_app) pode depender deles. Sem direitos aqui, não
    # acessam nada neste banco.
