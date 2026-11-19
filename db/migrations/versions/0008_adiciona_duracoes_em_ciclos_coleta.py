"""adiciona duracoes em ciclos coleta

Duração de cada etapa do ciclo de coleta (card T12), em segundos, para saber
onde o tempo do ciclo vai sem depender do log. Cada etapa soma todas as
tentativas do ciclo. NULL quando a etapa não ocorreu no ciclo (ex.: login com
a sessão ainda aberta, gravação num ciclo com falha) ou para fontes que não
medem etapas.

Revision ID: 0008
Revises: 0007
Create Date: 2026-11-19
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

DURACOES = (
    "duracao_login_s",  # POST /Login/Autenticar
    "duracao_requisicao_s",  # GET até o corpo inteiro chegar
    "duracao_leitura_s",  # leitura do JSON e validação
    "duracao_espera_s",  # espera entre as tentativas
    "duracao_gravacao_s",  # inserção das posições no bruto
)


def upgrade() -> None:
    for coluna in DURACOES:
        op.add_column(
            "ciclos_coleta",
            sa.Column(coluna, sa.Double(), sa.CheckConstraint(f"{coluna} >= 0")),
            schema="bruto",
        )


def downgrade() -> None:
    for coluna in reversed(DURACOES):
        op.drop_column("ciclos_coleta", coluna, schema="bruto")
