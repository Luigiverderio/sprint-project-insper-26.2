#!/usr/bin/env bash
# Cria o banco do zero em Docker: Postgres 16 + PostGIS + pg_partman, com a
# manutenção das partições ligada, o esquema aplicado e (opcional) os testes
# rodados num banco descartável.
#
#   POSTGRES_PASSWORD=... ./criar_banco.sh
#   POSTGRES_PASSWORD=... TESTAR=1 PORTA=5433 ./criar_banco.sh
#
# Variáveis (todas opcionais, menos a senha):
#   NOME=onibus-db          nome do container (e do volume de dados)
#   PORTA=5432              porta no host (só em 127.0.0.1)
#   BANCO=onibus            nome do banco
#   INICIO_PARTICOES=       mês da 1ª partição (AAAA-MM-01); padrão: 3 meses atrás
#   TESTAR=0                1 = roda teste.sql num banco <BANCO>_teste e apaga
#
# No Windows, rode no Git Bash. Para um Postgres que já existe (RDS etc.), não
# use este script: veja "Banco que já existe" no README.
set -euo pipefail
export MSYS_NO_PATHCONV=1   # Git Bash: não converter /tmp/... em caminho do Windows

cd "$(dirname "$0")"
NOME=${NOME:-onibus-db}
PORTA=${PORTA:-5432}
BANCO=${BANCO:-onibus}
INICIO_PARTICOES=${INICIO_PARTICOES:-}
TESTAR=${TESTAR:-0}
: "${POSTGRES_PASSWORD:?defina POSTGRES_PASSWORD (a senha do usuário postgres)}"

psql_c() { docker exec "$NOME" psql -U postgres -v ON_ERROR_STOP=1 "$@"; }

echo "== imagem"
docker build -q -t onibus-postgis-partman .

echo "== container $NOME (porta 127.0.0.1:$PORTA, volume $NOME-dados)"
docker run -d --name "$NOME" \
  -p "127.0.0.1:$PORTA:5432" \
  -e POSTGRES_PASSWORD="$POSTGRES_PASSWORD" -e POSTGRES_DB="$BANCO" \
  -v "$NOME-dados:/var/lib/postgresql/data" \
  onibus-postgis-partman \
  postgres -c shared_preload_libraries=pg_partman_bgw \
           -c pg_partman_bgw.dbname="$BANCO" \
           -c pg_partman_bgw.role=postgres \
           -c pg_partman_bgw.interval=3600 >/dev/null

# Espera pelo TCP: durante a inicialização o container sobe um servidor
# temporário só no socket, e rodar o esquema nele falharia no meio.
for _ in $(seq 1 60); do
  docker exec "$NOME" pg_isready -h 127.0.0.1 -U postgres -d "$BANCO" >/dev/null 2>&1 && break
  sleep 1
done
docker exec "$NOME" pg_isready -h 127.0.0.1 -U postgres -d "$BANCO" >/dev/null

docker cp esquema.sql "$NOME:/tmp/esquema.sql"
docker cp teste.sql "$NOME:/tmp/teste.sql"

if [ "$TESTAR" = "1" ]; then
  echo "== testes em ${BANCO}_teste"
  psql_c -d postgres -qc "CREATE DATABASE ${BANCO}_teste"
  psql_c -d "${BANCO}_teste" -q -v inicio_particoes=2026-08-01 -f /tmp/esquema.sql >/dev/null
  if saida=$(psql_c -d "${BANCO}_teste" -f /tmp/teste.sql 2>&1); then
    echo "$saida" | grep -E '^[0-9]+\. |TODOS'
    psql_c -d postgres -qc "DROP DATABASE ${BANCO}_teste"
  else
    echo "$saida" | tail -n 20
    echo "TESTES FALHARAM: o banco ${BANCO}_teste ficou no container para investigar." >&2
    exit 1
  fi
fi

echo "== esquema em $BANCO"
psql_c -d "$BANCO" -q ${INICIO_PARTICOES:+-v inicio_particoes="$INICIO_PARTICOES"} -f /tmp/esquema.sql >/dev/null
psql_c -d "$BANCO" -c "SELECT * FROM operacao.saude_particoes"

cat <<EOF
Pronto: postgresql://postgres:***@127.0.0.1:$PORTA/$BANCO

Próximo passo, criar um usuário de login por serviço (senha fora do repositório):
  docker exec -it $NOME psql -U postgres -d $BANCO
  CREATE ROLE api_app LOGIN PASSWORD '...' IN ROLE onibus_leitura;
  ALTER ROLE api_app SET statement_timeout = '10s';
  (idem coletor_app IN ROLE onibus_coletor e worker_app IN ROLE onibus_worker)
EOF
