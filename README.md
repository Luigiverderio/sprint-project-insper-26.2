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
- Os papéis onibus_coletor, onibus_worker e onibus_leitura existem sem login. Os usuários de cada serviço (api_app etc.) ainda não são criados: a API e o worker não usam o banco por enquanto, e o usuário postgres é só para desenvolvimento.
- O make seed aplica os .sql de db/seeds/ em ordem, cada um numa transação. Por enquanto a pasta não tem seeds; as convenções estão em db/seeds/README.md.
- A API e o worker voltam sozinhos se o processo cair. O worker não tem porta: o healthcheck confere um arquivo que ele atualiza a cada 10 s, e o make up só termina com os três saudáveis.
- O código entra na imagem, sem bind mount. Depois de mudar o código, rode make up de novo, que reconstrói só o que mudou. Para iterar rápido na API, use make dev fora do Docker.
- O front tem o próprio Docker no repositório dele e chama a API em http://localhost:8000.

## Executar a API

```sh
uv run --locked uvicorn onibus.api.main:create_app --factory --reload
```

Abra a documentação em http://127.0.0.1:8000/docs.
O módulo expõe uma app factory para criar instâncias isoladas nos testes.
A base do T1 oferece inicialização e documentação OpenAPI.
A conexão com o banco, /health, CORS e rotas /v1 entram no T10.

## Coletar da Olho Vivo

Coloque o token da API no .env (APP_OLHO_VIVO_TOKEN) e rode:

```sh
uv run --locked python -m onibus.coleta             # um ciclo só, para conferir o token
uv run --locked python -m onibus.coleta --schedule  # um ciclo a cada 30 s, até Ctrl+C
```

Cada ciclo faz até 5 tentativas, com espera crescente, e autentica de novo se a sessão expirar.
O ciclo tem prazo de 25 s: se a API travar, ele termina como falha antes do próximo disparo.
Se mesmo assim um ciclo passar de 30 s, o disparo seguinte é pulado e fica no log, sem ciclos em paralelo.
Depois de 10 minutos seguidos de falha, o log registra um alerta CRITICAL.
Cada ciclo vai para o log com a duração total e a de cada etapa (`login`, `request`, `parse`, `retry_wait`).
Por enquanto o ciclo não grava no banco: o registro na tabela de ciclos é o T8.

## Qualidade

```sh
uv run --locked pytest
uv run --locked ruff check .
uv run --locked ruff format --check .
uv run --locked mypy
```

Com GNU Make instalado, use make test e make lint.
No Windows, os comandos uv acima também funcionam diretamente no PowerShell.
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
