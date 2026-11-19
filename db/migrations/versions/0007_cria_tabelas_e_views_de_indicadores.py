"""cria tabelas e views de indicadores

Bloco "indicadores" de docs/modelo_banco/esquema.sql: saídas auditadas, faixas
do ICV e as views de IPP e ICV, com as mesmas colunas dos CSVs do R.

Revision ID: 0007
Revises: 0006
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
MES = "(date_trunc('month', data_servico::timestamp)::date)"
REALIZADAS = (
    "'realizada_no_horario', 'realizada_com_atraso', 'realizada_antecipadamente'"
)

# As cinco views de IPP têm as mesmas colunas que os CSVs do R; só muda o
# agrupamento. Filtre sempre por linha e/ou período.
IPP_VIEWS = {
    # ipp_diario_por_sentido.csv
    "ipp_diario_sentido": "codigo_linha, data_servico, tipo_dia, sentido",
    "ipp_diario": "codigo_linha, data_servico, tipo_dia",  # ipp_diario.csv
    # ipp_mensal_por_sentido.csv
    "ipp_mensal_sentido": "codigo_linha, mes, sentido",
    "ipp_mensal": "codigo_linha, mes",  # ranking_linhas.csv (sem as viagens)
    # ranking_linhas_horario.csv (por mês; o R soma todos os meses)
    "ipp_por_hora": "codigo_linha, mes, tipo_dia, hora_prevista",
}
IPP_COLUMNS = """
       count(*) AS programadas_total,
       count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario,
       count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas,
       count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b,
       count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario),
                       count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),
                       count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),
                       count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso
                                          OR antecipada),
                       count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada),
                       count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),
                       count(*) FILTER (WHERE valida)) AS inconclusivas_pct"""

# ICV (R/13_icv.R): realizadas limitadas ao programado em cada faixa; período =
# soma das limitadas / soma das programadas; dia = média dos períodos; mês do
# sentido = média dos dias; linha = média dos sentidos.
ICV_VIEWS = {
    "icv_periodo_dia": """
SELECT codigo_linha, data_servico, tipo_dia, sentido, periodo,
       sum(programadas) AS programadas,
       sum(LEAST(realizadas, programadas)) AS realizadas_lim,
       sum(LEAST(realizadas + quase_completas + inconclusivas, programadas))
         AS ampliadas_lim,
       sum(LEAST(realizadas, programadas))::numeric / sum(programadas) AS icv,
       sum(LEAST(realizadas + quase_completas + inconclusivas, programadas))::numeric
         / sum(programadas) AS icv_ampliado
  FROM indicadores.icv_faixas
 GROUP BY codigo_linha, data_servico, tipo_dia, sentido, periodo""",
    "icv_diario_sentido": """
SELECT codigo_linha, data_servico, tipo_dia, sentido,
       avg(icv) AS icv, avg(icv_ampliado) AS icv_ampliado,
       sum(programadas) AS programadas, sum(realizadas_lim) AS realizadas_lim,
       sum(ampliadas_lim) AS ampliadas_lim
  FROM indicadores.icv_periodo_dia
 GROUP BY codigo_linha, data_servico, tipo_dia, sentido""",
    "icv_mensal_sentido": """
SELECT codigo_linha, date_trunc('month', data_servico::timestamp)::date AS mes,
       sentido,
       avg(icv) AS icv, avg(icv_ampliado) AS icv_ampliado, count(*) AS dias,
       sum(programadas) AS programadas, sum(realizadas_lim) AS realizadas_lim
  FROM indicadores.icv_diario_sentido
 GROUP BY 1, 2, 3""",
    "icv_mensal": """
SELECT codigo_linha, mes,
       round(100 * avg(icv), 2)          AS icv_pct,
       round(100 * avg(icv_ampliado), 2) AS icv_ampliado_pct,
       sum(programadas)                  AS programadas,
       sum(realizadas_lim)               AS realizadas_lim
  FROM indicadores.icv_mensal_sentido
 GROUP BY codigo_linha, mes""",
}


def _check_in(column: str, *values: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})")


def _execucao() -> sa.Column[object]:
    return sa.Column(
        "execucao_id",
        sa.Uuid(),
        sa.ForeignKey("operacao.execucoes.id"),
        nullable=False,
    )


def _saida() -> sa.Column[object]:
    return sa.Column(
        "saida_id",
        sa.Uuid(),
        sa.ForeignKey("indicadores.saidas_auditadas.saida_id", ondelete="CASCADE"),
        nullable=False,
    )


def _percent(column: str) -> sa.Column[float]:
    return sa.Column(
        column, sa.Double(), sa.CheckConstraint(f"{column} BETWEEN 0 AND 100")
    )


def upgrade() -> None:
    # Uma linha por saída programada auditada. Junta auditoria_saidas e
    # diagnostico_saidas do R (eram 1:1 pela mesma chave). codigo_linha e
    # data_servico se repetem para filtrar sem JOIN; a FK composta garante que
    # batem com a saída.
    op.create_table(
        "saidas_auditadas",
        sa.Column("saida_id", sa.Uuid(), primary_key=True),
        sa.Column("codigo_linha", sa.Text(), nullable=False),
        sa.Column("data_servico", sa.Date(), nullable=False),
        sa.Column(
            "viagem_id",
            sa.Uuid(),
            sa.ForeignKey("operacao.viagens.id", ondelete="SET NULL"),
        ),
        sa.Column("prefixo_observado", sa.Integer()),
        sa.Column("partida_observada", TIMESTAMPTZ),
        # double, não real: em float4 uma diferença de 3,00000001 vira 3,0 e
        # muda o status (atraso -> no horário) em relação ao que o worker
        # calculou.
        sa.Column("diferenca_min", sa.Double()),
        sa.Column(
            "status",
            sa.Text(),
            _check_in(
                "status",
                "realizada_no_horario",
                "realizada_com_atraso",
                "realizada_antecipadamente",
                "viagem_quase_completa",
                "partida_sem_viagem_completa",
                "possivelmente_nao_realizada",
                "inconclusiva",
                "fora_do_periodo_coletado",
                "programacao_invalida",
            ),
            nullable=False,
        ),
        sa.Column(
            "nivel_confianca",
            sa.Text(),
            _check_in("nivel_confianca", "alta", "media", "baixa", "muito_baixa"),
        ),
        sa.Column(
            "cobertura_dados",
            sa.Text(),
            _check_in(
                "cobertura_dados",
                "boa",
                "parcial",
                "insuficiente",
                "lacuna_gps_veiculo",
            ),
        ),
        sa.Column(
            "frac_min_dados",
            sa.Double(),
            sa.CheckConstraint("frac_min_dados BETWEEN 0 AND 1"),
        ),
        _percent("pct_tracado"),
        _percent("pct_cobertura"),
        sa.Column("evidencia", sa.Text()),
        sa.Column("so_lacuna_aberta", sa.Boolean()),
        sa.Column(
            "diagnostico_frota",
            sa.Text(),
            _check_in(
                "diagnostico_frota",
                "sem_dados_da_linha",
                "nenhum_veiculo_sem_gps_poderia_fazer",
                "possivel_viagem_sem_gps_no_horario",
                "possivel_viagem_sem_gps_com_atraso",
                "viagem_interrompida_sem_lacuna",
                "viagem_quase_completa",
                "conclusao_compativel_com_lacuna_gps",
                "lacuna_gps_curta_demais_para_concluir",
            ),
        ),
        sa.Column("detalhe_frota", sa.Text()),
        sa.Column(  # era "21013,21035" em texto
            "veiculos_possiveis",
            postgresql.ARRAY(sa.Integer()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(  # IPP (a) e (b): 3 min
            "tolerancia_min",
            sa.Double(),
            sa.CheckConstraint("tolerancia_min > 0"),
            nullable=False,
        ),
        sa.Column(  # IPP (c)
            "tolerancia_ampliada_min",
            sa.Double(),
            server_default=sa.text("4"),
            nullable=False,
        ),
        _execucao(),
        sa.ForeignKeyConstraint(
            ["saida_id", "codigo_linha", "data_servico"],
            [
                "operacao.saidas_programadas.id",
                "operacao.saidas_programadas.codigo_linha",
                "operacao.saidas_programadas.data_servico",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("tolerancia_ampliada_min >= tolerancia_min"),
        # O status de uma saída realizada precisa bater com a diferença (regra
        # ipp-0.2.0).
        sa.CheckConstraint(
            "status <> 'realizada_no_horario' OR abs(diferenca_min) <= tolerancia_min"
        ),
        sa.CheckConstraint(
            "status <> 'realizada_com_atraso' OR diferenca_min > tolerancia_min"
        ),
        sa.CheckConstraint(
            "status <> 'realizada_antecipadamente' OR diferenca_min < -tolerancia_min"
        ),
        # Uma saída realizada sempre tem a diferença calculada: sem isto, NULL
        # passaria nos CHECKs acima.
        sa.CheckConstraint(
            f"status NOT IN ({REALIZADAS}) OR diferenca_min IS NOT NULL"
        ),
        sa.CheckConstraint(
            "status NOT IN ('possivelmente_nao_realizada',"
            " 'fora_do_periodo_coletado', 'programacao_invalida')"
            " OR viagem_id IS NULL"
        ),
        schema="indicadores",
    )
    op.create_index(
        "saidas_auditadas_linha_dia_idx",
        "saidas_auditadas",
        ["codigo_linha", "data_servico"],
        schema="indicadores",
    )
    # Ranking de todas as linhas num mês (a expressão é a mesma das views).
    op.create_index(
        "saidas_auditadas_mes_idx",
        "saidas_auditadas",
        [sa.text(MES)],
        schema="indicadores",
    )
    op.create_index(
        "saidas_auditadas_execucao_idx",
        "saidas_auditadas",
        ["execucao_id"],
        schema="indicadores",
    )

    # Era o texto "partida espúria descartada: 82409 às 09:36:58 percorreu 7%".
    op.create_table(
        "partidas_descartadas",
        _saida(),
        sa.Column("prefixo", sa.Integer(), nullable=False),
        sa.Column("partida_em", TIMESTAMPTZ, nullable=False),
        sa.Column(
            "frac_tracado",
            sa.Double(),
            sa.CheckConstraint("frac_tracado BETWEEN 0 AND 1"),
            nullable=False,
        ),
        sa.Column("km", sa.Double(), sa.CheckConstraint("km >= 0"), nullable=False),
        sa.PrimaryKeyConstraint("saida_id", "prefixo", "partida_em"),
        schema="indicadores",
    )

    # Era a coluna lacunas_ids em texto.
    op.create_table(
        "saidas_lacunas",
        _saida(),
        sa.Column(
            "lacuna_id",
            sa.Uuid(),
            sa.ForeignKey("operacao.lacunas_veiculo.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("saida_id", "lacuna_id"),
        schema="indicadores",
    )
    op.create_index(
        "saidas_lacunas_lacuna_idx",
        "saidas_lacunas",
        ["lacuna_id"],
        schema="indicadores",
    )

    # ICV (IQT.GSO.001.05): contagens por faixa horária. hora é a hora do
    # RELÓGIO local da partida programada (como no R/13_icv.R); 0–2h são o fim
    # do dia de serviço. O período sai da hora, então não tem como discordar.
    op.create_table(
        "icv_faixas",
        sa.Column(
            "codigo_linha",
            sa.Text(),
            sa.ForeignKey("referencia.linhas.codigo_linha"),
            nullable=False,
        ),
        sa.Column("data_servico", sa.Date(), nullable=False),
        sa.Column(  # circular = 0
            "sentido",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido IN (0, 1)"),
            nullable=False,
        ),
        sa.Column(
            "hora",
            sa.SmallInteger(),
            sa.CheckConstraint("hora BETWEEN 0 AND 23"),
            nullable=False,
        ),
        sa.Column(
            "tipo_dia", sa.Text(), _check_in("tipo_dia", "U", "S", "D"), nullable=False
        ),
        sa.Column(
            "periodo",
            sa.Text(),
            sa.Computed(
                "CASE"
                " WHEN hora BETWEEN 3 AND 5 THEN 'APM'"
                " WHEN hora BETWEEN 6 AND 8 THEN 'PM'"
                " WHEN hora BETWEEN 9 AND 16 THEN 'EP'"
                " WHEN hora BETWEEN 17 AND 19 THEN 'PT'"
                " WHEN hora BETWEEN 20 AND 23 THEN 'PN'"
                " ELSE 'PPN' END",
                persisted=True,
            ),
        ),
        sa.Column(  # só saídas avaliáveis
            "programadas",
            sa.SmallInteger(),
            sa.CheckConstraint("programadas > 0"),
            nullable=False,
        ),
        sa.Column(  # pode passar do programado
            "realizadas",
            sa.SmallInteger(),
            sa.CheckConstraint("realizadas >= 0"),
            nullable=False,
        ),
        sa.Column(
            "quase_completas",
            sa.SmallInteger(),
            sa.CheckConstraint("quase_completas >= 0"),
            server_default=sa.text("0"),
            nullable=False,
        ),
        sa.Column(
            "inconclusivas",
            sa.SmallInteger(),
            sa.CheckConstraint("inconclusivas BETWEEN 0 AND programadas"),
            server_default=sa.text("0"),
            nullable=False,
        ),
        _execucao(),
        sa.PrimaryKeyConstraint("codigo_linha", "data_servico", "sentido", "hora"),
        schema="indicadores",
    )
    op.create_index(
        "icv_faixas_mes_idx", "icv_faixas", [sa.text(MES)], schema="indicadores"
    )
    op.create_index(
        "icv_faixas_execucao_idx",
        "icv_faixas",
        ["execucao_id"],
        schema="indicadores",
    )

    # Percentual com 2 casas; NULL quando não há denominador. O corpo é
    # idêntico ao do esquema.sql, porque o Postgres guarda o texto como veio.
    op.execute(
        "CREATE FUNCTION indicadores.pct(parte numeric, total numeric)"
        " RETURNS numeric\n"
        "LANGUAGE sql IMMUTABLE PARALLEL SAFE AS"
        " $$ SELECT round(100 * parte / NULLIF(total, 0), 2) $$"
    )

    # Base das views de IPP: cada saída com as marcações da regra ipp-0.2.0
    # (R/07_auditoria.R, indicadores_ipp):
    #   (a) ipp:   realizada_no_horario / válidas
    #   (b) ipp_b: saiu (completa ou não) com |dif| <= tolerancia_min,
    #              + inconclusivas
    #   (c) ipp_c: como (b), com tolerancia_ampliada_min
    op.execute(
        f"""
CREATE VIEW indicadores.saidas_classificadas AS
SELECT a.saida_id, a.codigo_linha, a.data_servico,
       date_trunc('month', a.data_servico::timestamp)::date AS mes,
       s.sentido, s.tipo_dia,
       extract(hour FROM s.partida_prevista AT TIME ZONE 'America/Sao_Paulo')
         ::smallint AS hora_prevista,
       a.status,
       a.status NOT IN ('programacao_invalida', 'fora_do_periodo_coletado')
         AS valida,
       a.status = 'realizada_no_horario' AS no_horario,
       a.status = 'realizada_com_atraso' AS com_atraso,
       a.status = 'realizada_antecipadamente' AS antecipada,
       a.status = 'viagem_quase_completa' AS quase_completa,
       a.status = 'partida_sem_viagem_completa' AS sem_viagem_completa,
       a.status = 'possivelmente_nao_realizada' AS nao_realizada,
       a.status = 'inconclusiva' AS inconclusiva,
       coalesce(a.status IN ({REALIZADAS},
                             'partida_sem_viagem_completa', 'viagem_quase_completa')
                AND abs(a.diferenca_min) <= a.tolerancia_min, false)
         OR a.status = 'inconclusiva' AS pontual_b,
       coalesce(a.status IN ({REALIZADAS},
                             'partida_sem_viagem_completa', 'viagem_quase_completa')
                AND abs(a.diferenca_min) <= a.tolerancia_ampliada_min, false)
         OR a.status = 'inconclusiva' AS pontual_c
  FROM indicadores.saidas_auditadas a
  JOIN operacao.saidas_programadas s ON s.id = a.saida_id
"""
    )
    for view, keys in IPP_VIEWS.items():
        op.execute(
            f"CREATE VIEW indicadores.{view} AS\nSELECT {keys},{IPP_COLUMNS}\n"
            f"  FROM indicadores.saidas_classificadas\n GROUP BY {keys}"
        )
    for view, query in ICV_VIEWS.items():
        op.execute(f"CREATE VIEW indicadores.{view} AS{query}")


def downgrade() -> None:
    for view in reversed(ICV_VIEWS):
        op.execute(f"DROP VIEW indicadores.{view}")
    for view in reversed(IPP_VIEWS):
        op.execute(f"DROP VIEW indicadores.{view}")
    op.execute("DROP VIEW indicadores.saidas_classificadas")
    op.execute("DROP FUNCTION indicadores.pct(numeric, numeric)")
    for table in (
        "icv_faixas",
        "saidas_lacunas",
        "partidas_descartadas",
        "saidas_auditadas",
    ):
        op.drop_table(table, schema="indicadores")
