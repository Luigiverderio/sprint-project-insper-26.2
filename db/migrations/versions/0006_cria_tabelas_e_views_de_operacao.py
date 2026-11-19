"""cria tabelas e views de operacao

Bloco "operacao" de docs/modelo_banco/esquema.sql: o resultado do worker
(validado com as saídas do pipeline R) e as views do mapa ao vivo e da saúde
da coleta.

Revision ID: 0006
Revises: 0005
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)
VIEWS = ("posicoes_atuais", "lacunas_coleta", "saude_coleta", "saude_particoes")


def _id() -> sa.Column[object]:
    return sa.Column(
        "id", sa.Uuid(), server_default=sa.text("public.uuid_v7()"), primary_key=True
    )


def _linha(*, nullable: bool = False) -> sa.Column[str]:
    return sa.Column(
        "codigo_linha",
        sa.Text(),
        sa.ForeignKey("referencia.linhas.codigo_linha"),
        nullable=nullable,
    )


def _execucao() -> sa.Column[object]:
    """Execução que gerou a linha: apagar uma execução refeita usa o índice."""
    return sa.Column(
        "execucao_id",
        sa.Uuid(),
        sa.ForeignKey("operacao.execucoes.id"),
        nullable=False,
    )


def _check_in(column: str, *values: str) -> sa.CheckConstraint:
    listed = ", ".join(f"'{value}'" for value in values)
    return sa.CheckConstraint(f"{column} IN ({listed})")


def _non_negative(column: str) -> sa.Column[float]:
    return sa.Column(column, sa.Double(), sa.CheckConstraint(f"{column} >= 0"))


def _fraction(column: str) -> sa.Column[float]:
    return sa.Column(
        column, sa.Double(), sa.CheckConstraint(f"{column} BETWEEN 0 AND 1")
    )


def upgrade() -> None:
    op.create_table(
        "execucoes",
        _id(),
        sa.Column(
            "etapa",
            sa.Text(),
            _check_in("etapa", "mapmatching", "auditoria", "frota", "icv"),
            nullable=False,
        ),
        sa.Column("iniciada_em", TIMESTAMPTZ, nullable=False),
        sa.Column("concluida_em", TIMESTAMPTZ),
        sa.Column(
            "status",
            sa.Text(),
            _check_in("status", "rodando", "ok", "erro"),
            nullable=False,
        ),
        # Ex.: 'mm-ciclo-0.1.0' e 'ipp-0.2.0'.
        sa.Column("versao_algoritmo", sa.Text(), nullable=False),
        sa.Column("versao_regra", sa.Text()),
        # Os limiares usados (tolerância, raio...).
        sa.Column("parametros", postgresql.JSONB(), nullable=False),
        _linha(nullable=True),
        sa.Column("data_inicio", sa.Date()),
        sa.Column("data_fim", sa.Date()),
        sa.Column("erro", sa.Text()),
        sa.CheckConstraint("concluida_em IS NULL OR concluida_em >= iniciada_em"),
        sa.CheckConstraint(
            "data_fim IS NULL OR data_inicio IS NULL OR data_fim >= data_inicio"
        ),
        sa.CheckConstraint("status <> 'erro' OR erro IS NOT NULL"),
        schema="operacao",
    )

    # A programação que vale em cada dia: o worker escolhe a coleta válida mais
    # próxima e materializa aqui. É a base da auditoria.
    op.create_table(
        "saidas_programadas",
        _id(),
        _linha(),
        sa.Column("data_servico", sa.Date(), nullable=False),
        sa.Column(
            "sentido",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido IN (0, 1)"),
            nullable=False,
        ),
        sa.Column(
            "tipo_dia", sa.Text(), _check_in("tipo_dia", "U", "S", "D"), nullable=False
        ),
        sa.Column("ordem", sa.SmallInteger(), nullable=False),
        sa.Column("partida_prevista", TIMESTAMPTZ, nullable=False),
        sa.Column(
            "coleta_id",
            sa.Integer(),
            sa.ForeignKey("referencia.coletas_programacao.id"),
            nullable=False,
        ),
        # Era schedule_quality_flag.
        sa.Column("coleta_posterior_a_data", sa.Boolean(), nullable=False),
        sa.UniqueConstraint("codigo_linha", "data_servico", "sentido", "ordem"),
        # Alvo da FK composta de indicadores.saidas_auditadas.
        sa.UniqueConstraint("id", "codigo_linha", "data_servico"),
        schema="operacao",
    )

    op.create_table(
        "viagens",
        _id(),
        _linha(),
        sa.Column("prefixo", sa.Integer(), nullable=False),
        sa.Column("data_servico", sa.Date(), nullable=False),
        sa.Column(  # circular = 0
            "sentido",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido IN (0, 1)"),
            nullable=False,
        ),
        sa.Column("inicio_obs", TIMESTAMPTZ, nullable=False),
        sa.Column("fim_obs", TIMESTAMPTZ, nullable=False),
        sa.Column("partida_em", TIMESTAMPTZ),
        sa.Column("chegada_em", TIMESTAMPTZ),
        _non_negative("duracao_min"),
        _non_negative("distancia_km"),
        _non_negative("vel_media_kmh"),
        _fraction("cobertura"),  # observation_coverage
        _fraction("frac_tracado"),  # frac_observada
        sa.Column(
            "lacuna_max_s", sa.Integer(), sa.CheckConstraint("lacuna_max_s >= 0")
        ),
        sa.Column(
            "status_trajetoria",
            sa.Text(),
            _check_in(
                "status_trajetoria",
                "viagem_confirmada",
                "viagem_provavel",
                "trajetoria_parcial",
                "observacao_inconclusiva",
            ),
            nullable=False,
        ),
        sa.Column(
            "nivel_confianca",
            sa.Text(),
            _check_in("nivel_confianca", "alta", "media", "baixa", "muito_baixa"),
            nullable=False,
        ),
        sa.Column(
            "aderencia_rota",
            sa.Text(),
            _check_in(
                "aderencia_rota", "aderente", "aderente_com_ruido", "baixa_aderencia"
            ),
        ),
        sa.Column(
            "evidencia_origem",
            sa.Text(),
            _check_in(
                "evidencia_origem",
                "observado",
                "estimado_inicio_cadeia",
                "iniciou_apos_origem",
                "interpolado_lacuna",
                "estimado_terminal",
            ),
        ),
        sa.Column(
            "evidencia_destino",
            sa.Text(),
            _check_in(
                "evidencia_destino",
                "observado",
                "terminou_antes_destino",
                "interpolado_lacuna",
                "estimado_fim_cadeia",
                "estimado_terminal",
            ),
        ),
        sa.Column(  # ex.: {sem_partida_confirmada}
            "alertas",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
        sa.Column(
            "gtfs_versao_id",
            sa.Integer(),
            sa.ForeignKey("referencia.gtfs_versoes.id"),
            nullable=False,
        ),
        _execucao(),
        sa.CheckConstraint("fim_obs >= inicio_obs"),
        sa.CheckConstraint(
            "chegada_em IS NULL OR partida_em IS NULL OR chegada_em >= partida_em"
        ),
        schema="operacao",
    )
    op.create_index(
        "viagens_linha_dia_idx",
        "viagens",
        ["codigo_linha", "data_servico"],
        schema="operacao",
    )
    op.create_index(
        "viagens_prefixo_dia_idx",
        "viagens",
        ["prefixo", "data_servico"],
        schema="operacao",
    )
    op.create_index(
        "viagens_execucao_idx", "viagens", ["execucao_id"], schema="operacao"
    )

    # ATENÇÃO ao volume: nos dados de teste são ~168 mil passagens por
    # linha-mês; no sistema inteiro passaria de 200 milhões por mês. Decidir
    # particionamento ou retenção curta antes de ligar para todas as linhas.
    op.create_table(
        "passagens",
        sa.Column(
            "viagem_id",
            sa.Uuid(),
            sa.ForeignKey("operacao.viagens.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequencia", sa.SmallInteger(), nullable=False),
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column("passou_em", TIMESTAMPTZ, nullable=False),
        sa.Column(
            "metodo",
            sa.Text(),
            _check_in("metodo", "interpolado", "interpolado_sobre_lacuna"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("viagem_id", "sequencia"),
        schema="operacao",
    )

    op.create_table(
        "lacunas_veiculo",
        _id(),
        _linha(),
        sa.Column("prefixo", sa.Integer(), nullable=False),
        sa.Column("data_servico", sa.Date(), nullable=False),
        sa.Column("inicio_em", TIMESTAMPTZ, nullable=False),
        # NULL = não voltou a transmitir.
        sa.Column("fim_em", TIMESTAMPTZ),
        sa.Column("km_inicio", sa.Double()),
        sa.Column("km_fim", sa.Double()),
        _non_negative("viagens_cabiveis"),
        sa.Column(
            "tipo",
            sa.Text(),
            _check_in(
                "tipo",
                "parou_de_transmitir",
                "deslocou_sem_sinal",
                "parado_sem_sinal",
                "cabe_viagem_sem_sinal",
            ),
            nullable=False,
        ),
        _execucao(),
        sa.CheckConstraint("fim_em IS NULL OR fim_em >= inicio_em"),
        schema="operacao",
    )
    op.create_index(
        "lacunas_veiculo_linha_dia_idx",
        "lacunas_veiculo",
        ["codigo_linha", "data_servico"],
        schema="operacao",
    )
    op.create_index(
        "lacunas_veiculo_execucao_idx",
        "lacunas_veiculo",
        ["execucao_id"],
        schema="operacao",
    )

    # Mapa ao vivo (US22): última posição de cada veículo nos últimos 5 minutos.
    # A API lê esta view sem acesso ao bruto (a view lê com os direitos do
    # dono). O limite no futuro tira GPS com relógio adiantado.
    op.execute(
        """
CREATE VIEW operacao.posicoes_atuais AS
SELECT DISTINCT ON (p.prefixo)
       p.prefixo,
       l.letreiro                     AS codigo_linha,
       p.sentido_api,
       p.acessivel,
       p.horario_gps,
       ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326) AS geom
  FROM bruto.posicoes_gps p
  LEFT JOIN referencia.linhas_olhovivo l USING (cod_linha_sentido)
 WHERE p.horario_gps >  now() - interval '5 minutes'
   AND p.horario_gps <= now() + interval '2 minutes'
 ORDER BY p.prefixo, p.horario_gps DESC
"""
    )

    # Lacunas de coleta (US13): intervalos sem ciclo ok acima de 90 s.
    op.execute(
        """
CREATE VIEW operacao.lacunas_coleta AS
SELECT fonte, anterior AS inicio_lacuna, iniciado_em AS fim_lacuna,
       iniciado_em - anterior AS duracao
  FROM (SELECT fonte, iniciado_em,
               lag(iniciado_em) OVER (PARTITION BY fonte ORDER BY iniciado_em)
                 AS anterior
          FROM bruto.ciclos_coleta
         WHERE status = 'ok' AND fonte = 'olhovivo_posicao') s
 WHERE iniciado_em - anterior > interval '90 seconds'
"""
    )

    # Saúde da coleta (US30): último sucesso e último erro por fonte.
    op.execute(
        """
CREATE VIEW operacao.saude_coleta AS
SELECT fonte,
       max(concluido_em) FILTER (WHERE status = 'ok')  AS ultimo_ok,
       max(concluido_em) FILTER (WHERE status <> 'ok') AS ultimo_erro,
       count(*) FILTER (WHERE status <> 'ok'
                          AND concluido_em > now() - interval '1 hour')
         AS erros_ultima_hora
  FROM bruto.ciclos_coleta
 GROUP BY fonte
"""
    )

    # Saúde das partições: alertar quando ok = false. Dado na default quer dizer
    # GPS com horário fora das partições criadas; cobertura curta quer dizer
    # que a manutenção do pg_partman não está rodando.
    op.execute(
        r"""
CREATE VIEW operacao.saude_particoes AS
SELECT d.linhas_na_default, p.particoes, p.cobertura_ate,
       d.linhas_na_default = 0 AND p.cobertura_ate > now() + interval '1 month'
         AS ok
  FROM (SELECT count(*) AS linhas_na_default
          FROM bruto.posicoes_gps_default) d,
       (SELECT count(*) AS particoes,
               max(substring(pg_get_expr(c.relpartbound, c.oid)
                             FROM 'TO \(''([^'']+)''\)')::timestamptz)
                 AS cobertura_ate
          FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'bruto.posicoes_gps'::regclass
           AND c.relname <> 'posicoes_gps_default') p
"""
    )


def downgrade() -> None:
    for view in reversed(VIEWS):
        op.execute(f"DROP VIEW operacao.{view}")
    for table in (
        "lacunas_veiculo",
        "passagens",
        "viagens",
        "saidas_programadas",
        "execucoes",
    ):
        op.drop_table(table, schema="operacao")
