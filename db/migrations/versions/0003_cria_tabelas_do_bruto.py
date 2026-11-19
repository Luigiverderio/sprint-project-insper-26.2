"""cria tabelas do bruto

Blocos "T8: ciclos de coleta" e "T7: posições GPS" de
docs/modelo_banco/esquema.sql. O bruto é só de inserção: os triggers barram
UPDATE, DELETE e TRUNCATE até para o dono.

A primeira partição de bruto.posicoes_gps é a de 3 meses antes do mês atual.
Para escolher outra: ``alembic -x inicio_particoes=2026-08-01 upgrade head``.

Revision ID: 0003
Revises: 0002
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def _block_changes(table: str) -> None:
    """Liga o trigger de só inserção (linha) e o de TRUNCATE (comando)."""
    op.execute(
        f"CREATE TRIGGER {table}_so_insercao BEFORE UPDATE OR DELETE"
        f" ON bruto.{table}"
        " FOR EACH ROW EXECUTE FUNCTION bruto.bloquear_alteracao()"
    )
    op.execute(
        f"CREATE TRIGGER {table}_sem_truncate BEFORE TRUNCATE ON bruto.{table}"
        " FOR EACH STATEMENT EXECUTE FUNCTION bruto.bloquear_alteracao()"
    )


def upgrade() -> None:
    # Um ciclo por chamada a uma fonte, com sucesso ou falha. Serve para achar
    # lacunas (US13) e para a saúde da coleta (US30). Escrito numa única
    # inserção, no fim do ciclo, na mesma transação das posições.
    op.create_table(
        "ciclos_coleta",
        sa.Column(
            "id",
            sa.Uuid(),
            server_default=sa.text("public.uuid_v7()"),
            primary_key=True,
        ),
        sa.Column(
            "fonte",
            sa.Text(),
            sa.CheckConstraint(
                "fonte IN ('olhovivo_posicao', 'itinerarios_catalogo',"
                " 'itinerarios_linha', 'gtfs')"
            ),
            nullable=False,
        ),
        sa.Column("alvo", sa.Text()),  # ex.: código da linha consultada
        sa.Column("iniciado_em", TIMESTAMPTZ, nullable=False),
        sa.Column("concluido_em", TIMESTAMPTZ, nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            sa.CheckConstraint(
                "status IN ('ok', 'erro_rede', 'erro_http', 'erro_autenticacao',"
                " 'resposta_invalida')"
            ),
            nullable=False,
        ),
        sa.Column("http_status", sa.SmallInteger()),
        # Campo "hr" da Olho Vivo: hora local, texto de 5 caracteres.
        sa.Column("hora_api", sa.Text()),
        # Registros que vieram na resposta; gravados (a diferença eram
        # repetidos); descartados pelo coletor (coordenada ou horário inválido).
        sa.Column("n_recebidos", sa.Integer(), sa.CheckConstraint("n_recebidos >= 0")),
        sa.Column("n_inseridos", sa.Integer(), sa.CheckConstraint("n_inseridos >= 0")),
        sa.Column(
            "n_rejeitados", sa.Integer(), sa.CheckConstraint("n_rejeitados >= 0")
        ),
        sa.Column("arquivo_bruto", sa.Text()),  # chave do JSON original .gz
        sa.Column("versao_coletor", sa.Text(), nullable=False),
        sa.Column("erro", sa.Text()),
        sa.CheckConstraint("concluido_em >= iniciado_em"),
        sa.CheckConstraint(
            "n_inseridos IS NULL OR n_recebidos IS NULL"
            " OR n_inseridos + coalesce(n_rejeitados, 0) <= n_recebidos"
        ),
        # Toda falha diz o porquê.
        sa.CheckConstraint("status = 'ok' OR erro IS NOT NULL"),
        schema="bruto",
    )
    op.create_index(
        "ciclos_coleta_fonte_inicio_idx",
        "ciclos_coleta",
        ["fonte", "iniciado_em"],
        schema="bruto",
    )
    _block_changes("ciclos_coleta")

    # Uma linha por (veículo, instante do GPS). A Olho Vivo repete o mesmo "ta"
    # em consultas seguidas; a chave primária descarta a repetição
    # (INSERT ... ON CONFLICT DO NOTHING). c, lt0 e lt1 ficam em
    # referencia.linhas_olhovivo; a resposta íntegra, no JSON do ciclo. Os
    # CHECKs rejeitam o lote inteiro: o coletor filtra antes e conta em
    # ciclos_coleta.n_rejeitados.
    op.create_table(
        "posicoes_gps",
        sa.Column("horario_gps", TIMESTAMPTZ, nullable=False),  # "ta", em UTC
        sa.Column("prefixo", sa.Integer(), nullable=False),  # "p"
        # "cl": a Olho Vivo tem um cl por linha E sentido.
        sa.Column("cod_linha_sentido", sa.Integer(), nullable=False),
        sa.Column(
            "sentido_api",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido_api IN (1, 2)"),
            nullable=False,
        ),
        sa.Column("acessivel", sa.Boolean()),  # "a"
        sa.Column(
            "latitude",
            sa.Double(),
            sa.CheckConstraint("latitude BETWEEN -90 AND 90"),
            nullable=False,
        ),
        sa.Column(
            "longitude",
            sa.Double(),
            sa.CheckConstraint("longitude BETWEEN -180 AND 180"),
            nullable=False,
        ),
        # Deferida: posições e ciclo entram na mesma transação, ciclo por último.
        sa.Column(
            "ciclo_id",
            sa.Uuid(),
            sa.ForeignKey(
                "bruto.ciclos_coleta.id", deferrable=True, initially="DEFERRED"
            ),
            nullable=False,
        ),
        # Também serve "um ônibus num período".
        sa.PrimaryKeyConstraint("prefixo", "horario_gps"),
        schema="bruto",
        postgresql_partition_by="RANGE (horario_gps)",
    )
    # Leitura típica do worker e do app: "uma linha num dia".
    op.create_index(
        "posicoes_gps_linha_horario_idx",
        "posicoes_gps",
        ["cod_linha_sentido", "horario_gps"],
        schema="bruto",
    )
    # Mapa ao vivo: "últimos minutos". BRIN é minúsculo e serve a dados que
    # chegam quase em ordem de tempo.
    op.create_index(
        "posicoes_gps_horario_brin",
        "posicoes_gps",
        ["horario_gps"],
        schema="bruto",
        postgresql_using="brin",
    )
    # O trigger de linha é clonado em toda partição, inclusive nas criadas depois.
    _block_changes("posicoes_gps")

    # Partição mensal, com fronteiras em UTC. A default recebe horários fora das
    # faixas criadas (GPS com relógio errado): monitorar em
    # operacao.saude_particoes.
    op.execute("SET LOCAL timezone = 'UTC'")
    inicio = context.get_x_argument(as_dictionary=True).get("inicio_particoes")
    op.execute(
        sa.text(
            """
SELECT partman.create_parent(
  p_parent_table    => 'bruto.posicoes_gps',
  p_control         => 'horario_gps',
  p_interval        => '1 month',
  p_premake         => 3,
  p_start_partition => COALESCE(CAST(:inicio AS text),
                                to_char(date_trunc('month', now())
                                        - interval '3 months', 'YYYY-MM-DD'))
)
"""
        ).bindparams(inicio=inicio)
    )
    # Meses antigos: a manutenção só DESANEXA (retention_keep_table); o job de
    # arquivo exporta a tabela desanexada para Parquet, confere e faz o DROP.
    op.execute(
        "UPDATE partman.part_config"
        " SET retention = '3 months', retention_keep_table = true,"
        " infinite_time_partitions = true"
        " WHERE parent_table = 'bruto.posicoes_gps'"
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM partman.part_config WHERE parent_table = 'bruto.posicoes_gps'"
    )
    # Apagar a tabela mãe apaga as partições anexadas. Partições já desanexadas
    # pela retenção continuam no bruto, à espera do arquivo, e fazem o
    # downgrade da 0001 parar no DROP SCHEMA bruto: decida o que fazer com elas.
    op.drop_table("posicoes_gps", schema="bruto")
    op.execute("DROP TABLE partman.template_bruto_posicoes_gps")
    op.drop_table("ciclos_coleta", schema="bruto")
