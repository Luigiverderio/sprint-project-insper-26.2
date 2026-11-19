# Banco: Postgres 16 + PostGIS 3 + pg_partman 5, sem esquema.
#
# Única imagem do banco: make up, testes (tests/conftest.py),
# docs/modelo_banco/criar_banco.sh e produção no EC2 (ADR-003).
# As tabelas vêm das migrações Alembic (`make migrate`, veja db/migrations/).
# Por que não postgis/postgis: veja a P8 do docs/adr-001.md.
#
# Versões fixadas, para o build dar o mesmo banco em qualquer máquina e data:
# a imagem base pelo digest e os pacotes do PGDG pela versão exata, inclusive
# GEOS, PROJ, GDAL e SFCGAL, que mudam o resultado das funções espaciais.
# O repositório principal do PGDG só guarda as versões recentes. O de arquivo
# (apt-archive) guarda todas, então a versão fixada continua instalável.
#
# Para atualizar:
#   1. digest: docker buildx imagetools inspect postgres:16-bookworm
#   2. versões: tire as versões abaixo, faça o build e liste os pacotes do PGDG:
#      docker run --rm <imagem> dpkg-query -W -f '${Package}=${Version}\n' | grep pgdg
#   3. make db-reset e make test.
FROM postgres:16.16-bookworm@sha256:ce3efec6bc404f6b15ebf5d9bed6b45a9d4655aea238d7dacc3467ccb05a1175

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates \
 && echo "deb [ signed-by=/usr/local/share/keyrings/postgres.gpg.asc ] https://apt-archive.postgresql.org/pub/repos/apt bookworm-pgdg-archive main" \
      > /etc/apt/sources.list.d/pgdg-archive.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends \
      postgresql-16-postgis-3=3.7.0+dfsg-1.pgdg12+2 \
      postgresql-16-postgis-3-scripts=3.7.0+dfsg-1.pgdg12+2 \
      postgresql-16-partman=5.5.0-1.pgdg12+2 \
      libgeos-c1t64=3.15.0-2.pgdg12+1 \
      libgeos3.15.0=3.15.0-2.pgdg12+1 \
      libproj25=9.9.0-1.pgdg12+1 \
      proj-data=9.9.0-1.pgdg12+1 \
      libgdal39=3.13.3+dfsg-2.pgdg12+1 \
      gdal-data=3.13.3+dfsg-2.pgdg12+1 \
      gdal-plugins=3.13.3+dfsg-2.pgdg12+1 \
      libsfcgal2=2.3.0-1.pgdg12+1 \
      python3-psycopg2=2.9.10-1.pgdg120+1 \
 && rm -rf /var/lib/apt/lists/*
