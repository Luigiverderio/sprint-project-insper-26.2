# Migrações do banco (Alembic)

Toda mudança no banco passa por uma migração desta pasta (ADR-001, D6). Ninguém altera o banco à mão: assim todo mundo tem o mesmo esquema.

## Comandos

```sh
make migrate        # aplica as migrações que faltam; num banco limpo, cria tudo
make migrate-down   # desfaz a última migração aplicada
uv run --locked alembic current   # revisão em que o banco está
uv run --locked alembic history   # lista das migrações
uv run --locked alembic revision -m "cria tabela x" --rev-id 0008   # migração nova
```

- A conexão é a do dono do banco, com os dados do `.env`: `POSTGRES_DB`, `POSTGRES_PASSWORD`, `DB_PORT` e, se o banco não for o do Docker, `POSTGRES_HOST` (padrão `127.0.0.1`). Rode sempre com o mesmo usuário: os privilégios padrão da 0002 só valem para tabelas criadas por ele.
- As migrações pendentes rodam numa transação só. Se uma falhar, nada fica aplicado.
- A primeira partição de `bruto.posicoes_gps` é a de 3 meses antes do mês atual. Para outra: `uv run --locked alembic -x inicio_particoes=2026-08-01 upgrade head`.
- `uv run --locked alembic upgrade head --sql` mostra o SQL sem conectar no banco.

As migrações 0001 a 0007 reproduzem o modelo validado no T5 (`docs/modelo_banco/esquema.sql`). O teste `tests/test_migrations.py` compara o `pg_dump` dos dois bancos. Daqui em diante, mudanças vão só para migrações novas, e o `esquema.sql` fica como registro do modelo validado.

## Convenção de nomes

- Arquivo `NNNN_<mensagem>.py`, por exemplo `0008_cria_tabela_x.py`. O `alembic revision` monta o nome sozinho a partir do `--rev-id` e do `-m`.
- `revision` = `NNNN`, sequencial, com 4 dígitos. `down_revision` = a migração anterior. Uma head só.
- Mensagem em português, minúscula, sem acento, com verbo no presente: `cria tabela x`, `adiciona coluna y em z`, `remove indice w`.
- Objetos do banco em português, `snake_case`, sem acento (ADR-001, D3). Índices: `<tabela>_<colunas ou uso>_idx`. Constraints sem nome explícito ficam com o nome padrão do Postgres.
- Duas branches criaram o mesmo número? Quem integra por último renumera a sua no rebase (arquivo, `revision` e `down_revision`). O `make test` falha enquanto a cadeia estiver quebrada.

## O que o revisor confere

Sem PRs, a revisão acontece com o card em Revision (ADR-001, D1). Numa migração, confira:

1. **Nome e cadeia.** Segue a convenção acima, e `make test` passa (o teste confere número, nome e head única).
2. **Uma mudança lógica por migração.** Mudança de esquema e carga de dados vão em migrações separadas.
3. **Migração integrada não muda.** Se já está na `develop`, a correção é uma migração nova.
4. **O downgrade desfaz o upgrade**, na ordem inversa. Se não dá para desfazer (dado apagado), o `downgrade` levanta erro explicando o porquê, em vez de `pass`.
5. **Ida e volta testada**, num banco limpo: `make migrate`, `make migrate-down` e `make migrate` de novo. Com o Docker rodando, o `make test` faz isso para todas as migrações.
6. **Regras do modelo (ADR-001, P10):** `text` com `CHECK` em vez de enum, `timestamptz` em UTC, `double precision` em vez de `real`, unidade no nome da coluna, ids de log e de resultado em UUID v7 (`public.uuid_v7()`).
7. **Permissões.** Tabela nova num schema existente herda os direitos sozinha. Schema novo precisa de `GRANT USAGE` e dos `ALTER DEFAULT PRIVILEGES`. Nenhum papel ganha `UPDATE`, `DELETE` ou `TRUNCATE` em `bruto`.
8. **Bruto só de inserção.** Tabela nova em `bruto` liga os triggers `bruto.bloquear_alteracao()`, de linha e de `TRUNCATE`, como na 0003.
9. **Tabelas grandes** (`bruto.posicoes_gps`, `operacao.passagens`). Nada que reescreva a tabela ou a trave por muito tempo. Índice novo em tabela grande usa `CREATE INDEX CONCURRENTLY` dentro de `op.get_context().autocommit_block()`.
10. **SQL em `op.execute`:** um comando por chamada (o asyncpg não aceita vários). Evite `:palavra` dentro do texto, porque o SQLAlchemy lê isso como parâmetro.
11. **Autogenerate não substitui a leitura.** Ainda não há modelos ORM, então ele está desligado (`env.py`). Quando houver, revise o arquivo gerado linha a linha. Ele não entende partições nem o pg_partman.
