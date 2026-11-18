-- Desempenho com volume: ~12 milhões de posições sintéticas em outubro/2026
-- (200 veículos x 1 posição a cada 45 s x 31 dias = densidade real por veículo).
\set ON_ERROR_STOP on
SET timezone = 'UTC';

INSERT INTO bruto.ciclos_coleta (id, fonte, iniciado_em, concluido_em, status, versao_coletor)
VALUES ('00000000-0000-7000-8000-00000000aaaa', 'olhovivo_posicao', now(), now(), 'ok', 'sintetico')
ON CONFLICT (id) DO NOTHING;

\echo '== carga sintética (um INSERT por dia, cada um na sua transação)'
\timing on
SELECT format($$INSERT INTO bruto.posicoes_gps (horario_gps, prefixo, cod_linha_sentido, sentido_api, acessivel, latitude, longitude, ciclo_id)
SELECT t + v * interval '200 ms', 100000 + v, 900000 + v / 10, 1 + v %% 2, v %% 3 = 0,
       -23.55 + random() * 0.2, -46.63 + random() * 0.2, '00000000-0000-7000-8000-00000000aaaa'
  FROM generate_series(%L::timestamptz, %L::timestamptz - interval '1 s', interval '45 s') t,
       generate_series(1, 200) v$$, d, d + interval '1 day')
  FROM generate_series(date '2026-10-01', date '2026-10-31', interval '1 day') d
\gexec
\timing off
ANALYZE bruto.posicoes_gps;

\echo '== tamanho real da partição de outubro'
SELECT count(*) AS linhas,
       pg_size_pretty(pg_relation_size('bruto.posicoes_gps_p20261001')) AS tabela,
       pg_size_pretty(pg_indexes_size('bruto.posicoes_gps_p20261001')) AS indices,
       pg_size_pretty(pg_total_relation_size('bruto.posicoes_gps_p20261001')) AS total,
       round(pg_relation_size('bruto.posicoes_gps_p20261001')::numeric / count(*), 1) AS bytes_linha_tabela,
       round(pg_total_relation_size('bruto.posicoes_gps_p20261001')::numeric / count(*), 1) AS bytes_linha_total
  FROM bruto.posicoes_gps_p20261001;
SELECT indexrelid::regclass AS indice, pg_size_pretty(pg_relation_size(indexrelid)) AS tamanho
  FROM pg_index WHERE indrelid = 'bruto.posicoes_gps_p20261001'::regclass;

\echo '== um ciclo do coletor (5.000 posições) com a tabela cheia, 3 vezes'
\timing on
SELECT format($$BEGIN;
WITH novas AS (
  INSERT INTO bruto.posicoes_gps (horario_gps, prefixo, cod_linha_sentido, sentido_api, latitude, longitude, ciclo_id)
  SELECT %1$L::timestamptz - (random() * 40) * interval '1 s', 200000 + v, 950000 + v / 10, 1, -23.5, -46.6, %2$L::uuid
    FROM generate_series(1, 5000) v
  ON CONFLICT (prefixo, horario_gps) DO NOTHING RETURNING 1)
INSERT INTO bruto.ciclos_coleta (id, fonte, iniciado_em, concluido_em, status, n_recebidos, n_inseridos, versao_coletor)
SELECT %2$L::uuid, 'olhovivo_posicao', now(), now(), 'ok', 5000, count(*), 'sintetico' FROM novas;
COMMIT;$$, '2026-10-31 23:00:00+00'::timestamptz + n * interval '30 s', public.uuid_v7())
  FROM generate_series(1, 3) n
\gexec
\timing off

\echo '== A. uma linha num dia (worker / tela da linha)'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT * FROM bruto.posicoes_gps
 WHERE cod_linha_sentido = 900005
   AND horario_gps >= '2026-10-15 06:00+00' AND horario_gps < '2026-10-16 06:00+00';

\echo '== B. um ônibus num mês (relatório de um veículo)'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT * FROM bruto.posicoes_gps
 WHERE prefixo = 100057 AND horario_gps >= '2026-10-01' AND horario_gps < '2026-11-01';

\echo '== C. mapa ao vivo: última posição de cada veículo em 5 minutos (instante fixo de outubro)'
EXPLAIN (ANALYZE, BUFFERS, COSTS OFF, SUMMARY ON)
SELECT DISTINCT ON (prefixo) prefixo, horario_gps, latitude, longitude
  FROM bruto.posicoes_gps
 WHERE horario_gps > '2026-10-20 11:55+00' AND horario_gps <= '2026-10-20 12:02+00'
 ORDER BY prefixo, horario_gps DESC;

\echo '== D. a view do mapa ao vivo (now()): poda das partições na execução'
EXPLAIN (ANALYZE, COSTS OFF, SUMMARY ON) SELECT * FROM operacao.posicoes_atuais;

\echo '== E. filtros nas views de indicadores chegam até a tabela? (seqscan desligado para ver o índice)'
SET enable_seqscan = off;
EXPLAIN (COSTS OFF) SELECT * FROM indicadores.ipp_mensal WHERE mes = '2026-09-01';
EXPLAIN (COSTS OFF) SELECT * FROM indicadores.ipp_diario WHERE codigo_linha = '701A-10' AND data_servico = '2026-09-10';
EXPLAIN (COSTS OFF) SELECT * FROM indicadores.icv_mensal WHERE mes = '2026-09-01';
EXPLAIN (COSTS OFF) SELECT * FROM indicadores.icv_mensal WHERE codigo_linha = '7267-10';
RESET enable_seqscan;
\echo '== F. tempo das views com os dados reais'
\timing on
SELECT codigo_linha, mes, ipp_pct, ipp_b_pct, ipp_c_pct FROM indicadores.ipp_mensal ORDER BY 1, 2;
SELECT * FROM indicadores.icv_mensal ORDER BY 1, 2;
SELECT count(*) FROM indicadores.ipp_diario_sentido WHERE codigo_linha = '701A-10';
\timing off
SELECT * FROM operacao.saude_particoes;
