-- Compara os indicadores das views com os CSVs que o pipeline R gerou, célula
-- por célula (FULL JOIN pelas chaves).
\set ON_ERROR_STOP on
SET timezone = 'UTC';

-- colunas de IPP, na ordem dos CSVs do R
\set m 'programadas_total int, programadas_validas int, no_horario int, com_atraso int, antecipadas int, quase_completas int, partida_sem_viagem_completa int, possivelmente_nao_realizadas int, inconclusivas int, pontuais_b int, pontuais_c int, ipp_pct numeric, ipp_b_pct numeric, ipp_c_pct numeric, realizadas_pct numeric, nao_realizadas_pct numeric, inconclusivas_pct numeric'
CREATE TABLE validacao.r_ipp_diario_sentido (codigo_linha text, data_servico date, tipo_dia text, sentido smallint, :m);
CREATE TABLE validacao.r_ipp_diario (codigo_linha text, data_servico date, tipo_dia text, :m);
CREATE TABLE validacao.r_ipp_mensal_sentido (codigo_linha text, mes_txt text, sentido smallint, :m);
CREATE TABLE validacao.r_ranking (codigo_linha text, mes_txt text, viagens_reconstruidas int, viagens_completas int, :m);
CREATE TABLE validacao.r_ipp_hora (codigo_linha text, tipo_dia text, hora smallint, :m);
CREATE TABLE validacao.r_icv_mensal (codigo_linha text, mes_txt text, icv_pct numeric, icv_ampliado_pct numeric,
                                     programadas int, realizadas_lim int);
\copy validacao.r_ipp_diario_sentido FROM '/trab/dados/ipp_diario_por_sentido.csv' CSV HEADER
\copy validacao.r_ipp_diario FROM '/trab/dados/ipp_diario.csv' CSV HEADER
\copy validacao.r_ipp_mensal_sentido FROM '/trab/dados/ipp_mensal_por_sentido.csv' CSV HEADER
\copy validacao.r_ranking FROM '/trab/dados/ranking_linhas.csv' CSV HEADER
\copy validacao.r_ipp_hora FROM '/trab/dados/ranking_linhas_horario.csv' CSV HEADER
\copy validacao.r_icv_mensal FROM '/trab/dados/icv_mensal.csv' CSV HEADER

CREATE FUNCTION validacao.comparar(nome text, r text, b text, chaves text[], cols text[])
RETURNS TABLE (comparacao text, coluna text, linhas_r bigint, linhas_banco bigint, so_no_r bigint,
               so_no_banco bigint, diferentes bigint, max_dif numeric)
LANGUAGE plpgsql AS $f$
DECLARE c text; junta text;
BEGIN
  junta := (SELECT string_agg(format('r.%1$I = b.%1$I', k), ' AND ') FROM unnest(chaves) k);
  FOREACH c IN ARRAY cols LOOP
    RETURN QUERY EXECUTE format($q$
      SELECT %1$L::text, %2$L::text,
             count(*) FILTER (WHERE r_ok), count(*) FILTER (WHERE b_ok),
             count(*) FILTER (WHERE r_ok AND NOT b_ok), count(*) FILTER (WHERE b_ok AND NOT r_ok),
             count(*) FILTER (WHERE r_ok AND b_ok AND rv IS DISTINCT FROM bv),
             max(abs(rv - bv))
        FROM (SELECT r._r IS NOT NULL AS r_ok, b._b IS NOT NULL AS b_ok,
                     r.%2$I::numeric AS rv, b.%2$I::numeric AS bv
                FROM (SELECT *, true AS _r FROM (%3$s) x) r
                FULL JOIN (SELECT *, true AS _b FROM (%4$s) y) b ON %5$s) z $q$,
      nome, c, r, b, junta);
  END LOOP;
END $f$;

\set cols '''{programadas_total,programadas_validas,no_horario,com_atraso,antecipadas,quase_completas,partida_sem_viagem_completa,possivelmente_nao_realizadas,inconclusivas,pontuais_b,pontuais_c,ipp_pct,ipp_b_pct,ipp_c_pct,realizadas_pct,nao_realizadas_pct,inconclusivas_pct}'''

CREATE TEMP TABLE resultado AS
SELECT * FROM validacao.comparar('ipp_diario_sentido',
  'SELECT * FROM validacao.r_ipp_diario_sentido', 'SELECT * FROM indicadores.ipp_diario_sentido',
  '{codigo_linha,data_servico,tipo_dia,sentido}', :cols)
UNION ALL SELECT * FROM validacao.comparar('ipp_diario',
  'SELECT * FROM validacao.r_ipp_diario', 'SELECT * FROM indicadores.ipp_diario',
  '{codigo_linha,data_servico,tipo_dia}', :cols)
UNION ALL SELECT * FROM validacao.comparar('ipp_mensal_sentido',
  'SELECT *, to_date(mes_txt || ''-01'', ''YYYY-MM-DD'') AS mes FROM validacao.r_ipp_mensal_sentido',
  'SELECT * FROM indicadores.ipp_mensal_sentido', '{codigo_linha,mes,sentido}', :cols)
UNION ALL SELECT * FROM validacao.comparar('ipp_mensal',
  'SELECT *, to_date(mes_txt || ''-01'', ''YYYY-MM-DD'') AS mes FROM validacao.r_ranking',
  'SELECT * FROM indicadores.ipp_mensal', '{codigo_linha,mes}', :cols)
UNION ALL SELECT * FROM validacao.comparar('ipp_por_hora (somado nos meses)',
  'SELECT * FROM validacao.r_ipp_hora',
  'SELECT codigo_linha, tipo_dia, hora_prevista AS hora,
          sum(programadas_total) programadas_total, sum(programadas_validas) programadas_validas,
          sum(no_horario) no_horario, sum(com_atraso) com_atraso, sum(antecipadas) antecipadas,
          sum(quase_completas) quase_completas, sum(partida_sem_viagem_completa) partida_sem_viagem_completa,
          sum(possivelmente_nao_realizadas) possivelmente_nao_realizadas, sum(inconclusivas) inconclusivas,
          sum(pontuais_b) pontuais_b, sum(pontuais_c) pontuais_c,
          indicadores.pct(sum(no_horario), sum(programadas_validas)) ipp_pct,
          indicadores.pct(sum(pontuais_b), sum(programadas_validas)) ipp_b_pct,
          indicadores.pct(sum(pontuais_c), sum(programadas_validas)) ipp_c_pct,
          indicadores.pct(sum(no_horario + com_atraso + antecipadas), sum(programadas_validas)) realizadas_pct,
          indicadores.pct(sum(possivelmente_nao_realizadas), sum(programadas_validas)) nao_realizadas_pct,
          indicadores.pct(sum(inconclusivas), sum(programadas_validas)) inconclusivas_pct
     FROM indicadores.ipp_por_hora GROUP BY 1, 2, 3',
  '{codigo_linha,tipo_dia,hora}', :cols)
UNION ALL SELECT * FROM validacao.comparar('icv_mensal',
  'SELECT *, to_date(mes_txt || ''-01'', ''YYYY-MM-DD'') AS mes FROM validacao.r_icv_mensal',
  'SELECT * FROM indicadores.icv_mensal', '{codigo_linha,mes}',
  '{icv_pct,icv_ampliado_pct,programadas,realizadas_lim}')
UNION ALL SELECT * FROM validacao.comparar('viagens por linha-mês (ranking)',
  'SELECT codigo_linha, to_date(mes_txt || ''-01'', ''YYYY-MM-DD'') AS mes, viagens_reconstruidas, viagens_completas FROM validacao.r_ranking',
  'SELECT codigo_linha, date_trunc(''month'', data_servico::timestamp)::date AS mes, count(*) AS viagens_reconstruidas,
          count(*) FILTER (WHERE status_trajetoria IN (''viagem_confirmada'', ''viagem_provavel'')) AS viagens_completas
     FROM operacao.viagens GROUP BY 1, 2',
  '{codigo_linha,mes}', '{viagens_reconstruidas,viagens_completas}');

\echo '== resumo por comparação (células comparadas e diferentes)'
SELECT comparacao, max(linhas_r) AS linhas_r, max(linhas_banco) AS linhas_banco,
       max(so_no_r) AS so_no_r, max(so_no_banco) AS so_no_banco,
       sum(linhas_r) AS celulas, sum(diferentes) AS celulas_diferentes, max(max_dif) AS maior_diferenca
  FROM resultado GROUP BY comparacao ORDER BY comparacao;
\echo '== colunas com diferença'
SELECT * FROM resultado WHERE diferentes > 0 OR so_no_r > 0 OR so_no_banco > 0 ORDER BY comparacao, coluna;
