# AGENTS.md

Regras para todo agente (Codex, Claude Code) e membro do time. Fonte única: regras mudam só aqui e nas skills de `.agents/skills/` (`.claude/skills/` só aponta para elas).

## Projeto
- Backend Python do projeto. Stack: FastAPI, PostgreSQL + PostGIS + pg_partman, Alembic, pytest.
- Comandos: `uv sync` · `make test` · `make lint` (ruff check, ruff format --check e mypy) · `make format`.
- Docker local: `make up` (banco, API e worker) · `make down` · `make logs` · `make psql`.
- Banco: `make migrate` (aplica as migrações) · `make migrate-down` (desfaz a última) · `make db-reset` (**apaga os dados** e recria o banco do zero).
- **Verde** = `uv run ruff check .`, `uv run ruff format --check .` e `make test` passando.
- Segredos só em `.env` (nunca ler, imprimir ou commitar). Variável nova → `.env.example` com valor fictício.
- Arquitetura (fontes: `docs/adr-001.md` e `docs/adr-002.md`): tabelas e colunas em português, `snake_case`; API versionada com prefixo `/v1`; processamento pesado (map-matching, indicadores) roda no worker, nunca dentro de uma requisição da API.
- Banco: PostgreSQL 16 com PostGIS e `pg_partman`. Local, roda no Docker (`make up`). Produção, num **EC2 da AWS** (decisão do time em 19/11/2026; o plano está no card T13).
- O esquema do banco vem só das migrações Alembic em `db/migrations/`. As convenções e o checklist do revisor estão em `db/migrations/README.md`. `docs/modelo_banco/` é o modelo de referência validado (T5): o `esquema.sql` não muda mais, e mudança de esquema é migração nova.

## Branches
- `main`: release. Só recebe merge `--ff-only` de `develop` verde, quando o humano pedir.
- `develop`: integração. Base de todo trabalho.
- Trabalho: `<tipo>/<card>-<descricao-kebab>` criada a partir de `develop` atualizada; `<card>` = id do card em minúsculo (ex.: `feat/h1-coleta-posicoes`, `docs/t3-convencoes`).
- Sem PRs: integração por merge local + push.
- Proibido: commit direto em `main` ou `develop`; `push --force` nelas (na própria branch, só `--force-with-lease`); apagar branches remotas.

## Fluxo
1. `git switch develop && git pull --ff-only && git switch -c <branch>`. Card → Doing.
2. Pronto para integrar: `git fetch && git rebase origin/develop` e verde na branch. Card → Revision.
3. Merge só quando o humano pedir, com ou sem revisão: `git switch develop && git pull --ff-only && git merge --ff-only <branch>`.
4. Verde de novo **na `develop`** → `git push origin develop`. Falhou → sem push; `git reset --hard origin/develop` e corrija na branch.
5. O card só vai para Done depois da revisão de um colega; se o merge veio antes, ele espera em Revision.
6. Release (só a pedido do humano): `git switch main && git pull --ff-only && git merge --ff-only develop && git push origin main`.

## Commits (Conventional Commits)
`<tipo>(<escopo>): <descrição>` + rodapé `Refs: <card>` (ex.: `Refs: H1`, `Refs: T5`).
- Tipos: `feat` `fix` `refactor` `test` `docs` `style` `perf` `build` `ci` `chore` `revert`.
- Tipo/escopo em inglês minúsculo; descrição em português, verbo no presente, minúscula, sem ponto, ≤72 caracteres.
- Um commit = uma mudança lógica; reutilize escopos (`git log --oneline`). Quebra de compatibilidade: `!` + rodapé `BREAKING CHANGE:`.

## Regras de execução
- Toda mudança pertence a um card do quadro https://trello.com/b/qypssKqv/sprint-2semestre; sem card, pergunte. Antes de qualquer ação no Trello (mesmo só leitura), carregue a skill `processo-trello`.
- Commit e push só quando o humano pedir; confira a branch atual antes.
- Siga os padrões do código; altere só o escopo da tarefa, sem refatorar ou reformatar fora dele.
- Mudança de comportamento exige teste em `tests/test_<modulo>.py`. Não desative testes para passar; se algo falhar, reporte.
- Dependência nova só se necessária, em commit `build(deps): ...`.
- Mudança no banco (tabela, coluna, índice, papel) é uma migração nova em `db/migrations/versions/`, no formato `NNNN_<mensagem>.py`, com `downgrade` real. Nunca edite uma migração já integrada na `develop` e nunca altere o banco à mão.
- `make db-reset` e qualquer comando contra o banco de produção (EC2) só a pedido explícito do humano.
- Código em inglês, PEP 8, type hints em funções públicas.
- Requisito, schema ou contrato de API ambíguo → pergunte, não suponha.
- Não edite `AGENTS.md`, `CLAUDE.md` ou skills sem pedido explícito.

## Pronto quando
Verde na `develop` com push feito · teste cobrindo a mudança · commits no padrão · sem segredos no diff · revisado por colega · card em Done atendendo o critério de aceite.
