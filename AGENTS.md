# AGENTS.md

Regras para todo agente (Codex, Claude Code) e membro do time. Fonte única: regras mudam só aqui e nas skills de `.agents/skills/` (`.claude/skills/` só aponta para elas).

## Projeto
- Backend Python do projeto. Stack: FastAPI, PostgreSQL + PostGIS, Alembic, pytest.
- Comandos (TODO: confirmar quando o card T1 criar `pyproject.toml`/`Makefile`): `uv sync` · `make test` · `uv run ruff check .` · `uv run ruff format .`
- **Verde** = `uv run ruff check .`, `uv run ruff format --check .` e `make test` passando.
- Segredos só em `.env` (nunca ler, imprimir ou commitar). Variável nova → `.env.example` com valor fictício.
- Arquitetura (fonte: `docs/adr-001.md`): tabelas e colunas em português, `snake_case`; API versionada com prefixo `/v1`; processamento pesado (map-matching, indicadores) roda no worker, nunca dentro de uma requisição da API.

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
- Código em inglês, PEP 8, type hints em funções públicas.
- Requisito, schema ou contrato de API ambíguo → pergunte, não suponha.
- Não edite `AGENTS.md`, `CLAUDE.md` ou skills sem pedido explícito.

## Pronto quando
Verde na `develop` com push feito · teste cobrindo a mudança · commits no padrão · sem segredos no diff · revisado por colega · card em Done atendendo o critério de aceite.
