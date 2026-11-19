"""cria programacao

Bloco "T8: programação (API de itinerários da SPTrans)" de
docs/modelo_banco/esquema.sql. Cada coleta é uma fotografia da programação: o
histórico fica guardado, porque a auditoria de um dia precisa da programação
que valia nele.

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
# Hora do relógio de São Paulo em texto, como a API manda (de 00:00 a 23:59).
HORA_TEXTO = "'^([01][0-9]|2[0-3]):[0-5][0-9]$'"
LINHA_PROGRAMACAO = [
    "referencia.linhas_programacao.coleta_id",
    "referencia.linhas_programacao.codigo_linha",
]


def _key(*, sentido: bool = False) -> list[sa.Column[object]]:
    """Colunas iniciais das tabelas do detalhe de uma linha numa coleta."""
    columns = [
        sa.Column("coleta_id", sa.Integer(), nullable=False),
        sa.Column("codigo_linha", sa.Text(), nullable=False),
    ]
    if sentido:
        columns.append(_sentido())
    return columns


def _sentido() -> sa.Column[int]:
    return sa.Column(
        "sentido",
        sa.SmallInteger(),
        sa.CheckConstraint("sentido IN (0, 1)"),
        nullable=False,
    )


def _tipo_dia() -> sa.Column[str]:
    return sa.Column(
        "tipo_dia",
        sa.Text(),
        sa.CheckConstraint("tipo_dia IN ('U', 'S', 'D')"),
        nullable=False,
    )


def _of_line() -> sa.ForeignKeyConstraint:
    """FK para o cabeçalho da linha na coleta: apagar a coleta apaga tudo."""
    return sa.ForeignKeyConstraint(
        ["coleta_id", "codigo_linha"], LINHA_PROGRAMACAO, ondelete="CASCADE"
    )


def upgrade() -> None:
    op.create_table(
        "coletas_programacao",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("data_coleta", sa.Date(), nullable=False),
        sa.Column(
            "fonte",
            sa.Text(),
            sa.CheckConstraint("fonte IN ('api_itinerarios', 'raspagem_navegador')"),
            nullable=False,
        ),
        sa.Column("iniciada_em", TIMESTAMPTZ, nullable=False),
        sa.Column("concluida_em", TIMESTAMPTZ),
        sa.Column("observacao", sa.Text()),
        sa.UniqueConstraint("data_coleta", "fonte"),
        sa.CheckConstraint("concluida_em IS NULL OR concluida_em >= iniciada_em"),
        schema="referencia",
    )

    # Catálogo + cabeçalho do detalhe de cada linha, por coleta.
    op.create_table(
        "linhas_programacao",
        sa.Column(
            "coleta_id",
            sa.Integer(),
            sa.ForeignKey("referencia.coletas_programacao.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "codigo_linha",
            sa.Text(),
            sa.ForeignKey("referencia.linhas.codigo_linha"),
            nullable=False,
        ),
        # CdPjOID (vai no corpo do detalhe).
        sa.Column("cod_planejamento", sa.Integer(), nullable=False),
        sa.Column("area", sa.SmallInteger()),  # AreCodVig / area
        # Os acentos vêm corrompidos da API (METR�).
        sa.Column("nome", sa.Text()),
        sa.Column("consorcio", sa.Text()),
        sa.Column("empresa", sa.Text()),
        sa.Column("letreiro_ida", sa.Text()),
        sa.Column("letreiro_volta", sa.Text()),
        sa.Column("circular", sa.Boolean()),
        # Linha principal onde o site exibe a auxiliar.
        sa.Column("exibida_em", sa.Text()),
        # Preenchido quando a API respondeu erro.
        sa.Column("erro_api", sa.Text()),
        sa.PrimaryKeyConstraint("coleta_id", "codigo_linha"),
        schema="referencia",
    )

    # Uma linha por horário de partida. minuto_servico deixa a ordem correta e
    # a montagem do instante trivial: horários antes de 03:00 pertencem ao dia
    # de serviço anterior e somam 1440 ('00:10' -> 1450).
    op.create_table(
        "partidas_programadas",
        *_key(sentido=True),
        _tipo_dia(),
        sa.Column("ordem", sa.SmallInteger(), nullable=False),  # ordem na página
        sa.Column(
            "horario",
            sa.Text(),
            sa.CheckConstraint(f"horario ~ {HORA_TEXTO}"),
            nullable=False,
        ),
        sa.Column(
            "minuto_servico",
            sa.SmallInteger(),
            sa.Computed(
                "(split_part(horario, ':', 1)::int * 60"
                " + split_part(horario, ':', 2)::int"
                " + CASE WHEN split_part(horario, ':', 1)::int < 3"
                " THEN 1440 ELSE 0 END)::smallint",
                persisted=True,
            ),
        ),
        sa.Column(
            "faixa_horaria",
            sa.SmallInteger(),
            sa.CheckConstraint("faixa_horaria BETWEEN 0 AND 23"),
            nullable=False,
        ),
        sa.Column("veiculo_acessivel", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint(
            "coleta_id", "codigo_linha", "sentido", "tipo_dia", "ordem"
        ),
        _of_line(),
        schema="referencia",
    )

    op.create_table(
        "tempos_estimados",
        *_key(),
        _tipo_dia(),
        _sentido(),  # inicial* = 0, final* = 1
        sa.Column(
            "periodo",
            sa.Text(),
            sa.CheckConstraint("periodo IN ('manha', 'entrepico', 'tarde')"),
            nullable=False,
        ),
        # NULL quando a API traz <= 1 (o site mostra "-").
        sa.Column("minutos", sa.SmallInteger(), sa.CheckConstraint("minutos > 1")),
        sa.PrimaryKeyConstraint(
            "coleta_id", "codigo_linha", "tipo_dia", "sentido", "periodo"
        ),
        _of_line(),
        schema="referencia",
    )

    # "horarios" da API: "04:00-23:20" separado em início e fim.
    op.create_table(
        "horarios_operacao",
        *_key(),
        _tipo_dia(),
        *(
            sa.Column(name, sa.Text(), sa.CheckConstraint(f"{name} ~ {HORA_TEXTO}"))
            for name in (
                "inicio_ponto_inicial",
                "fim_ponto_inicial",
                "inicio_ponto_final",
                "fim_ponto_final",
            )
        ),
        sa.PrimaryKeyConstraint("coleta_id", "codigo_linha", "tipo_dia"),
        _of_line(),
        schema="referencia",
    )

    op.create_table(
        "itinerarios_vias",
        *_key(sentido=True),
        _tipo_dia(),
        sa.Column("ordem", sa.SmallInteger(), nullable=False),
        sa.Column("nome_via", sa.Text(), nullable=False),  # nomeVia
        sa.Column("numeracao", sa.Text()),  # numVia, ex.: '353 - 523'
        sa.PrimaryKeyConstraint(
            "coleta_id", "codigo_linha", "sentido", "tipo_dia", "ordem"
        ),
        _of_line(),
        schema="referencia",
    )

    op.create_table(
        "eventos_via",
        *_key(),
        sa.Column("sentido", sa.SmallInteger(), nullable=False),
        sa.Column("tipo_dia", sa.Text(), nullable=False),
        sa.Column("ordem_via", sa.SmallInteger(), nullable=False),
        sa.Column("evento_id", sa.Integer(), nullable=False),  # id
        # Ex.: 'Feira-livre - Rua Mourato Coelho'.
        sa.Column("evento", sa.Text()),
        sa.Column("descricao", sa.Text()),
        sa.Column("tipo_evento", sa.Text()),  # ex.: 'Feira Semanal'
        sa.Column("periodicidade", sa.SmallInteger()),
        sa.Column(
            "dia_semana",
            sa.SmallInteger(),
            sa.CheckConstraint("dia_semana BETWEEN 0 AND 7"),
        ),
        sa.Column("horario_inicio", sa.Text()),
        sa.Column("horario_fim", sa.Text()),
        sa.Column("itinerario_alternativo", sa.Text()),  # campo "itinerario"
        sa.PrimaryKeyConstraint(
            "coleta_id", "codigo_linha", "sentido", "tipo_dia", "ordem_via", "evento_id"
        ),
        sa.ForeignKeyConstraint(
            ["coleta_id", "codigo_linha", "sentido", "tipo_dia", "ordem_via"],
            [
                f"referencia.itinerarios_vias.{column}"
                for column in (
                    "coleta_id",
                    "codigo_linha",
                    "sentido",
                    "tipo_dia",
                    "ordem",
                )
            ],
            ondelete="CASCADE",
        ),
        schema="referencia",
    )

    # Instante (UTC) de uma partida programada: data de serviço + minutos, no
    # fuso de São Paulo. STABLE porque depende das regras de fuso. O corpo é
    # idêntico ao do esquema.sql, porque o Postgres guarda o texto como veio.
    op.execute(
        """
CREATE FUNCTION referencia.instante_programado(data_servico date, minuto_servico integer)
RETURNS timestamptz LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT (data_servico + make_interval(mins => minuto_servico)) AT TIME ZONE 'America/Sao_Paulo'
$$
"""  # noqa: E501
    )


def downgrade() -> None:
    op.execute("DROP FUNCTION referencia.instante_programado(date, integer)")
    for table in (
        "eventos_via",
        "itinerarios_vias",
        "horarios_operacao",
        "tempos_estimados",
        "partidas_programadas",
        "linhas_programacao",
        "coletas_programacao",
    ):
        op.drop_table(table, schema="referencia")
