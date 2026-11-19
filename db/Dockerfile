# Banco local: Postgres 16 + PostGIS 3 + pg_partman 5, sem esquema.
#
# As tabelas vêm das migrações Alembic (`make migrate`, veja db/migrations/).
# É a mesma imagem de docs/modelo_banco/Dockerfile; mantenha as duas iguais.
# Por que não postgis/postgis: veja a P8 do docs/adr-001.md.
FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-postgis-3 \
      postgresql-16-postgis-3-scripts \
      postgresql-16-partman \
 && rm -rf /var/lib/apt/lists/*
