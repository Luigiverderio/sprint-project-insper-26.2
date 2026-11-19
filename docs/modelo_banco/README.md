# Modelo do banco

Banco da plataforma (PostgreSQL 16 + PostGIS + pg_partman) e referência para as migrações Alembic (T6). Segue as decisões do time (T3 a T13), com os ajustes da seção "O que mudou" abaixo.

> **Desde o T6, o banco é criado pelas migrações Alembic** (`db/migrations/`, `make migrate`). As migrações 0001 a 0007 reproduzem este `esquema.sql`, e `tests/test_migrations.py` compara o `pg_dump` dos dois. O `esquema.sql` fica como registro do modelo validado: mudança nova vai só para migração nova.

| Arquivo | O que é |
|---|---|
| `esquema.sql` | O banco inteiro: extensões, schemas, tabelas, índices, partições, views e papéis. Roda numa transação. |
| `criar_banco.sh` | Cria tudo do zero em Docker (imagem, container, manutenção das partições, esquema e, opcionalmente, testes) |
| `teste.sql` | 13 grupos de verificações: duplicidade, FK, só inserção, papéis, mapa ao vivo, lacunas, GTFS, fuso, IPP/ICV, UUID, partições e retenção |
| `Dockerfile` | Postgres 16 + PostGIS 3 + pg_partman 5 |
| `validacao/` | Carga das saídas do pipeline R e comparação dos indicadores (ver "Validação") |

Testado em 2026-11-18 com Postgres 16.16, PostGIS 3.7.0 e pg_partman 5.5.0.

## Como criar o banco

**Do zero, em Docker** (no Windows, pelo Git Bash):

```bash
cd docs/modelo_banco
POSTGRES_PASSWORD='troque-isto' TESTAR=1 ./criar_banco.sh
```

O script constrói a imagem e sobe o container só em `127.0.0.1`, com volume de dados. Ele liga o background worker do pg_partman, roda os testes num banco descartável e aplica o esquema. Variáveis: `NOME`, `PORTA`, `BANCO`, `INICIO_PARTICOES` e `TESTAR`.

**Banco que já existe (RDS, EC2, outro projeto).** Requisitos: Postgres 16+, extensões `postgis` e `pg_partman` disponíveis e um banco vazio só deste sistema. Rode com o usuário dono do banco:

```bash
psql "host=... dbname=onibus user=<dono> sslmode=require" -v ON_ERROR_STOP=1 -f esquema.sql
```

Depois, por ambiente:

1. **Crie um usuário de login por serviço** (modelo no fim do `esquema.sql`).
   - `api_app` entra em `onibus_leitura`, com `statement_timeout`.
   - `coletor_app` entra em `onibus_coletor`.
   - `worker_app` entra em `onibus_worker`.
2. **Agende a manutenção das partições** a cada hora: background worker, `pg_cron` no RDS ou cron da máquina. Sem ela, a partir do 4º mês os dados caem na partição default.
3. **Monitore** `SELECT * FROM operacao.saude_particoes` (`ok` tem de ser `true`) e `operacao.saude_coleta`.

O caminho do RDS não foi testado. Testei o esquema só no container.

## Estrutura

| Schema | Tabelas | Quem escreve |
|---|---|---|
| `bruto` | `ciclos_coleta`, `posicoes_gps` (mensal, pg_partman) | coletor (só INSERT) |
| `referencia` | `linhas`, `linhas_olhovivo`, `feriados`, `gtfs_*` (7 tabelas), `coletas_programacao`, `linhas_programacao`, `partidas_programadas`, `tempos_estimados`, `horarios_operacao`, `itinerarios_vias`, `eventos_via` | coletor |
| `operacao` | `execucoes`, `saidas_programadas`, `viagens`, `passagens`, `lacunas_veiculo` + views `posicoes_atuais`, `lacunas_coleta`, `saude_coleta`, `saude_particoes` | worker |
| `indicadores` | `saidas_auditadas`, `partidas_descartadas`, `saidas_lacunas`, `icv_faixas` + views de IPP e ICV | worker |

A mesma chave de linha (`'701A-10'`) vale nas três fontes: `c` na Olho Vivo, `route_id` no GTFS e `codigo` na API de itinerários.

### Views de indicadores (as mesmas colunas dos CSVs do R)

| View | Agrupamento | CSV do R |
|---|---|---|
| `ipp_diario_sentido` | linha, dia, tipo de dia, sentido | `ipp_diario_por_sentido.csv` |
| `ipp_diario` | linha, dia, tipo de dia | `ipp_diario.csv` |
| `ipp_mensal_sentido` | linha, mês, sentido | `ipp_mensal_por_sentido.csv` |
| `ipp_mensal` | linha, mês | `ranking_linhas.csv` |
| `ipp_por_hora` | linha, mês, tipo de dia, hora local prevista | `ranking_linhas_horario.csv` (o R soma todos os meses) |
| `icv_periodo_dia`, `icv_diario_sentido`, `icv_mensal_sentido`, `icv_mensal` | período, dia, mês | `icv_mensal.csv` |

## Validação (2026-11-18)

### Indicadores iguais aos do R

Carreguei as saídas reais do pipeline R (linhas 701A-10 e 7267-10, agosto e setembro de 2026) e comparei as views com os CSVs do R, célula por célula.

| | Resultado |
|---|---|
| Carga | 5.339 saídas, 4.870 viagens, 1.319 lacunas, 1.760 faixas de ICV e 902.725 posições. Entrou tudo, sem nenhuma violação de `CHECK` ou FK. |
| Duplicatas de GPS | 3.384 descartadas pela chave, exatamente as 1.345 + 2.039 que o R marcou |
| IPP (diário, diário por sentido, mensal, mensal por sentido, por hora) | **4.828 células, 0 diferenças**, inclusive no arredondamento |
| Viagens por linha e mês (do ranking) | 8 células, 0 diferenças |
| ICV mensal | 12 células, 0 diferenças (701A-10 set/2026: 79,73%, ampliado 91,56%) |
| Período do ICV gerado pela hora | igual ao do R nas 1.760 faixas |

Para repetir: `validacao/exportar.R` gera os CSVs, e `validacao/carregar.sql` e `validacao/comparar.sql` rodam no container (esperam os arquivos em `/trab/dados`).

### Desempenho com volume (11,9 milhões de linhas numa partição)

Gerado por `validacao/desempenho.sql`, com 200 veículos, uma posição a cada 45 s e 31 dias (a densidade real por veículo):

| Medida | Resultado |
|---|---|
| Espaço por linha | 84,5 bytes na tabela + 75,9 nos índices = **160 bytes** |
| Projeção para ~400 milhões de linhas por mês | **~64 GB por mês** no Postgres |
| Um ciclo do coletor (5.000 linhas, `ON CONFLICT` + FK) com a tabela cheia | 23 a 66 ms |
| Uma linha num dia (19 mil posições, cache frio) | 135 ms, só a partição do mês |
| Um ônibus num mês (59 mil posições) | 287 ms, pela chave primária |
| Mapa ao vivo (5 minutos) | 159 ms; a view poda as partições na execução e só abre a do mês atual |
| Filtro por mês ou linha nas views de IPP e ICV | chega ao índice da tabela (`EXPLAIN` com seqscan desligado) |

O índice `(cod_linha_sentido, horario_gps)` ocupa 502 MB e o da chave primária, 359 MB. O primeiro é maior porque recebe inserções fora de ordem. Um `REINDEX` da partição depois que o mês fecha deve recuperar parte desse espaço, mas isso não foi medido.

No sintético, o BRIN do mapa ao vivo é impreciso: cada faixa cobre ~3 h de dados de 200 veículos. Com ~12 mil veículos, cada faixa cobriria segundos, mas isso só se confirma com o volume real.

### Segurança verificada

- O bruto aceita só INSERT. UPDATE, DELETE e TRUNCATE são barrados para os papéis e também para o dono, na tabela mãe e direto na partição.
- Cada papel só faz o seu:
  - a API não lê o bruto;
  - o coletor não entra em `operacao`;
  - o worker não grava no bruto.
- Um usuário sem papel não conecta, e ninguém cria objetos no `public`.
- O `statement_timeout` e o `default_transaction_read_only` do login da API funcionam. A consulta de 11 s foi cancelada.
- Partição nova criada pela manutenção herda o trigger e os 3 índices.
- A retenção desanexa o mês antigo sem apagar. A tabela fica intacta até o job de arquivo exportar e conferir.

## O que mudou em relação ao plano

1. **Imagem do banco (T4, T11).** A `postgis/postgis` não serve. As tags 16-* dela são Debian bullseye, e o repositório PGDG do bullseye saiu do ar (404), então não há como instalar o pg_partman nela. O `Dockerfile` parte de `postgres:16-bookworm` e instala PostGIS e pg_partman pelos pacotes PGDG.
2. **O volume é maior que "dezenas de milhões".**
   - Nos dados de teste há 1.100 a 1.250 registros por veículo por dia, porque o GPS atualiza a cada ~45 s.
   - Com ~12 mil veículos ativos por dia, seriam ~14 milhões de linhas por dia e **~400 milhões por mês**. Os 12 mil veículos são estimativa, a confirmar no primeiro dia de coleta.
   - Com os 160 bytes por linha medidos, isso dá **~64 GB por mês**.
   - A partição mensal foi mantida. Partição diária é uma opção: muda só o `p_interval`.
3. **O coletor em R descartava campos.** `coletor_de_posicoes_v3.R` remove `a`, `lt0`, `lt1`, `qv`, `sv` e `is`, e não guarda o `hr`. No modelo novo:
   - `a` vira `posicoes_gps.acessivel`;
   - `hr` vira `ciclos_coleta.hora_api`;
   - `c`, `lt0` e `lt1` são iguais para todos os veículos de um `cl` e vão para `referencia.linhas_olhovivo`, em vez de se repetirem em 400 milhões de linhas;
   - `sv` e `is` não são documentados: ficam só no JSON original até alguém descobrir o que são.
4. **"Bruto sem alteração" = JSON original + tabela.** A resposta íntegra de cada ciclo vai comprimida (`.json.gz`) para disco ou S3, apontada por `ciclos_coleta.arquivo_bruto`. A tabela guarda os campos já tipados.
5. **Os ciclos ficam no schema `bruto`**, não no `referencia`. São um log só de inserção e as posições apontam para eles (FK deferida: posições e ciclo entram na mesma transação).
6. **Prefixo como `integer`.** Nos dados atuais todos têm 5 dígitos (21011 a 82774), sem zero à esquerda.
7. **Repetição com coordenada diferente existe.** O mesmo veículo e o mesmo instante com outra coordenada aparecem em 0,06% a 0,4% dos registros. A chave `(prefixo, horario_gps)` fica com o primeiro, e a diferença aparece em `n_recebidos - n_inseridos`.
8. **Ids em UUID v7.** Todos os ids que eram `bigint` viraram `uuid`.
   - **É v7, não v4.** O v7 começa com o instante em milissegundos, então a inserção na chave primária continua no fim do índice. Medi 2 milhões de inserções: **5,1 s com v7 e 10,8 s com v4**. O Postgres 16 não tem `uuidv7()` nativo, então `public.uuid_v7()` o substitui.
   - **Custo:** +8 bytes por linha em `bruto.posicoes_gps` (~+3 GB por mês). Se pesar, o `ciclo_id` das posições é o único lugar onde vale reverter para `bigint`.
   - Tabelas de referência pequenas (`gtfs_versoes`, `coletas_programacao`) continuam com `integer`.
9. **Três papéis em vez de dois.** O plano tinha um papel de escrita para o coletor e o worker juntos. Separei em `onibus_coletor` e `onibus_worker`, para um bug no worker não conseguir gravar no bruto e vice-versa. Se o time preferir um só, basta dar os dois papéis ao mesmo login.

## Revisão do esquema (2026-11-18)

O que a revisão encontrou e corrigiu:

| Tema | Antes | Agora |
|---|---|---|
| TRUNCATE no bruto | o trigger de linha não pegava TRUNCATE | trigger `BEFORE TRUNCATE` na mãe e nos ciclos |
| Acesso ao banco | `PUBLIC` podia conectar e criar objetos no `public` | só os três papéis conectam; `CREATE` no `public` revogado |
| Papel de leitura | tinha `USAGE` no schema `bruto` | sem acesso nenhum ao bruto |
| `diferenca_min` | `real` (float4): 3,00000001 vira 3,0 e muda o status | `double precision` |
| Status da saída | qualquer status com qualquer diferença | `CHECK`: no horário ⇒ \|dif\| ≤ tolerância; atraso ⇒ dif > tolerância; antecipada ⇒ dif < −tolerância |
| Linha e dia repetidos em `saidas_auditadas` | podiam divergir da saída | FK composta para `saidas_programadas` |
| Tolerância do IPP (c) | 4 min fixo na view | coluna `tolerancia_ampliada_min`, como a de (a) e (b) |
| ICV | `hora` aceitava 0–27 e `periodo` era digitado à parte | `hora` 0–23 (hora do relógio, como no R) e `periodo` gerado pela hora |
| Views de IPP | faltavam mensal, mensal por sentido e por hora, e 4 colunas | as cinco views, com as colunas do R |
| Ranking de todas as linhas num mês | lia a tabela inteira | índice pela expressão do mês, usado pelas views |
| Mapa ao vivo | GPS com relógio adiantado ficava no mapa | corta horários mais de 2 min no futuro |
| Partições | nada avisava dado na default ou manutenção parada | view `operacao.saude_particoes` |
| Coordenadas | aceitava qualquer número | `CHECK` de faixa válida (o coletor filtra e conta em `n_rejeitados`) |
| Script | parava no meio se algo falhasse | uma transação só; mês inicial das partições é parâmetro |
| Código da linha | (testei um regex) | o catálogo tem `209P-1`, `CPTM L07-0` e `METRÔ L1-0`: só barra vazio e espaço nas pontas |
| Tipos numéricos | 15 colunas em `real` (float4); `3.1::double precision <= 3.1::real` dá falso e quebrava o `CHECK` com tolerância decimal | `double precision` em todas |
| Saída realizada sem diferença | `diferenca_min` nula passava nos `CHECK` de status | `CHECK`: `realizada_no_horario`, `realizada_com_atraso` e `realizada_antecipadamente` exigem `diferenca_min` |
| Horário do `stop_times.txt` | comentário dizia deslocamento a partir de zero | relativo ao início da viagem, com base qualquer por viagem; o carregador guarda o deslocamento até a primeira parada (ADR-001, P6) |

## Melhorias em relação às saídas do R

| No R | No banco |
|---|---|
| Chaves montadas em texto (`701A-10_20260831_0_0400`) | `id` uuid + colunas com `UNIQUE` |
| Listas em texto (`"21013,21035"`, `lacunas_ids`) | `integer[]` e tabela de ligação `saidas_lacunas` |
| `partida_descartada` em texto livre ("82409 às 09:36:58 percorreu 7%…") | tabela `partidas_descartadas` com prefixo, horário, fração e km |
| `quality_flags` com números dentro (`cobertura_6%;lacuna_max_348s`) | os números já são colunas; o resto vira `alertas text[]` |
| `schedule_source` com nome de arquivo | `coleta_id` com chave estrangeira |
| `auditoria_saidas` e `diagnostico_saidas` separadas (1:1) | uma tabela `saidas_auditadas` |
| 8 CSVs de IPP e ranking com o mesmo cálculo | contagens guardadas uma vez; percentuais nas views |
| Feriados fixos em `00_config.R` | tabela `referencia.feriados` |
| Parâmetros em `parametros_execucao.csv` | `operacao.execucoes.parametros` (jsonb) em cada resultado |
| `shapes.txt` como um ponto por linha | uma `LineString` por traçado, com comprimento calculado |

## Endpoints e o que cada um lê

| Endpoint (exemplo) | História | Lê | Índice |
|---|---|---|---|
| `GET /v1/posicoes/atuais` | US22 | view `operacao.posicoes_atuais` | BRIN em `horario_gps` |
| `GET /v1/linhas` | US23 | `referencia.linhas` + última coleta | PK |
| `GET /v1/linhas/{codigo}/tracado` | US22 | `gtfs_viagens` + `gtfs_tracados` (`ST_AsGeoJSON`) | `gtfs_viagens_rota_idx` |
| `GET /v1/linhas/{codigo}/programacao?data=` | US04 | `operacao.saidas_programadas` | `UNIQUE (codigo_linha, data_servico, …)` |
| `GET /v1/linhas/{codigo}/saidas?data=` | US23 | `indicadores.saidas_auditadas` | `(codigo_linha, data_servico)` |
| `GET /v1/linhas/{codigo}/ipp?de=&ate=` | US16 | `ipp_diario` ou `ipp_diario_sentido` | idem |
| `GET /v1/linhas/{codigo}/ipp?mes=` | US16 | `ipp_mensal`, `ipp_mensal_sentido`, `ipp_por_hora` | idem |
| `GET /v1/ranking?mes=` | US16 | `ipp_mensal` | índice do mês em `saidas_auditadas` |
| `GET /v1/linhas/{codigo}/icv?mes=` | US14 | `icv_mensal` (e `icv_mensal_sentido`, `icv_periodo_dia`) | PK e índice do mês de `icv_faixas` |
| `GET /v1/veiculos/{prefixo}/posicoes?de=&ate=` | — | `bruto.posicoes_gps` pelo worker (a API não lê o bruto) | PK `(prefixo, horario_gps)` |
| `GET /v1/coleta/lacunas` | US13 | view `operacao.lacunas_coleta` | `(fonte, iniciado_em)` |
| `GET /v1/saude` | US30 | views `saude_coleta` e `saude_particoes` | idem |

Filtre sempre por linha e/ou período: as views agregam o que sobrar do filtro.

O dia de serviço (03:00 a 03:00 de São Paulo) vai de 06:00Z a 06:00Z. A fronteira das partições é em UTC: as posições de 31/08 à noite (hora local) ficam na partição de setembro.

## Como o coletor grava (T12)

```sql
-- o coletor gera o UUID do ciclo (v7) ANTES de chamar a API: sem ida ao banco só para pedir id
BEGIN;
INSERT INTO bruto.posicoes_gps (..., ciclo_id) VALUES (..., :ciclo_id)   -- as posições válidas do ciclo
  ON CONFLICT (prefixo, horario_gps) DO NOTHING;
INSERT INTO bruto.ciclos_coleta (id, ..., n_recebidos, n_inseridos, n_rejeitados) VALUES (:ciclo_id, ...);
COMMIT;                                                                  -- a FK deferida é checada aqui
```

- Os `CHECK`s de coordenada rejeitam o **lote inteiro**. Por isso o coletor filtra antes as linhas inválidas e conta quantas em `n_rejeitados`.
- Um ciclo com falha grava só a linha em `ciclos_coleta`, com o `status` e o `erro`. O `CHECK` exige a mensagem.
- Em Python, `uuid.uuid7()` existe a partir do 3.14. No 3.12, use o pacote `uuid-utils` (`uuid_utils.uuid7()`) ou peça ao banco: `SELECT public.uuid_v7()`.

## Notas para as migrações (T6)

- **O `esquema.sql` usa comandos do psql** (`\set`, `\if`), que o Alembic não entende. Nas migrações, `inicio_particoes` vira uma constante ou configuração, e o `BEGIN/COMMIT` fica por conta do Alembic.
- **Autogenerate não entende partições nem o pg_partman.** Escreva esses trechos com `op.execute(...)`. No `env.py`, use `include_object` para ignorar o schema `partman` e as tabelas filhas (`posicoes_gps_p*` e `posicoes_gps_default`), senão o autogenerate tenta apagá-las.
- **Downgrade da tabela particionada:** `DELETE FROM partman.part_config WHERE parent_table = 'bruto.posicoes_gps'` e depois `DROP TABLE bruto.posicoes_gps CASCADE`.
- **Colunas calculadas:** `comprimento_m`, `minuto_servico` e `icv_faixas.periodo` usam `sa.Computed(...)`.
- **Geometrias:** `geoalchemy2.Geometry("LineString", srid=4326)` e `Geometry("Point", srid=4326)`.
- **Papéis:** os papéis de grupo (`onibus_coletor`, `onibus_worker`, `onibus_leitura`) ficam na migração. Os usuários de login e as senhas são criados por ambiente.
- **Rode todas as migrações com o mesmo usuário.** Os `ALTER DEFAULT PRIVILEGES` só valem para tabelas criadas por quem rodou o esquema.

## Manutenção das partições (T13)

- `CALL partman.run_maintenance_proc();` cria os meses futuros e desanexa os antigos. Rode a cada hora por um destes:
  - background worker (`criar_banco.sh` já liga);
  - `pg_cron`;
  - cron da máquina.
- O RDS suporta `pg_partman` e `pg_cron`.
- **Retenção:** está em 3 meses como valor provisório. A partição antiga só é desanexada (`retention_keep_table`) e continua como tabela (`bruto.posicoes_gps_pAAAAMMDD`). O job de arquivo exporta para Parquet no S3, confere o arquivo e então faz o `DROP`. O teste 13 confirma que nada se perde na desanexação.
- **Partição default:** recebe horários fora das faixas criadas, como um GPS com relógio errado. A view `operacao.saude_particoes` acusa dado nela e cobertura curta (manutenção parada).

## Decisões em aberto

1. Partição mensal ou diária, com o volume medido no primeiro dia de coleta do sistema inteiro.
2. Retenção do bruto no Postgres (3 meses é provisório; ~64 GB por mês).
3. `operacao.passagens`: nos dados de teste são ~168 mil por linha-mês, ou mais de 200 milhões por mês no sistema inteiro. Particionar, reter por pouco tempo ou calcular sob demanda.
4. Posições casadas ao traçado (o `posicoes_normalizadas` do R): guardar no Postgres com retenção curta ou só em Parquet.
5. Onde guardar os JSON originais: disco local em desenvolvimento, S3 em produção.
6. Medir o BRIN do mapa ao vivo e o `REINDEX` das partições fechadas com o volume real.
