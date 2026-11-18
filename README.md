# Ônibus Backend

Backend da plataforma de auditoria cidadã dos ônibus de São Paulo.
A base usa Python 3.12, FastAPI e uv.

## Instalação

Instale Git e [uv](https://docs.astral.sh/uv/getting-started/installation/).
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
- src/onibus/processamento/ — processamento dos workers.
- src/onibus/config.py — configuração por pydantic-settings.
- db/init/, db/migrations/, db/seeds/ — inicialização, migrações e dados de teste.
- tests/ — testes automatizados.
- docs/adr-001.md — decisões de arquitetura do time.
- legado_r/ — espaço reservado para referências e gabaritos do parceiro.

O Docker Compose e os comandos de banco entram nos cards T4 a T6.
As dependências de banco e testes de integração já estão declaradas conforme o T1.

## Fluxo do time

Siga AGENTS.md, as skills em .agents/skills/ e docs/adr-001.md.
Crie branches a partir da develop atualizada, usando o id do card.
Antes de integrar, atualize a branch com origin/develop e rode as checagens.
A integração usa merge local --ff-only, mediante pedido humano.
Commits seguem Conventional Commits com descrição em português e rodapé Refs: T1.
