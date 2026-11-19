"""cria extensoes, schemas e funcoes

Bloco "T5: extensões e schemas" de docs/modelo_banco/esquema.sql.

Revision ID: 0001
Revises:
Create Date: 2026-11-19
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMAS = ("bruto", "referencia", "operacao", "indicadores")


def upgrade() -> None:
    op.execute(
        """
DO $$ BEGIN
  IF current_setting('server_version_num')::int < 160000 THEN
    RAISE EXCEPTION 'este esquema foi testado no PostgreSQL 16+; o servidor é %',
      version();
  END IF;
END $$
"""
    )
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")
    op.execute("CREATE SCHEMA IF NOT EXISTS partman")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman")

    # bruto: o que as fontes devolveram, só INSERT; referencia: GTFS,
    # programação e catálogos; operacao: viagens, passagens e frota;
    # indicadores: resultados calculados.
    for schema in SCHEMAS:
        op.execute(f"CREATE SCHEMA {schema}")

    # Bloqueia UPDATE, DELETE e TRUNCATE no bruto até para o dono da tabela
    # (defesa extra além dos GRANTs). Arquivar meses antigos usa DETACH/DROP de
    # partição, que não passa por aqui.
    op.execute(
        """
CREATE FUNCTION bruto.bloquear_alteracao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'schema bruto é só de inserção (% em %)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$
"""
    )

    # UUID versão 7: os 48 primeiros bits são o instante em milissegundos, então
    # os ids crescem com o tempo e as inserções nas chaves primárias continuam no
    # fim do índice. O Postgres 16 não tem uuidv7() nativo (vem no 18); esta
    # função o substitui e pode ser trocada pela nativa depois. O corpo é
    # idêntico ao do esquema.sql, porque o Postgres guarda o texto como veio.
    op.execute(
        """
CREATE FUNCTION public.uuid_v7() RETURNS uuid
LANGUAGE sql VOLATILE PARALLEL SAFE AS $$
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid())
              PLACING substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
              FROM 1 FOR 6),
      52, 1), 53, 1),
    'hex')::uuid
$$
"""  # noqa: E501
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION public.uuid_v7()")
    op.execute("DROP FUNCTION bruto.bloquear_alteracao()")
    # Sem CASCADE: se sobrou algo num schema, o downgrade para em vez de apagar.
    for schema in reversed(SCHEMAS):
        op.execute(f"DROP SCHEMA {schema}")
    op.execute("DROP EXTENSION pg_partman")
    op.execute("DROP SCHEMA partman")
    op.execute("DROP EXTENSION postgis")
