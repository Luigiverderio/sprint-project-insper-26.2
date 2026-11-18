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

```sh
make up        # constrói e sobe banco, API e worker; espera ficarem saudáveis
make down      # para tudo (os dados do banco ficam no volume)
make logs      # acompanha os logs
make db-reset  # apaga os dados do banco e recria tudo do zero
```

- API: http://127.0.0.1:8000/docs. Mude API_PORT no .env se a porta estiver ocupada.
- Banco: PostgreSQL 17 + PostGIS + pg_partman em localhost:5432 (mude DB_PORT no .env se a porta estiver ocupada). Na primeira subida, os scripts de db/init/ criam extensões, schemas e papéis. Depois de mudar esses scripts ou as senhas do .env, rode make db-reset. O modelo está em docs/modelo-banco.md.
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
- db/Dockerfile, db/init/ — imagem do banco e scripts de criação (extensões, schemas e papéis).
- db/migrations/, db/seeds/ — migrações e dados de teste.
- Dockerfile, docker-compose.yml — imagem da API e do worker, e o ambiente local.
- tests/ — testes automatizados.
- docs/adr-001.md — decisões de arquitetura do time.
- legado_r/ — espaço reservado para referências e gabaritos do parceiro.

O modelo do banco está em docs/modelo-banco.md. As migrações Alembic entram no card T6.
As dependências de banco e testes de integração já estão declaradas conforme o T1.

## Fluxo do time

Siga AGENTS.md, as skills em .agents/skills/ e docs/adr-001.md.
Crie branches a partir da develop atualizada, usando o id do card.
Antes de integrar, atualize a branch com origin/develop e rode as checagens.
A integração usa merge local --ff-only, mediante pedido humano.
Commits seguem Conventional Commits com descrição em português e rodapé Refs: T1.
