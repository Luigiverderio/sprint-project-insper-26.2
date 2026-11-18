# Postgres 16 + PostGIS 3 + pg_partman 5, a partir da imagem oficial do Postgres.
#
# Por que não postgis/postgis: as tags 16-* dela são Debian bullseye, e o
# repositório PGDG do bullseye saiu do ar (404 em apt.postgresql.org), então não
# dá para instalar o pg_partman nela. No bookworm os pacotes PGDG existem.
#
# Usada pelo docker-compose (T4) e pelos testes com Testcontainers (T11).
# Depois do primeiro build, fixe as versões dos pacotes (apt-cache policy ...).
FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-postgis-3 \
      postgresql-16-postgis-3-scripts \
      postgresql-16-partman \
 && rm -rf /var/lib/apt/lists/*
