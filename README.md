# Ônibus Backend

Backend da plataforma de auditoria cidadã dos ônibus de São Paulo.
A base usa Python 3.12, FastAPI e uv.

## Pré-requisitos

- Git
- [Docker Desktop](https://docs.docker.com/desktop/) (no Windows, com WSL2)
- [uv](https://docs.astral.sh/uv/getting-started/installation/), que instala o Python 3.12
- GNU Make
- Node 20+ (só para o front)

No Windows, uv e make saem pelo winget. Abra um terminal novo depois, para o PATH valer:

```powershell
winget install --id astral-sh.uv -e
winget install --id ezwinports.make -e
```

## Instalação

Na raiz deste repositório, execute:

```sh
uv sync --locked
```

Se o uv informar um erro de certificado UnknownIssuer, use uv --system-certs sync --locked
para aproveitar os certificados confiáveis do sistema.

O uv instala o Python 3.12 quando necessário e cria o ambiente .venv.
Copie .env.example para .env e ajuste as configurações locais.
No PowerShell:

```powershell
Copy-Item .env.example .env
```

Mantenha tokens e senhas em .env. Esse arquivo fica fora do Git.

## Ambiente com Docker

Antes da primeira vez, copie .env.example para .env e troque POSTGRES_PASSWORD por uma senha sua.
Sem ela, o Docker Compose não sobe.

```sh
make up            # constrói e sobe banco, API e worker; espera ficarem saudáveis
make migrate       # cria ou atualiza o esquema do banco (rode depois do make up)
make migrate-down  # desfaz a última migração
make seed          # carrega os dados de teste de db/seeds/ (rode depois do make migrate)
make down          # para tudo e mantém os dados do banco
make logs          # acompanha os logs
make psql          # abre o psql no banco do Docker
make db-reset      # APAGA os dados e recria o banco do zero, com todas as migrações
```

- API: http://127.0.0.1:8000/docs. Banco: 127.0.0.1:5432, usuário postgres, banco onibus e a senha do seu .env. Mude API_PORT e DB_PORT no .env se as portas estiverem ocupadas.
- O banco é o PostgreSQL 16 com PostGIS 3 e pg_partman 5, em UTC (ADR-001, P8, e ADR-002). Ele sobe vazio: o esquema vem das migrações Alembic de db/migrations/, aplicadas pelo make migrate. Os dados ficam no volume onibus_dados_postgres.
- As versões do banco ficam fixadas no db/Dockerfile: a imagem base pelo digest e os pacotes pela versão exata. Assim o build dá o mesmo banco em qualquer máquina. Para atualizar, siga os passos do próprio arquivo.
- Toda mudança no esquema é uma migração nova. Depois de atualizar a branch, rode make migrate. Convenções e checklist de revisão: db/migrations/README.md.
- Banco local criado antes do T6 (com o esquema aplicado na primeira subida): rode make db-reset uma vez. Ele apaga os dados, que por enquanto são só de teste, e recria o banco pelas migrações.
- O pg_partman cria as partições dos meses seguintes sozinho, a cada hora, dentro do próprio banco. Confira com make psql e SELECT * FROM operacao.saude_particoes (ok deve ser true).
- O banco cria os papéis de grupo `onibus_coletor`, `onibus_worker` e `onibus_leitura`, sem login. Cada serviço conecta com um login próprio (veja Executar a API e Coletar da Olho Vivo); o usuário postgres é só para as migrações.
- O make seed aplica os .sql de db/seeds/ em ordem, cada um numa transação. Por enquanto a pasta não tem seeds; as convenções estão em db/seeds/README.md.
- A API e o worker voltam sozinhos se o processo cair. O worker não tem porta: o healthcheck confere um arquivo que ele atualiza a cada 10 s, e o make up só termina com os três saudáveis.
- O código entra na imagem, sem bind mount. Depois de mudar o código, rode make up de novo, que reconstrói só o que mudou. Para iterar rápido na API, use make dev fora do Docker.
- O front tem o próprio Docker no repositório dele e chama a API em http://localhost:8000.

## Executar a API

No ambiente local, suba o banco, aplique o esquema com `make migrate` e abra
o `psql` com `make psql`.
Crie o login da API uma vez por ambiente e defina a senha quando o `psql` solicitar.

```sql
CREATE ROLE api_app LOGIN IN ROLE onibus_leitura;
ALTER ROLE api_app SET statement_timeout = '10s';
ALTER ROLE api_app SET default_transaction_read_only = on;
\password api_app
```

O `statement_timeout` segue o modelo do banco (`docs/modelo_banco`). A API ainda
corta cada consulta em 3 s do lado do cliente.

Defina a mesma senha em `APP_API_DATABASE_PASSWORD` no seu `.env`. Sem ela, a API
sobe, registra um aviso no log e o `/health` responde 503.
Se a API estiver no Compose, aplique a configuração com `docker compose up -d api`.
Se executar a API diretamente na máquina, configure `APP_DATABASE_HOST`,
`APP_DATABASE_PORT` e `APP_DATABASE_NAME` para o banco publicado pelo Compose.
Configure `APP_CORS_ORIGINS` como uma lista JSON das origens permitidas do frontend.

```sh
uv run --locked uvicorn onibus.api.main:create_app --factory --reload
```

Abra a documentação em http://127.0.0.1:8000/docs.
As rotas públicas usam o prefixo `/v1`. O endpoint operacional `GET /health`
fica na raiz e responde com HTTP 200 quando a API consegue consultar o banco.
`last_collection_at` traz o horário de conclusão do último ciclo de posições
do Olho Vivo, inclusive em caso de falha na coleta.
`last_successful_collection_at` traz o do último ciclo com sucesso. Se ele ficar
para trás de `last_collection_at`, o coletor está rodando, mas falhando.
Os dois valores são `null` até o primeiro ciclo registrado. Se o banco estiver
indisponível ou o login ainda não estiver configurado, o endpoint retorna HTTP 503.

Os erros da API usam o formato `{"error": {"code": "...", "message": "..."}}`.

## Carregar o GTFS

Com o banco migrado (`make up` e `make migrate`), carregue o GTFS da SPTrans:

```sh
make load-gtfs GTFS=caminho/do/gtfs.zip
```

- Cada `.zip` vira uma versão em `referencia.gtfs_versoes`, com o hash do arquivo, e as linhas de todas as tabelas `gtfs_*` levam o id da versão. Versões diferentes convivem. O mesmo arquivo duas vezes não faz nada.
- O nome da versão é `AAAAMMDD` da data da captura em São Paulo (`-2`, `-3`... se repetir no mesmo dia). Para escolher: `--name`; para informar quando o arquivo foi capturado: `--captured-at` (ISO 8601). Esses dois só funcionam rodando direto: `uv run --locked python -m onibus.coleta.gtfs ARQUIVO.zip --name feed-novembro`.
- O horário do `stop_times.txt` é relativo ao início da viagem, com uma base qualquer por viagem. A carga guarda só o deslocamento até a primeira parada, em segundos (ADR-001, P6). O GTFS inteiro carrega em cerca de 13 s.
- A carga é uma transação só: se algo falhar, nada fica gravado. O fuso do `agency.txt` precisa ser `America/Sao_Paulo`.
- Roda como o dono do banco, com os dados do `.env`, como as migrações. No EC2, o ideal é rodar como `coletor_app` (T14).

## Coletar da Olho Vivo

A coleta contínua grava no banco com um login próprio, que só insere no bruto.
Crie-o uma vez por ambiente com `make psql` e defina a senha quando o `psql` pedir:

```sql
CREATE ROLE coletor_app LOGIN IN ROLE onibus_coletor;
\password coletor_app
```

Coloque a mesma senha em `APP_COLETOR_DATABASE_PASSWORD` e o token da API em
`APP_OLHO_VIVO_TOKEN`, no .env, e rode:

```sh
uv run --locked python -m onibus.coleta             # um ciclo só, para conferir o token
uv run --locked python -m onibus.coleta --schedule  # um ciclo a cada 30 s, grava no banco
```

Cada ciclo faz até 5 tentativas, com espera crescente, e autentica de novo se a sessão expirar.
O ciclo tem prazo de 25 s: se a API travar, ele termina como falha antes do próximo disparo.
Se mesmo assim um ciclo passar de 30 s, o disparo seguinte é pulado e fica no log, sem ciclos em paralelo.
Depois de 10 minutos seguidos de falha, o log registra um alerta CRITICAL.
Cada ciclo vai para o log com a duração total e a de cada etapa (`login`, `request`, `parse`, `retry_wait`, `write`).
Com `--schedule`, cada ciclo, com sucesso ou falha, vira uma linha em `bruto.ciclos_coleta`, com as durações das etapas, e as posições vão para `bruto.posicoes_gps` na mesma transação.
Posições repetidas (mesmo veículo e horário de GPS) são ignoradas; coordenada ou sentido inválido é descartado e contado em `n_rejeitados`.
Sem `APP_COLETOR_DATABASE_PASSWORD`, o `--schedule` não sobe. O ciclo único não grava nada.
Fora do Compose, `APP_DATABASE_HOST` e `APP_DATABASE_PORT` apontam para o banco, como na API.

## Qualidade

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

Com GNU Make instalado, use make test e make lint.
O `make test` exige o Docker em execução. O Testcontainers constrói a imagem de
banco do projeto, inicia um PostgreSQL descartável e aplica as migrações.
No PowerShell, `uv run --locked pytest --require-docker` executa a mesma suíte.
Para rodar os testes sem banco, use `uv run --locked pytest` com o Docker
desligado. Nesse modo, os testes de integração são pulados.
Para medir a cobertura, acrescente `--cov=onibus` ao comando do pytest.
Se o Windows bloquear o mypy.exe ("política de Controle de Aplicativo"), use uv run --locked python -m mypy, que é o que o make lint faz.
Para formatar, execute uv run --locked ruff format .

Instale os hooks locais depois de uv sync:

```sh
uv run --locked pre-commit install
uv run --locked pre-commit run --all-files
```

Os hooks usam a versão do Ruff instalada pelo uv.lock.

## Estrutura

- src/onibus/api/ — aplicação HTTP.
- src/onibus/db/ — acesso ao banco.
- src/onibus/coleta/ — clientes e rotinas de coleta.
- src/onibus/processamento/ — processamento dos workers (python -m onibus.processamento).
- src/onibus/config.py — configuração por pydantic-settings.
- db/Dockerfile — imagem do banco (local, testes e produção), sem esquema. db/migrations/ — migrações Alembic (alembic.ini na raiz). db/init/ — inicialização. db/seeds/ — dados de teste do make seed.
- docs/modelo_banco/ — modelo do banco: esquema, testes e validação (veja o README dessa pasta).
- Dockerfile, docker-compose.yml — imagem da API e do worker, e o ambiente local.
- tests/ — testes automatizados.
- docs/adr-001.md — decisões de arquitetura do time.
- legado_r/ — espaço reservado para referências e gabaritos do parceiro.

O esquema do banco vem das migrações Alembic (T6), que reproduzem o modelo do T5 (docs/modelo_banco).
As dependências de banco e testes de integração já estão declaradas conforme o T1.

## Fluxo do time

Siga AGENTS.md, as skills em .agents/skills/ e docs/adr-001.md.
Crie branches a partir da develop atualizada, usando o id do card.
Antes de integrar, atualize a branch com origin/develop e rode as checagens.
A integração usa merge local --ff-only, mediante pedido humano.
Commits seguem Conventional Commits com descrição em português e rodapé Refs: T1.
