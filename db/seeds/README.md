# Seeds: dados de teste do banco local

`make seed` aplica os arquivos `.sql` desta pasta no banco do Docker, em ordem de nome. Ele usa a conexão do `make migrate` (o dono do banco, com os dados do `.env`). Rode depois do `make migrate`.

- Nome: `NNNN_<descricao>.sql`, por exemplo `0001_linhas_de_teste.sql`. O número define a ordem.
- Cada arquivo roda numa transação. Se um falhar, nada dele fica aplicado e os seguintes não rodam.
- SQL puro: sem comandos do psql (`\set`, `\copy`) e sem `BEGIN`/`COMMIT`.
- O seed pode rodar de novo sem erro: use `ON CONFLICT DO NOTHING`.
- Só dados de teste do ambiente local. O `make seed` não roda com `APP_ENVIRONMENT=production`. Dado de que a produção também precisa (feriados, por exemplo) vai numa migração de dados (`db/migrations/README.md`).
- As regras do banco valem aqui também: o `bruto` só aceita `INSERT`, e posições e ciclo entram na mesma transação (a FK é deferida).

Exemplo:

```sql
INSERT INTO referencia.linhas (codigo_linha)
VALUES ('701A-10'), ('7267-10')
ON CONFLICT DO NOTHING;
```

Por enquanto a pasta não tem seeds. Os dados de teste entram com os cards de dados (GTFS, coleta).
