"""cria catalogos e gtfs

Blocos "T5/T8: catálogos de linhas" e "T8: GTFS versionado" de
docs/modelo_banco/esquema.sql.

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TIMESTAMPTZ = sa.DateTime(timezone=True)


def _gtfs_version() -> sa.Column[int]:
    """Primeira coluna das tabelas do GTFS: apagar a versão apaga tudo dela."""
    return sa.Column(
        "gtfs_versao_id",
        sa.Integer(),
        sa.ForeignKey("referencia.gtfs_versoes.id", ondelete="CASCADE"),
        nullable=False,
    )


def upgrade() -> None:
    # Linha canônica, uma por código, alimentada pelo catálogo da API de
    # itinerários; operacao e indicadores apontam para cá. '701A-10', mas o
    # catálogo também tem '209P-1', 'CPTM L07-0' e 'METRÔ L1-0': não dá para
    # impor um formato; só barra vazio e espaço nas pontas.
    op.create_table(
        "linhas",
        sa.Column(
            "codigo_linha",
            sa.Text(),
            sa.CheckConstraint(
                "codigo_linha <> '' AND codigo_linha = btrim(codigo_linha)"
            ),
            primary_key=True,
        ),
        sa.Column(
            "criada_em", TIMESTAMPTZ, server_default=sa.text("now()"), nullable=False
        ),
        schema="referencia",
    )

    # O que a Olho Vivo diz de cada "cl". Atualizada pelo coletor quando aparece
    # um cl novo (e, no máximo uma vez por hora, o visto_ultimo_em).
    op.create_table(
        "linhas_olhovivo",
        sa.Column(
            "cod_linha_sentido", sa.Integer(), primary_key=True, autoincrement=False
        ),  # "cl"
        sa.Column("letreiro", sa.Text(), nullable=False),  # "c", ex.: '701A-10'
        sa.Column(
            "sentido_api",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido_api IN (1, 2)"),
            nullable=False,
        ),
        sa.Column("letreiro_destino", sa.Text()),  # "lt0"
        sa.Column("letreiro_origem", sa.Text()),  # "lt1"
        sa.Column("visto_primeiro_em", TIMESTAMPTZ, nullable=False),
        sa.Column("visto_ultimo_em", TIMESTAMPTZ, nullable=False),
        sa.CheckConstraint("visto_ultimo_em >= visto_primeiro_em"),
        schema="referencia",
    )
    op.create_index(
        "linhas_olhovivo_letreiro_idx",
        "linhas_olhovivo",
        ["letreiro"],
        schema="referencia",
    )

    # Feriados: no R eram uma lista fixa em 00_config.R.
    op.create_table(
        "feriados",
        sa.Column("data", sa.Date(), primary_key=True),
        sa.Column("nome", sa.Text(), nullable=False),
        sa.Column(
            "tipo_dia_aplicado",
            sa.Text(),
            sa.CheckConstraint("tipo_dia_aplicado IN ('U', 'S', 'D')"),
            server_default=sa.text("'D'"),
            nullable=False,
        ),
        schema="referencia",
    )

    # Cada .zip capturado vira uma versão (o hash garante que não se repete).
    # Todas as tabelas do GTFS levam gtfs_versao_id na chave, então várias
    # versões convivem.
    op.create_table(
        "gtfs_versoes",
        sa.Column("id", sa.Integer(), sa.Identity(), primary_key=True),
        sa.Column("nome", sa.Text(), nullable=False, unique=True),  # '20261104'
        sa.Column(
            "hash_sha256",
            sa.Text(),
            sa.CheckConstraint("hash_sha256 ~ '^[0-9a-f]{64}$'"),
            nullable=False,
            unique=True,
        ),
        sa.Column("capturado_em", TIMESTAMPTZ, nullable=False),
        # Caminho ou chave do .zip guardado.
        sa.Column("arquivo_origem", sa.Text(), nullable=False),
        sa.Column("ciclo_id", sa.Uuid(), sa.ForeignKey("bruto.ciclos_coleta.id")),
        schema="referencia",
    )

    op.create_table(  # routes.txt
        "gtfs_rotas",
        _gtfs_version(),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("agency_id", sa.Text()),
        sa.Column("nome_curto", sa.Text()),  # route_short_name
        sa.Column("nome_longo", sa.Text()),  # route_long_name
        sa.Column("tipo_rota", sa.SmallInteger()),  # route_type (3 = ônibus)
        sa.Column("cor", sa.Text()),
        sa.Column("cor_texto", sa.Text()),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "route_id"),
        schema="referencia",
    )

    op.create_table(  # calendar.txt
        "gtfs_calendario",
        _gtfs_version(),
        sa.Column("service_id", sa.Text(), nullable=False),
        *(
            sa.Column(day, sa.Boolean(), nullable=False)
            for day in (
                "segunda",
                "terca",
                "quarta",
                "quinta",
                "sexta",
                "sabado",
                "domingo",
            )
        ),
        sa.Column("data_inicio", sa.Date(), nullable=False),
        sa.Column("data_fim", sa.Date(), nullable=False),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "service_id"),
        sa.CheckConstraint("data_fim >= data_inicio"),
        schema="referencia",
    )

    # shapes.txt vira UMA LineString por shape (em vez de um ponto por linha):
    # menos linhas e operações espaciais diretas (ST_LineLocatePoint etc.).
    op.create_table(
        "gtfs_tracados",
        _gtfs_version(),
        sa.Column("shape_id", sa.Text(), nullable=False),
        sa.Column(
            "geom",
            Geometry("LINESTRING", srid=4326, spatial_index=False),
            sa.CheckConstraint("ST_IsValid(geom)"),
            nullable=False,
        ),
        sa.Column(
            "comprimento_m",
            sa.Double(),
            sa.Computed("ST_Length(geom::geography)", persisted=True),
        ),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "shape_id"),
        schema="referencia",
    )
    op.create_index(
        "gtfs_tracados_geom_idx",
        "gtfs_tracados",
        ["geom"],
        schema="referencia",
        postgresql_using="gist",
    )

    op.create_table(  # stops.txt
        "gtfs_paradas",
        _gtfs_version(),
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column("nome", sa.Text()),
        sa.Column("descricao", sa.Text()),
        sa.Column(  # ST_X = lon, ST_Y = lat
            "geom",
            Geometry("POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "stop_id"),
        schema="referencia",
    )
    op.create_index(
        "gtfs_paradas_geom_idx",
        "gtfs_paradas",
        ["geom"],
        schema="referencia",
        postgresql_using="gist",
    )

    op.create_table(  # trips.txt
        "gtfs_viagens",
        sa.Column("gtfs_versao_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("route_id", sa.Text(), nullable=False),
        sa.Column("service_id", sa.Text()),
        sa.Column("letreiro", sa.Text()),  # trip_headsign
        sa.Column(  # direction_id
            "sentido",
            sa.SmallInteger(),
            sa.CheckConstraint("sentido IN (0, 1)"),
            nullable=False,
        ),
        sa.Column("shape_id", sa.Text()),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "trip_id"),
        sa.ForeignKeyConstraint(
            ["gtfs_versao_id", "route_id"],
            ["referencia.gtfs_rotas.gtfs_versao_id", "referencia.gtfs_rotas.route_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gtfs_versao_id", "shape_id"],
            [
                "referencia.gtfs_tracados.gtfs_versao_id",
                "referencia.gtfs_tracados.shape_id",
            ],
            ondelete="CASCADE",
        ),
        schema="referencia",
    )
    op.create_index(
        "gtfs_viagens_rota_idx",
        "gtfs_viagens",
        ["gtfs_versao_id", "route_id"],
        schema="referencia",
    )

    # stop_times.txt. No GTFS da SPTrans o horário é relativo ao início da
    # viagem, com uma base qualquer por viagem: só as diferenças valem. Aqui
    # fica o deslocamento até a primeira parada, em segundos (zero nela),
    # normalizado na carga. Não é hora do relógio e não se converte para UTC
    # (ADR-001, P6).
    op.create_table(
        "gtfs_horarios_paradas",
        sa.Column("gtfs_versao_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column("sequencia", sa.Integer(), nullable=False),  # stop_sequence
        sa.Column("stop_id", sa.Text(), nullable=False),
        sa.Column(  # arrival_time
            "chegada_s", sa.Integer(), sa.CheckConstraint("chegada_s >= 0")
        ),
        sa.Column(  # departure_time
            "partida_s", sa.Integer(), sa.CheckConstraint("partida_s >= 0")
        ),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "trip_id", "sequencia"),
        sa.ForeignKeyConstraint(
            ["gtfs_versao_id", "trip_id"],
            [
                "referencia.gtfs_viagens.gtfs_versao_id",
                "referencia.gtfs_viagens.trip_id",
            ],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["gtfs_versao_id", "stop_id"],
            [
                "referencia.gtfs_paradas.gtfs_versao_id",
                "referencia.gtfs_paradas.stop_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "partida_s IS NULL OR chegada_s IS NULL OR partida_s >= chegada_s"
        ),
        schema="referencia",
    )

    # frequencies.txt: o GTFS da SPTrans descreve a operação por intervalo.
    op.create_table(
        "gtfs_frequencias",
        sa.Column("gtfs_versao_id", sa.Integer(), nullable=False),
        sa.Column("trip_id", sa.Text(), nullable=False),
        sa.Column(  # start_time
            "inicio_s",
            sa.Integer(),
            sa.CheckConstraint("inicio_s >= 0"),
            nullable=False,
        ),
        sa.Column("fim_s", sa.Integer(), nullable=False),  # end_time
        sa.Column(  # headway_secs
            "intervalo_s",
            sa.Integer(),
            sa.CheckConstraint("intervalo_s > 0"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("gtfs_versao_id", "trip_id", "inicio_s"),
        sa.ForeignKeyConstraint(
            ["gtfs_versao_id", "trip_id"],
            [
                "referencia.gtfs_viagens.gtfs_versao_id",
                "referencia.gtfs_viagens.trip_id",
            ],
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("fim_s > inicio_s"),
        schema="referencia",
    )


def downgrade() -> None:
    for table in (
        "gtfs_frequencias",
        "gtfs_horarios_paradas",
        "gtfs_viagens",
        "gtfs_paradas",
        "gtfs_tracados",
        "gtfs_calendario",
        "gtfs_rotas",
        "gtfs_versoes",
        "feriados",
        "linhas_olhovivo",
        "linhas",
    ):
        op.drop_table(table, schema="referencia")
