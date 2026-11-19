-- Testes do esquema (rodar depois de esquema.sql, num banco descartável, com o
-- dono do banco). Cada bloco falha com EXCEPTION se a regra não valer.
--   psql -v ON_ERROR_STOP=1 -d onibus_teste -f teste.sql
-- Os dados de teste são de setembro de 2026: crie o banco com
-- -v inicio_particoes=2026-08-01 (ou anterior).
\set ON_ERROR_STOP on
SET timezone = 'UTC';

\echo '1. coleta: ciclo + posições na mesma transação; repetidas são descartadas'
BEGIN;
-- o coletor gera o UUID do ciclo no começo (sem nextval) e grava o ciclo no fim;
-- aqui o id é fixo para o teste.
WITH novas AS (
  INSERT INTO bruto.posicoes_gps
    (horario_gps, prefixo, cod_linha_sentido, sentido_api, acessivel, latitude, longitude, ciclo_id)
  VALUES ('2026-09-15 08:30:12+00', 21173, 689, 1, true, -23.4801, -46.5702, '00000000-0000-7000-8000-000000001001'::uuid),
         ('2026-09-15 08:30:12+00', 21173, 689, 1, true, -23.4801, -46.5702, '00000000-0000-7000-8000-000000001001'::uuid),  -- repetida
         ('2026-09-15 08:30:40+00', 21174, 33457, 2, false, -23.5560, -46.6900, '00000000-0000-7000-8000-000000001001'::uuid)
  ON CONFLICT (prefixo, horario_gps) DO NOTHING
  RETURNING 1)
INSERT INTO bruto.ciclos_coleta (id, fonte, iniciado_em, concluido_em, status, http_status,
                                 hora_api, n_recebidos, n_inseridos, n_rejeitados, versao_coletor)
SELECT '00000000-0000-7000-8000-000000001001'::uuid, 'olhovivo_posicao', '2026-09-15 08:30:00+00', '2026-09-15 08:30:02+00', 'ok', 200,
       '05:30', 3, count(*), 0, 'teste' FROM novas;
COMMIT;
DO $$ BEGIN
  IF (SELECT n_inseridos FROM bruto.ciclos_coleta WHERE id = '00000000-0000-7000-8000-000000001001'::uuid) <> 2 THEN
    RAISE EXCEPTION 'esperava 2 inseridas (1 repetida descartada)'; END IF;
  IF (SELECT tableoid::regclass::text FROM bruto.posicoes_gps WHERE prefixo = 21173) NOT LIKE 'bruto.posicoes_gps_p2026%09%' THEN
    RAISE EXCEPTION 'posição de setembro caiu na partição errada: %',
      (SELECT tableoid::regclass FROM bruto.posicoes_gps WHERE prefixo = 21173); END IF;
END $$;
SELECT tableoid::regclass AS particao, prefixo, horario_gps FROM bruto.posicoes_gps ORDER BY 3;

\echo '2. FK deferida: posição sem ciclo é rejeitada no COMMIT; coordenada impossível é rejeitada'
DO $$ BEGIN
  BEGIN
    INSERT INTO bruto.posicoes_gps VALUES ('2026-09-15 09:00:00+00', 1, 1, 1, NULL, -23.5, -46.6, '00000000-0000-7000-8000-00000000ffff'::uuid);
    SET CONSTRAINTS ALL IMMEDIATE;
    RAISE EXCEPTION 'FK não barrou ciclo inexistente';
  EXCEPTION WHEN foreign_key_violation THEN RAISE NOTICE 'ok: FK barrou';
  END;
  BEGIN
    INSERT INTO bruto.posicoes_gps VALUES ('2026-09-15 09:00:00+00', 2, 1, 1, NULL, -230.5, -46.6, '00000000-0000-7000-8000-000000001001'::uuid);
    RAISE EXCEPTION 'aceitou latitude -230.5';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: coordenada impossível barrada';
  END;
  BEGIN
    INSERT INTO bruto.ciclos_coleta (fonte, iniciado_em, concluido_em, status, versao_coletor)
    VALUES ('olhovivo_posicao', now(), now(), 'erro_http', 'teste');
    RAISE EXCEPTION 'aceitou ciclo com erro sem dizer o porquê';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: falha sem mensagem barrada';
  END;
END $$;

\echo '3. bruto é só inserção: UPDATE, DELETE e TRUNCATE barrados até para o dono, na mãe e na partição'
SET ROLE onibus_coletor;
DO $$ BEGIN
  BEGIN UPDATE bruto.posicoes_gps SET latitude = 0; RAISE EXCEPTION 'coletor conseguiu UPDATE';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: coletor sem UPDATE'; END;
  BEGIN DELETE FROM bruto.ciclos_coleta; RAISE EXCEPTION 'coletor conseguiu DELETE';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: coletor sem DELETE'; END;
  BEGIN TRUNCATE bruto.posicoes_gps; RAISE EXCEPTION 'coletor conseguiu TRUNCATE';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: coletor sem TRUNCATE'; END;
END $$;
RESET ROLE;
DO $$ DECLARE part text; BEGIN
  BEGIN UPDATE bruto.posicoes_gps SET latitude = 0 WHERE prefixo = 21173; RAISE EXCEPTION 'dono conseguiu UPDATE';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: trigger barrou o UPDATE do dono'; END;
  BEGIN TRUNCATE bruto.posicoes_gps; RAISE EXCEPTION 'dono conseguiu TRUNCATE';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: trigger barrou o TRUNCATE do dono'; END;
  BEGIN TRUNCATE bruto.ciclos_coleta CASCADE; RAISE EXCEPTION 'dono conseguiu TRUNCATE nos ciclos';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: trigger barrou o TRUNCATE dos ciclos'; END;
  -- direto na partição (o trigger de linha é clonado nela)
  SELECT tableoid::regclass::text INTO part FROM bruto.posicoes_gps WHERE prefixo = 21173;
  BEGIN EXECUTE format('DELETE FROM %s', part); RAISE EXCEPTION 'dono apagou direto na partição %', part;
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: partição % também barra DELETE', part; END;
END $$;

\echo '4. papéis: cada serviço só faz o seu'
SET ROLE onibus_leitura;
DO $$ BEGIN
  BEGIN INSERT INTO referencia.linhas VALUES ('X'); RAISE EXCEPTION 'leitura conseguiu INSERT';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: leitura sem INSERT'; END;
  BEGIN PERFORM 1 FROM bruto.posicoes_gps LIMIT 1; RAISE EXCEPTION 'leitura leu o bruto';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: leitura sem acesso ao bruto'; END;
  PERFORM * FROM operacao.saude_coleta;
  PERFORM * FROM operacao.posicoes_atuais;
  PERFORM * FROM operacao.saude_particoes;
  PERFORM * FROM indicadores.ipp_mensal;
  RAISE NOTICE 'ok: leitura usa as views';
END $$;
RESET ROLE;
SET ROLE onibus_coletor;
DO $$ BEGIN
  BEGIN PERFORM 1 FROM operacao.viagens LIMIT 1; RAISE EXCEPTION 'coletor leu operacao';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: coletor fora de operacao'; END;
  BEGIN DELETE FROM referencia.linhas; RAISE EXCEPTION 'coletor apagou referência';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: coletor não apaga referência'; END;
END $$;
RESET ROLE;
SET ROLE onibus_worker;
DO $$ BEGIN
  PERFORM 1 FROM bruto.posicoes_gps LIMIT 1;
  BEGIN INSERT INTO bruto.ciclos_coleta (fonte, iniciado_em, concluido_em, status, versao_coletor)
        VALUES ('gtfs', now(), now(), 'ok', 'x');
        RAISE EXCEPTION 'worker gravou no bruto';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: worker lê mas não grava o bruto'; END;
  BEGIN INSERT INTO referencia.linhas VALUES ('X'); RAISE EXCEPTION 'worker gravou referência';
  EXCEPTION WHEN insufficient_privilege THEN RAISE NOTICE 'ok: worker não grava referência'; END;
END $$;
RESET ROLE;
CREATE ROLE onibus_teste_sem_papel;
DO $$ BEGIN
  IF has_database_privilege('onibus_teste_sem_papel', current_database(), 'CONNECT') THEN
    RAISE EXCEPTION 'um usuário qualquer consegue conectar'; END IF;
  IF has_schema_privilege('onibus_teste_sem_papel', 'public', 'CREATE') THEN
    RAISE EXCEPTION 'um usuário qualquer cria objetos no public'; END IF;
  IF NOT has_database_privilege('onibus_leitura', current_database(), 'CONNECT') THEN
    RAISE EXCEPTION 'leitura sem CONNECT'; END IF;
  RAISE NOTICE 'ok: sem papel não conecta nem cria no public';
END $$;
DROP ROLE onibus_teste_sem_papel;

\echo '5. mapa ao vivo: só a última posição recente de cada veículo; GPS adiantado fica de fora'
INSERT INTO referencia.linhas_olhovivo VALUES (689, '701A-10', 1, 'METRÔ VL. MADALENA', 'PQ. EDU CHAVES', now(), now());
INSERT INTO bruto.ciclos_coleta (id, fonte, iniciado_em, concluido_em, status, versao_coletor)
VALUES ('00000000-0000-7000-8000-000000001002'::uuid, 'olhovivo_posicao', now() - interval '1 min', now(), 'ok', 'teste');
INSERT INTO bruto.posicoes_gps VALUES
  (now() - interval '90 s', 30001, 689, 1, true, -23.50, -46.60, '00000000-0000-7000-8000-000000001002'::uuid),
  (now() - interval '30 s', 30001, 689, 1, true, -23.51, -46.61, '00000000-0000-7000-8000-000000001002'::uuid),
  (now() - interval '20 min', 30002, 689, 1, true, -23.52, -46.62, '00000000-0000-7000-8000-000000001002'::uuid),
  (now() + interval '1 hour', 30003, 689, 1, true, -23.53, -46.63, '00000000-0000-7000-8000-000000001002'::uuid);
DO $$ BEGIN
  IF (SELECT count(*) FROM operacao.posicoes_atuais) <> 1 THEN RAISE EXCEPTION 'esperava 1 veículo ao vivo'; END IF;
  IF (SELECT ST_Y(geom) FROM operacao.posicoes_atuais) <> -23.51 THEN RAISE EXCEPTION 'não pegou a última posição'; END IF;
  IF (SELECT codigo_linha FROM operacao.posicoes_atuais) <> '701A-10' THEN RAISE EXCEPTION 'não resolveu o cl'; END IF;
END $$;
SELECT prefixo, codigo_linha, horario_gps, ST_AsText(geom) FROM operacao.posicoes_atuais;

\echo '6. lacunas de coleta'
INSERT INTO bruto.ciclos_coleta (fonte, iniciado_em, concluido_em, status, versao_coletor) VALUES
  ('olhovivo_posicao', '2026-09-15 08:31:00+00', '2026-09-15 08:31:01+00', 'ok', 'teste'),
  ('olhovivo_posicao', '2026-09-15 08:40:00+00', '2026-09-15 08:40:01+00', 'ok', 'teste');
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM operacao.lacunas_coleta
                  WHERE inicio_lacuna = '2026-09-15 08:31:00+00' AND duracao = interval '9 min') THEN
    RAISE EXCEPTION 'não achou a lacuna de 9 min'; END IF;
END $$;
SELECT * FROM operacao.lacunas_coleta WHERE fim_lacuna < '2026-10-01';

\echo '7. GTFS: duas versões convivem; comprimento calculado; cascata ao apagar'
INSERT INTO referencia.gtfs_versoes (nome, hash_sha256, capturado_em, arquivo_origem) VALUES
  ('20260226', repeat('a', 64), '2026-02-26', 'gtfs/20260226.zip'),
  ('20261104', repeat('b', 64), '2026-11-04', 'gtfs/20261104.zip');
INSERT INTO referencia.gtfs_rotas (gtfs_versao_id, route_id, nome_curto, tipo_rota)
SELECT id, '701A-10', '701A', 3 FROM referencia.gtfs_versoes;
INSERT INTO referencia.gtfs_tracados (gtfs_versao_id, shape_id, geom)
SELECT id, 's1', ST_GeomFromText('LINESTRING(-46.57 -23.48, -46.69 -23.556)', 4326) FROM referencia.gtfs_versoes;
INSERT INTO referencia.gtfs_viagens (gtfs_versao_id, trip_id, route_id, sentido, shape_id)
SELECT id, '701A-10-0', '701A-10', 0, 's1' FROM referencia.gtfs_versoes;
DO $$ BEGIN
  IF (SELECT count(*) FROM referencia.gtfs_viagens WHERE trip_id = '701A-10-0') <> 2 THEN
    RAISE EXCEPTION 'as duas versões deviam conviver'; END IF;
  IF (SELECT round(comprimento_m) FROM referencia.gtfs_tracados LIMIT 1) NOT BETWEEN 14000 AND 16000 THEN
    RAISE EXCEPTION 'comprimento estranho: %', (SELECT comprimento_m FROM referencia.gtfs_tracados LIMIT 1); END IF;
END $$;
DELETE FROM referencia.gtfs_versoes WHERE nome = '20260226';
DO $$ BEGIN
  IF (SELECT count(*) FROM referencia.gtfs_viagens) <> 1 THEN RAISE EXCEPTION 'cascata não apagou a versão'; END IF;
END $$;
SELECT v.nome, t.shape_id, round(t.comprimento_m) AS comprimento_m FROM referencia.gtfs_tracados t
  JOIN referencia.gtfs_versoes v ON v.id = t.gtfs_versao_id;

\echo '8. programação: minuto do dia de serviço e instante em UTC'
INSERT INTO referencia.linhas VALUES ('701A-10'), ('CPTM L07-0'), ('209P-1');   -- formatos reais do catálogo
DO $$ BEGIN
  BEGIN INSERT INTO referencia.linhas VALUES (' 701A-10'); RAISE EXCEPTION 'aceitou código com espaço';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: código com espaço barrado'; END;
END $$;
INSERT INTO referencia.coletas_programacao (data_coleta, fonte, iniciada_em) VALUES ('2026-11-16', 'api_itinerarios', now());
INSERT INTO referencia.linhas_programacao (coleta_id, codigo_linha, cod_planejamento, area)
SELECT id, '701A-10', 236941, 2 FROM referencia.coletas_programacao;
INSERT INTO referencia.partidas_programadas (coleta_id, codigo_linha, sentido, tipo_dia, ordem, horario, faixa_horaria, veiculo_acessivel)
SELECT id, '701A-10', 0, 'U', o, h, f, true FROM referencia.coletas_programacao,
  (VALUES (1, '04:00', 4), (2, '23:50', 23), (3, '00:10', 0)) AS x(o, h, f);
DO $$ BEGIN
  IF (SELECT minuto_servico FROM referencia.partidas_programadas WHERE horario = '00:10') <> 1450 THEN
    RAISE EXCEPTION '00:10 devia ser 1450'; END IF;
  IF referencia.instante_programado('2026-09-15', 1450) <> '2026-09-16 03:10:00+00' THEN
    RAISE EXCEPTION '00:10 do dia 15 devia ser 03:10Z do dia 16: %', referencia.instante_programado('2026-09-15', 1450); END IF;
  IF referencia.instante_programado('2026-09-15', 240) <> '2026-09-15 07:00:00+00' THEN
    RAISE EXCEPTION '04:00 local devia ser 07:00Z'; END IF;
END $$;
SELECT horario, minuto_servico, referencia.instante_programado('2026-09-15', minuto_servico) AS instante_utc
  FROM referencia.partidas_programadas ORDER BY minuto_servico;
DO $$ BEGIN
  BEGIN INSERT INTO referencia.partidas_programadas (coleta_id, codigo_linha, sentido, tipo_dia, ordem, horario, faixa_horaria, veiculo_acessivel)
        SELECT id, '701A-10', 0, 'U', 9, '24:10', 0, true FROM referencia.coletas_programacao;
        RAISE EXCEPTION 'aceitou horário inválido';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: horário inválido barrado'; END;
END $$;

\echo '9. IPP e ICV pelas views (mesma fórmula do R) e regras de consistência'
INSERT INTO operacao.execucoes (etapa, iniciada_em, status, versao_algoritmo, versao_regra, parametros)
VALUES ('auditoria', now(), 'ok', 'mm-ciclo-0.1.0', 'ipp-0.2.0', '{"tolerancia_min": 3}')
RETURNING id AS exec_id \gset
INSERT INTO operacao.saidas_programadas (codigo_linha, data_servico, sentido, tipo_dia, ordem, partida_prevista, coleta_id, coleta_posterior_a_data)
SELECT '701A-10', '2026-09-15', 0, 'U', o, referencia.instante_programado('2026-09-15', 240 + 20 * o), c.id, true
  FROM referencia.coletas_programacao c, generate_series(1, 6) o;
INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, diferenca_min, status, tolerancia_min, execucao_id)
SELECT s.id, s.codigo_linha, s.data_servico, d, st, 3, :'exec_id'::uuid
  FROM operacao.saidas_programadas s
  JOIN (VALUES (1, 1.0, 'realizada_no_horario'), (2, 3.5, 'realizada_com_atraso'),
               (3, NULL, 'inconclusiva'), (4, NULL, 'possivelmente_nao_realizada'),
               (5, NULL, 'fora_do_periodo_coletado')) AS x(o, d, st) ON x.o = s.ordem;
-- válidas = 4; no horário = 1 -> 25%; (b) = no horário + inconclusiva = 50%; (c) inclui o atraso de 3,5 -> 75%
DO $$ DECLARE r record; BEGIN
  SELECT * INTO r FROM indicadores.ipp_diario WHERE codigo_linha = '701A-10';
  IF r.programadas_validas <> 4 OR r.ipp_pct <> 25 OR r.ipp_b_pct <> 50 OR r.ipp_c_pct <> 75
     OR r.realizadas_pct <> 50 OR r.nao_realizadas_pct <> 25 OR r.inconclusivas_pct <> 25 THEN
    RAISE EXCEPTION 'IPP errado: %', r; END IF;
  SELECT * INTO r FROM indicadores.ipp_mensal WHERE codigo_linha = '701A-10';
  IF r.mes <> '2026-09-01' OR r.ipp_pct <> 25 THEN RAISE EXCEPTION 'IPP mensal errado: %', r; END IF;
  SELECT * INTO r FROM indicadores.ipp_por_hora WHERE codigo_linha = '701A-10' AND hora_prevista = 4;
  IF r.programadas_total <> 2 THEN RAISE EXCEPTION 'IPP por hora errado (04:20 e 04:40 locais): %', r; END IF;
END $$;
SELECT codigo_linha, data_servico, programadas_validas, ipp_pct, ipp_b_pct, ipp_c_pct FROM indicadores.ipp_diario;
DO $$ DECLARE sid uuid; BEGIN
  SELECT id INTO sid FROM operacao.saidas_programadas WHERE ordem = 6;
  -- status precisa bater com a diferença
  BEGIN INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, diferenca_min, status, tolerancia_min, execucao_id)
        SELECT sid, '701A-10', '2026-09-15', 5, 'realizada_no_horario', 3, id FROM operacao.execucoes;
        RAISE EXCEPTION 'aceitou no horário com 5 min de diferença';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: status incoerente com a diferença barrado'; END;
  -- linha e dia repetidos precisam ser os da saída
  BEGIN INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, status, tolerancia_min, execucao_id)
        SELECT sid, '701A-10', '2026-09-16', 'inconclusiva', 3, id FROM operacao.execucoes;
        RAISE EXCEPTION 'aceitou dia diferente do da saída';
  EXCEPTION WHEN foreign_key_violation THEN RAISE NOTICE 'ok: FK composta barrou dia errado'; END;
  -- saída realizada sem diferença calculada é barrada (NULL não passa)
  BEGIN INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, status, tolerancia_min, execucao_id)
        SELECT sid, '701A-10', '2026-09-15', 'realizada_com_atraso', 3, id FROM operacao.execucoes;
        RAISE EXCEPTION 'aceitou realizada sem diferenca_min';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: realizada sem diferenca_min barrada'; END;
  -- tolerância com decimal: 3,1 de diferença com tolerância 3,1 é no horário (em float4 contra float8 dava falso)
  BEGIN INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, diferenca_min, status, tolerancia_min, execucao_id)
        SELECT sid, '701A-10', '2026-09-15', 3.1, 'realizada_no_horario', 3.1, id FROM operacao.execucoes;
        RAISE EXCEPTION 'desfaz_teste';
  EXCEPTION WHEN check_violation THEN RAISE EXCEPTION 'tolerância 3,1 rejeitada: tipos float4 e float8 misturados';
            WHEN raise_exception THEN RAISE NOTICE 'ok: tolerância 3,1 aceita'; END;
  -- 3,00000001 min é atraso (como no R, em double); em real viraria 3,0
  INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, diferenca_min, status, tolerancia_min, execucao_id)
  SELECT sid, '701A-10', '2026-09-15', 3.00000001, 'realizada_com_atraso', 3, id FROM operacao.execucoes;
  RAISE NOTICE 'ok: 3,00000001 min aceito como atraso';
END $$;
INSERT INTO indicadores.icv_faixas (codigo_linha, data_servico, sentido, hora, tipo_dia,
                                    programadas, realizadas, quase_completas, inconclusivas, execucao_id) VALUES
  ('701A-10', '2026-09-15', 0, 7, 'U', 4, 5, 0, 0, :'exec_id'),   -- limitado a 4/4 = 1
  ('701A-10', '2026-09-15', 0, 8, 'U', 4, 2, 1, 0, :'exec_id'),   -- período PM: 6/8
  ('701A-10', '2026-09-15', 0, 12, 'U', 2, 1, 0, 0, :'exec_id'),  -- EP: 1/2
  ('701A-10', '2026-09-15', 1, 7, 'U', 2, 2, 0, 0, :'exec_id');   -- sentido 1: 1
-- sentido 0: média(0,75; 0,5) = 0,625; sentido 1: 1 -> linha: 0,8125
DO $$ BEGIN
  IF (SELECT icv_pct FROM indicadores.icv_mensal) <> 81.25 THEN
    RAISE EXCEPTION 'ICV errado: %', (SELECT icv_pct FROM indicadores.icv_mensal); END IF;
  IF (SELECT periodo FROM indicadores.icv_faixas WHERE hora = 12) <> 'EP' THEN
    RAISE EXCEPTION 'período não saiu da hora'; END IF;
  BEGIN INSERT INTO indicadores.icv_faixas (codigo_linha, data_servico, sentido, hora, tipo_dia, programadas, realizadas, execucao_id)
        SELECT '701A-10', '2026-09-15', 0, 24, 'U', 1, 1, id FROM operacao.execucoes;
        RAISE EXCEPTION 'aceitou hora 24';
  EXCEPTION WHEN check_violation THEN RAISE NOTICE 'ok: hora fora de 0–23 barrada'; END;
END $$;
SELECT * FROM indicadores.icv_mensal;

\echo '10. uuid_v7: versão 7, variante RFC, e ids crescem com o tempo'
DO $$ DECLARE a uuid; b uuid; ids uuid[]; BEGIN
  a := public.uuid_v7(); PERFORM pg_sleep(0.01); b := public.uuid_v7();
  IF substr(a::text, 15, 1) <> '7' THEN RAISE EXCEPTION 'versão errada: %', a; END IF;
  IF substr(a::text, 20, 1) NOT IN ('8', '9', 'a', 'b') THEN RAISE EXCEPTION 'variante errada: %', a; END IF;
  IF NOT (a < b) THEN RAISE EXCEPTION 'ids não crescem: % >= %', a, b; END IF;
  IF abs(extract(epoch FROM now()) * 1000 - ('x' || replace(substr(a::text, 1, 13), '-', ''))::bit(48)::bigint) > 5000 THEN
    RAISE EXCEPTION 'o instante embutido não bate com o relógio: %', a; END IF;
  SELECT array_agg(public.uuid_v7()) INTO ids FROM generate_series(1, 1000);
  IF (SELECT count(DISTINCT x) FROM unnest(ids) x) <> 1000 THEN RAISE EXCEPTION 'ids repetidos'; END IF;
  RAISE NOTICE 'ok: uuid_v7 versão 7, crescente e único (ex.: %)', a;
END $$;

\echo '11. nenhuma coluna bigint em tabela; ids e FKs são uuid'
DO $$ BEGIN
  -- só tabelas: nas views, count(*) devolve bigint e isso não é id.
  IF EXISTS (SELECT 1 FROM information_schema.columns c
               JOIN information_schema.tables t USING (table_schema, table_name)
              WHERE t.table_type = 'BASE TABLE' AND c.data_type = 'bigint'
                AND c.table_schema IN ('bruto', 'referencia', 'operacao', 'indicadores')) THEN
    RAISE EXCEPTION 'sobrou coluna bigint em tabela: %', (SELECT string_agg(c.table_schema || '.' || c.table_name || '.' || c.column_name, ', ')
       FROM information_schema.columns c JOIN information_schema.tables t USING (table_schema, table_name)
      WHERE t.table_type = 'BASE TABLE' AND c.data_type = 'bigint'
        AND c.table_schema IN ('bruto', 'referencia', 'operacao', 'indicadores'));
  END IF;
  IF (SELECT count(*) FROM information_schema.columns
       WHERE table_schema IN ('bruto', 'operacao', 'indicadores') AND data_type = 'uuid') < 15 THEN
    RAISE EXCEPTION 'esperava ao menos 15 colunas uuid'; END IF;
  RAISE NOTICE 'ok: % colunas uuid',
    (SELECT count(*) FROM information_schema.columns
      WHERE table_schema IN ('bruto', 'operacao', 'indicadores') AND data_type = 'uuid');
END $$;

\echo '12. partições: a manutenção cria meses novos com trigger e índices; a saúde aponta dado na default'
UPDATE partman.part_config SET premake = 5 WHERE parent_table = 'bruto.posicoes_gps';
SELECT partman.run_maintenance('bruto.posicoes_gps');
DO $$ DECLARE ult regclass; n_idx int; BEGIN
  SELECT c.oid::regclass INTO ult FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
   WHERE i.inhparent = 'bruto.posicoes_gps'::regclass AND c.relname <> 'posicoes_gps_default'
   ORDER BY c.relname DESC LIMIT 1;
  IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgrelid = ult AND tgname = 'posicoes_gps_so_insercao') THEN
    RAISE EXCEPTION 'a partição nova % não herdou o trigger', ult; END IF;
  SELECT count(*) INTO n_idx FROM pg_index WHERE indrelid = ult;
  IF n_idx < 3 THEN RAISE EXCEPTION 'a partição nova % tem só % índices', ult, n_idx; END IF;
  IF NOT (SELECT ok FROM operacao.saude_particoes) THEN
    RAISE EXCEPTION 'saúde das partições devia estar ok: %', (SELECT row(s.*) FROM operacao.saude_particoes s); END IF;
  RAISE NOTICE 'ok: partição nova % com trigger e % índices', ult, n_idx;
END $$;
BEGIN;   -- GPS com relógio errado cai na default e a saúde acusa (desfeito no fim)
INSERT INTO bruto.posicoes_gps VALUES ('2031-01-01 00:00:00+00', 99999, 689, 1, NULL, -23.5, -46.6, '00000000-0000-7000-8000-000000001002'::uuid);
DO $$ BEGIN
  IF (SELECT ok FROM operacao.saude_particoes) THEN RAISE EXCEPTION 'saúde não acusou dado na default'; END IF;
  RAISE NOTICE 'ok: saúde acusou % linha(s) na default', (SELECT linhas_na_default FROM operacao.saude_particoes);
END $$;
ROLLBACK;
SELECT * FROM operacao.saude_particoes;

\echo '13. retenção: mês antigo sai da tabela mãe, mas a tabela continua lá (nada se perde antes do arquivo)'
UPDATE partman.part_config SET retention = '1 day' WHERE parent_table = 'bruto.posicoes_gps';
SELECT partman.run_maintenance('bruto.posicoes_gps');
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM bruto.posicoes_gps WHERE prefixo = 21173) THEN
    RAISE EXCEPTION 'setembro devia ter sido desanexado'; END IF;
  IF to_regclass('bruto.posicoes_gps_p20260901') IS NULL THEN
    RAISE EXCEPTION 'a partição desanexada sumiu: o dado se perdeu'; END IF;
  IF (SELECT count(*) FROM bruto.posicoes_gps_p20260901) <> 2 THEN
    RAISE EXCEPTION 'a partição desanexada perdeu linhas'; END IF;
  RAISE NOTICE 'ok: setembro desanexado com as % linhas intactas, pronto para exportar e apagar',
    (SELECT count(*) FROM bruto.posicoes_gps_p20260901);
END $$;
UPDATE partman.part_config SET retention = '3 months' WHERE parent_table = 'bruto.posicoes_gps';
\echo 'TODOS OS TESTES PASSARAM'
