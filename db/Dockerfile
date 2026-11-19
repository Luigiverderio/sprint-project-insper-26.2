# Banco local: Postgres 16 + PostGIS 3 + pg_partman 5, com o esquema já embutido.
#
# Contexto de build: docs/modelo_banco (veja o docker-compose.yml). A primeira
# parte é a mesma de docs/modelo_banco/Dockerfile; mantenha as duas iguais.
# Por que não postgis/postgis: veja a P8 do docs/adr-001.md.
#
# O Postgres roda os arquivos de /docker-entrypoint-initdb.d só na PRIMEIRA
# subida, com o volume de dados vazio. Para refazer o banco: `make db-reset`.
FROM postgres:16-bookworm

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-postgis-3 \
      postgresql-16-postgis-3-scripts \
      postgresql-16-partman \
 && rm -rf /var/lib/apt/lists/*

COPY esquema.sql /docker-entrypoint-initdb.d/10-esquema.sql
