-- Carrega as saídas do pipeline R no banco, cada etapa com o papel do serviço
-- que a faria (coletor ou worker). Os CHECKs e FKs validam os dados reais.
\set ON_ERROR_STOP on
SET timezone = 'UTC';

CREATE SCHEMA validacao;
CREATE TABLE validacao.auditoria (scheduled_trip_id text, service_date date, line_id text, direction_id smallint,
  tipo_dia text, scheduled_departure_utc timestamptz, observed_departure_utc timestamptz,
  departure_difference_minutes double precision, observed_trip_id text, observed_vehicle_id text, audit_status text,
  confidence_level text, data_coverage_status text, frac_min_dados double precision, pct_tracado double precision,
  pct_cobertura double precision, evidence_summary text, so_lacuna_aberta boolean, schedule_quality_flag text,
  tolerancia_min double precision, diagnostico_frota text, detalhe_frota text, veiculos_possiveis text, lacunas_ids text);
CREATE TABLE validacao.icv (line_id text, service_date date, tipo_dia text, sentido smallint, hora smallint, periodo text,
  programadas int, realizadas int, quase_completas int, inconclusivas int);
CREATE TABLE validacao.viagens (trip_id text, line_id text, vehicle_id text, service_date date, direction_id smallint,
  inicio_obs timestamptz, fim_obs timestamptz, partida_em timestamptz, chegada_em timestamptz,
  duration_minutes double precision, distance_km double precision, vel_media_kmh double precision,
  observation_coverage double precision, frac_observada double precision, lacuna_max_s double precision,
  trajectory_status text, confidence_level text, route_match_status text, origin_evidence text,
  destination_evidence text, alertas text, gtfs_versao text);
CREATE TABLE validacao.lacunas (gid int, line_id text, vehicle_id text, service_date date, inicio_em timestamptz,
  fim_em timestamptz, s0_km double precision, s1_km double precision, viagens_max double precision, tipo text);
CREATE TABLE validacao.posicoes (horario_gps timestamptz, prefixo text, line_id text, cl int, direction_sl smallint,
  latitude double precision, longitude double precision, qc_dup_instante boolean, qc_dup_exato boolean,
  qc_coord_invalida boolean, qc_ts_invalido boolean);

\copy validacao.auditoria FROM '/trab/dados/auditoria.csv' CSV HEADER
\copy validacao.icv FROM '/trab/dados/icv_faixas.csv' CSV HEADER
\copy validacao.viagens FROM '/trab/dados/viagens.csv' CSV HEADER
\copy validacao.lacunas FROM '/trab/dados/lacunas.csv' CSV HEADER
\copy validacao.posicoes FROM '/trab/dados/posicoes.csv' CSV HEADER

GRANT USAGE ON SCHEMA validacao TO onibus_coletor, onibus_worker;
GRANT SELECT ON ALL TABLES IN SCHEMA validacao TO onibus_coletor, onibus_worker;

\echo '== coletor: catálogos, programação, GTFS e posições'
SET ROLE onibus_coletor;
INSERT INTO referencia.linhas (codigo_linha)
SELECT line_id FROM validacao.auditoria UNION SELECT line_id FROM validacao.icv
UNION SELECT line_id FROM validacao.viagens UNION SELECT line_id FROM validacao.lacunas
UNION SELECT line_id FROM validacao.posicoes;

INSERT INTO referencia.linhas_olhovivo (cod_linha_sentido, letreiro, sentido_api, visto_primeiro_em, visto_ultimo_em)
SELECT cl, min(line_id), min(direction_sl), min(horario_gps), max(horario_gps)
  FROM validacao.posicoes GROUP BY cl;

INSERT INTO referencia.coletas_programacao (data_coleta, fonte, iniciada_em, observacao)
VALUES ('2026-10-25', 'raspagem_navegador', '2026-10-25 12:00+00', 'validação com as saídas do R')
RETURNING id AS coleta \gset
INSERT INTO referencia.gtfs_versoes (nome, hash_sha256, capturado_em, arquivo_origem)
VALUES ('20261104', repeat('0', 64), '2026-11-04', 'validacao')
RETURNING id AS gtfs \gset

SELECT public.uuid_v7() AS ciclo \gset
BEGIN;
WITH novas AS (
  INSERT INTO bruto.posicoes_gps (horario_gps, prefixo, cod_linha_sentido, sentido_api, latitude, longitude, ciclo_id)
  SELECT horario_gps, prefixo::int, cl, direction_sl, latitude, longitude, :'ciclo'::uuid
    FROM validacao.posicoes ORDER BY horario_gps
  ON CONFLICT (prefixo, horario_gps) DO NOTHING
  RETURNING 1)
INSERT INTO bruto.ciclos_coleta (id, fonte, alvo, iniciado_em, concluido_em, status, n_recebidos, n_inseridos,
                                 n_rejeitados, versao_coletor)
SELECT :'ciclo'::uuid, 'olhovivo_posicao', 'validação R', now(), now(), 'ok',
       (SELECT count(*) FROM validacao.posicoes), (SELECT count(*) FROM novas), 0, 'validacao';
COMMIT;
RESET ROLE;

\echo '== worker: execuções, saídas, viagens, lacunas, auditoria e ICV'
SET ROLE onibus_worker;
INSERT INTO operacao.execucoes (etapa, iniciada_em, concluida_em, status, versao_algoritmo, versao_regra, parametros)
VALUES ('mapmatching', now(), now(), 'ok', 'mm-ciclo-0.1.0', NULL, '{}') RETURNING id AS exec_mm \gset
INSERT INTO operacao.execucoes (etapa, iniciada_em, concluida_em, status, versao_algoritmo, versao_regra, parametros)
VALUES ('frota', now(), now(), 'ok', 'mm-ciclo-0.1.0', NULL, '{"lacuna_min_s": 600}') RETURNING id AS exec_frota \gset
INSERT INTO operacao.execucoes (etapa, iniciada_em, concluida_em, status, versao_algoritmo, versao_regra, parametros)
VALUES ('auditoria', now(), now(), 'ok', 'mm-ciclo-0.1.0', 'ipp-0.2.0',
        '{"tolerancia_min": 3, "tolerancia_ampliada_min": 4}') RETURNING id AS exec_aud \gset
INSERT INTO operacao.execucoes (etapa, iniciada_em, concluida_em, status, versao_algoritmo, versao_regra, parametros)
VALUES ('icv', now(), now(), 'ok', 'mm-ciclo-0.1.0', 'icv-0.1.0', '{}') RETURNING id AS exec_icv \gset

CREATE TEMP TABLE mapa_saida  AS SELECT scheduled_trip_id, public.uuid_v7() AS id FROM validacao.auditoria;
CREATE TEMP TABLE mapa_viagem AS SELECT trip_id, public.uuid_v7() AS id FROM validacao.viagens;
CREATE TEMP TABLE mapa_lacuna AS SELECT line_id, gid, public.uuid_v7() AS id FROM validacao.lacunas;

INSERT INTO operacao.saidas_programadas (id, codigo_linha, data_servico, sentido, tipo_dia, ordem, partida_prevista,
                                         coleta_id, coleta_posterior_a_data)
SELECT m.id, a.line_id, a.service_date, a.direction_id, a.tipo_dia,
       row_number() OVER (PARTITION BY a.line_id, a.service_date, a.direction_id
                          ORDER BY a.scheduled_departure_utc, a.scheduled_trip_id),
       a.scheduled_departure_utc, :coleta, coalesce(a.schedule_quality_flag = 'raspagem_posterior_a_data', false)
  FROM validacao.auditoria a JOIN mapa_saida m USING (scheduled_trip_id);

INSERT INTO operacao.viagens (id, codigo_linha, prefixo, data_servico, sentido, inicio_obs, fim_obs, partida_em,
  chegada_em, duracao_min, distancia_km, vel_media_kmh, cobertura, frac_tracado, lacuna_max_s, status_trajetoria,
  nivel_confianca, aderencia_rota, evidencia_origem, evidencia_destino, alertas, gtfs_versao_id, execucao_id)
SELECT m.id, v.line_id, v.vehicle_id::int, v.service_date, v.direction_id, v.inicio_obs, v.fim_obs, v.partida_em,
       v.chegada_em, v.duration_minutes, v.distance_km, v.vel_media_kmh, v.observation_coverage, v.frac_observada,
       v.lacuna_max_s, v.trajectory_status, v.confidence_level, v.route_match_status, v.origin_evidence,
       v.destination_evidence, v.alertas::text[], :gtfs, :'exec_mm'::uuid
  FROM validacao.viagens v JOIN mapa_viagem m USING (trip_id);

INSERT INTO operacao.lacunas_veiculo (id, codigo_linha, prefixo, data_servico, inicio_em, fim_em, km_inicio, km_fim,
                                      viagens_cabiveis, tipo, execucao_id)
SELECT m.id, l.line_id, l.vehicle_id::int, l.service_date, l.inicio_em, l.fim_em, l.s0_km, l.s1_km, l.viagens_max,
       l.tipo, :'exec_frota'::uuid
  FROM validacao.lacunas l JOIN mapa_lacuna m USING (line_id, gid);

INSERT INTO indicadores.saidas_auditadas (saida_id, codigo_linha, data_servico, viagem_id, prefixo_observado,
  partida_observada, diferenca_min, status, nivel_confianca, cobertura_dados, frac_min_dados, pct_tracado,
  pct_cobertura, evidencia, so_lacuna_aberta, diagnostico_frota, detalhe_frota, veiculos_possiveis,
  tolerancia_min, tolerancia_ampliada_min, execucao_id)
SELECT ms.id, a.line_id, a.service_date, mv.id, a.observed_vehicle_id::int, a.observed_departure_utc,
       a.departure_difference_minutes, a.audit_status, a.confidence_level, a.data_coverage_status, a.frac_min_dados,
       a.pct_tracado, a.pct_cobertura, a.evidence_summary, a.so_lacuna_aberta, a.diagnostico_frota, a.detalhe_frota,
       a.veiculos_possiveis::int[], a.tolerancia_min, 4, :'exec_aud'::uuid
  FROM validacao.auditoria a
  JOIN mapa_saida ms USING (scheduled_trip_id)
  LEFT JOIN mapa_viagem mv ON mv.trip_id = a.observed_trip_id;

INSERT INTO indicadores.saidas_lacunas (saida_id, lacuna_id)
SELECT DISTINCT ms.id, ml.id
  FROM validacao.auditoria a
  JOIN mapa_saida ms USING (scheduled_trip_id)
  CROSS JOIN LATERAL unnest(string_to_array(NULLIF(a.lacunas_ids, ''), ',')) AS g(gid)
  JOIN mapa_lacuna ml ON ml.line_id = a.line_id AND ml.gid = g.gid::int;

INSERT INTO indicadores.icv_faixas (codigo_linha, data_servico, sentido, hora, tipo_dia, programadas, realizadas,
                                    quase_completas, inconclusivas, execucao_id)
SELECT line_id, service_date, sentido, hora, tipo_dia, programadas, realizadas, quase_completas, inconclusivas,
       :'exec_icv'::uuid
  FROM validacao.icv;
RESET ROLE;

\echo '== contagens: R x banco'
SELECT 'saidas'   AS tabela, (SELECT count(*) FROM validacao.auditoria) AS no_r, (SELECT count(*) FROM indicadores.saidas_auditadas) AS no_banco
UNION ALL SELECT 'viagens', (SELECT count(*) FROM validacao.viagens), (SELECT count(*) FROM operacao.viagens)
UNION ALL SELECT 'lacunas', (SELECT count(*) FROM validacao.lacunas), (SELECT count(*) FROM operacao.lacunas_veiculo)
UNION ALL SELECT 'icv_faixas', (SELECT count(*) FROM validacao.icv), (SELECT count(*) FROM indicadores.icv_faixas)
UNION ALL SELECT 'saidas com viagem', (SELECT count(*) FROM validacao.auditoria WHERE observed_trip_id IS NOT NULL),
                                     (SELECT count(*) FROM indicadores.saidas_auditadas WHERE viagem_id IS NOT NULL)
UNION ALL SELECT 'saida-lacuna', (SELECT count(*) FROM (SELECT DISTINCT a.scheduled_trip_id, g FROM validacao.auditoria a,
                                   unnest(string_to_array(NULLIF(a.lacunas_ids, ''), ',')) g) x),
                                (SELECT count(*) FROM indicadores.saidas_lacunas)
UNION ALL SELECT 'posicoes recebidas', (SELECT count(*) FROM validacao.posicoes), (SELECT n_recebidos FROM bruto.ciclos_coleta WHERE versao_coletor = 'validacao')
UNION ALL SELECT 'posicoes (veiculo, instante) distintas', (SELECT count(*) FROM (SELECT DISTINCT prefixo, horario_gps FROM validacao.posicoes) x),
                                (SELECT count(*) FROM bruto.posicoes_gps);
SELECT 'cl com mais de uma linha ou sentido' AS checagem, count(*) AS casos
  FROM (SELECT cl FROM validacao.posicoes GROUP BY cl HAVING count(DISTINCT (line_id, direction_sl)) > 1) x
UNION ALL
SELECT 'período do ICV diferente do R', count(*)
  FROM validacao.icv r JOIN indicadores.icv_faixas f
    ON f.codigo_linha = r.line_id AND f.data_servico = r.service_date AND f.sentido = r.sentido AND f.hora = r.hora
 WHERE f.periodo <> r.periodo;
SELECT tableoid::regclass AS particao, count(*) FROM bruto.posicoes_gps GROUP BY 1 ORDER BY 1;
