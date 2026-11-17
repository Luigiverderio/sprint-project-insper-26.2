# AGENTS.md

Regras para todo agente (Codex, Claude Code) e membro do time. Fonte única: regras mudam só aqui.

## Projeto
- Backend Python. Ambiente em `.venv`, segredos em `.env` (nunca ler, imprimir ou commitar).
- Comandos (TODO: confirmar quando a stack fechar): `pip install -r requirements.txt` · `pytest` · `ruff check .` · `ruff format .`

## Branches
- `main`: protegida. Só recebe merge de `develop`, feito por humano.
- `develop`: integração e camada de teste. Base de todo trabalho; recebe só merges de branches de trabalho.
- Trabalho: `<tipo>/<descricao-kebab>` (ex.: `feat/cadastro-usuario`), criada a partir de `develop` atualizada.
- Sem PRs: a integração é por merge local + push.
- Proibido: commit direto em `main` ou `develop`; qualquer ação em `main`; `push --force` em `main`/`develop` (na própria branch, só `--force-with-lease`); apagar branches remotas.

## Fluxo
1. `git switch develop && git pull --ff-only` → `git switch -c <tipo>/<descricao>`
2. Antes de integrar: `git fetch && git rebase origin/develop`; testes e lint precisam passar.
3. Merge (só quando o humano pedir): `git switch develop && git pull --ff-only && git merge --ff-only <branch>`.
4. Testes e lint de novo **na `develop`**. Passou → `git push origin develop`. Falhou → não faça push; desfaça com `git reset --hard origin/develop` e corrija na branch.
5. Release (humano): `git switch main && git pull --ff-only && git merge --ff-only develop && git push origin main`, só com `develop` verde.

## Commits (Conventional Commits)
`<tipo>(<escopo>): <descrição>`
- Tipos: `feat` `fix` `refactor` `test` `docs` `style` `perf` `build` `ci` `chore` `revert`.
- Tipo/escopo em inglês minúsculo. Descrição em português, verbo no presente, minúscula, sem ponto, ≤72 caracteres.
- Reutilize escopos existentes (`git log --oneline`). Um commit = uma mudança lógica. Quebra de compatibilidade: `!` + rodapé `BREAKING CHANGE:`.
- Ex.: `feat(auth): adiciona login com JWT` · `fix(usuarios): corrige email duplicado`

## Regras de execução
- Commit e push só quando o humano pedir. Confira a branch atual antes.
- Siga os padrões já existentes no código. Altere só o escopo da tarefa, sem refatorar ou reformatar fora dele.
- Mudança de comportamento exige teste. Não desative testes para passar; se algo falhar, reporte.
- Dependência nova: só se necessária, registrada no arquivo de dependências, em commit `build(deps): ...`.
- Variável de ambiente nova → `.env.example` com valor fictício.
- Requisito, schema ou contrato de API ambíguo → pergunte, não suponha.
- Código em inglês (PEP 8, type hints em funções públicas). Testes em `tests/test_<modulo>.py`.
- Não edite `AGENTS.md`/`CLAUDE.md` sem pedido explícito.

## Pronto quando
Branch rebaseada sobre `develop` · teste novo cobrindo a mudança · commits no padrão · sem segredos no diff · merge em `develop` com testes e lint verdes nela · push feito.
