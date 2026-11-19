-- =============================================================================
-- Modelo do banco: Auditoria Cidadã Contínua dos Ônibus de São Paulo
-- PostgreSQL 16+ com PostGIS 3 e pg_partman 5
--
-- Como rodar, num banco VAZIO e com o usuário dono do banco:
--
--   psql -v ON_ERROR_STOP=1 -d onibus -f esquema.sql
--   psql -v ON_ERROR_STOP=1 -v inicio_particoes=2026-08-01 -d onibus -f esquema.sql
--
-- inicio_particoes é o mês da primeira partição de bruto.posicoes_gps (padrão:
-- 3 meses antes do mês atual). Tudo roda numa transação: se algo falhar, nada
-- fica criado. Depois, agende a manutenção das partições (ver o fim do arquivo).
--
-- É também a referência para escrever as migrações Alembic (T6): cada bloco
-- "== Tn ==" vira uma ou mais migrações. Testes: teste.sql.
--
-- Convenções (ADR-001):
--   * nomes em português, snake_case;
--   * todo instante é timestamptz (guardado em UTC); hora local só na exibição;
--   * dia de serviço começa às 03:00 de São Paulo;
--   * sentido: 0 = ida, 1 = volta (a Olho Vivo usa 1/2 em "sl": fica em sentido_api;
--     a regra é sentido = sentido_api - 1, como no R: direction_id = sl - 1);
--   * a mesma chave de linha (ex.: '701A-10') vale na Olho Vivo ("c"), no GTFS
--     (route_id) e na API de itinerários ("codigo");
--   * listas fechadas de valores: text + CHECK (mais fácil de migrar que ENUM);
--   * contagens são guardadas; percentuais são calculados nas views;
--   * ids das tabelas de log e de resultado são UUID v7 (public.uuid_v7()), gerados
--     no banco ou, de preferência, pelo cliente; o coletor não precisa de nextval().
--     Tabelas de referência pequenas (versões de GTFS, coletas) mantêm integer.
-- =============================================================================

\set ON_ERROR_STOP on
\if :{?inicio_particoes}
\else
  \set inicio_particoes ''
\endif

BEGIN;
SET LOCAL timezone = 'UTC';   -- as fronteiras das partições ficam em UTC

DO $$ BEGIN
  IF current_setting('server_version_num')::int < 160000 THEN
    RAISE EXCEPTION 'este esquema foi testado no PostgreSQL 16+; o servidor é %', version();
  END IF;
END $$;

-- == T5: extensões e schemas ==================================================
CREATE EXTENSION IF NOT EXISTS postgis;
CREATE SCHEMA IF NOT EXISTS partman;
CREATE EXTENSION IF NOT EXISTS pg_partman SCHEMA partman;

CREATE SCHEMA bruto;        -- o que as fontes devolveram; só INSERT
CREATE SCHEMA referencia;   -- GTFS, programação, catálogos
CREATE SCHEMA operacao;     -- viagens, passagens, frota
CREATE SCHEMA indicadores;  -- resultados calculados

-- Bloqueia UPDATE, DELETE e TRUNCATE no bruto até para o dono da tabela
-- (defesa extra além dos GRANTs). Arquivar meses antigos usa DETACH/DROP de
-- partição, que não passa por aqui.
CREATE FUNCTION bruto.bloquear_alteracao() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'schema bruto é só de inserção (% em %)', TG_OP, TG_TABLE_NAME
    USING ERRCODE = 'insufficient_privilege';
END $$;

-- UUID versão 7: os 48 primeiros bits são o instante em milissegundos, então os
-- ids crescem com o tempo e as inserções nas chaves primárias continuam
-- "no fim do índice", como numa sequência. gen_random_uuid() (v4, totalmente
-- aleatório) espalharia cada inserção pelo índice inteiro. O Postgres 16 não
-- tem uuidv7() nativo (vem no 18); esta função o substitui e pode ser trocada
-- pela nativa depois, sem mudar nenhuma tabela.
CREATE FUNCTION public.uuid_v7() RETURNS uuid
LANGUAGE sql VOLATILE PARALLEL SAFE AS $$
  SELECT encode(
    set_bit(set_bit(
      overlay(uuid_send(gen_random_uuid())
              PLACING substring(int8send((extract(epoch FROM clock_timestamp()) * 1000)::bigint) FROM 3)
              FROM 1 FOR 6),
      52, 1), 53, 1),
    'hex')::uuid
$$;

-- == T8: ciclos de coleta (um por chamada a uma fonte) ========================
-- Registra toda chamada, com sucesso ou falha. Serve para achar lacunas
-- (US13) e para a saúde da coleta (US30). Escrito numa única inserção, no fim
-- do ciclo, na mesma transação das posições (ver FK deferida abaixo).
CREATE TABLE bruto.ciclos_coleta (
  id              uuid DEFAULT public.uuid_v7() PRIMARY KEY,
  fonte           text        NOT NULL CHECK (fonte IN
                    ('olhovivo_posicao', 'itinerarios_catalogo', 'itinerarios_linha', 'gtfs')),
  alvo            text,                       -- ex.: código da linha consultada
  iniciado_em     timestamptz NOT NULL,
  concluido_em    timestamptz NOT NULL,
  status          text        NOT NULL CHECK (status IN
                    ('ok', 'erro_rede', 'erro_http', 'erro_autenticacao', 'resposta_invalida')),
  http_status     smallint,
  hora_api        text,                       -- campo "hr" da Olho Vivo (HH:MM, hora local)
  n_recebidos     integer CHECK (n_recebidos >= 0),   -- registros que vieram na resposta
  n_inseridos     integer CHECK (n_inseridos >= 0),   -- gravados; a diferença eram repetidos
  n_rejeitados    integer CHECK (n_rejeitados >= 0),  -- descartados pelo coletor (coordenada ou horário inválido)
  arquivo_bruto   text,                       -- chave do JSON original .gz (disco ou S3)
  versao_coletor  text        NOT NULL,
  erro            text,
  CHECK (concluido_em >= iniciado_em),
  CHECK (n_inseridos IS NULL OR n_recebidos IS NULL
         OR n_inseridos + coalesce(n_rejeitados, 0) <= n_recebidos),
  CHECK (status = 'ok' OR erro IS NOT NULL)   -- toda falha diz o porquê
);
CREATE INDEX ciclos_coleta_fonte_inicio_idx ON bruto.ciclos_coleta (fonte, iniciado_em);
CREATE TRIGGER ciclos_coleta_so_insercao BEFORE UPDATE OR DELETE ON bruto.ciclos_coleta
  FOR EACH ROW EXECUTE FUNCTION bruto.bloquear_alteracao();
CREATE TRIGGER ciclos_coleta_sem_truncate BEFORE TRUNCATE ON bruto.ciclos_coleta
  FOR EACH STATEMENT EXECUTE FUNCTION bruto.bloquear_alteracao();

-- == T7: posições GPS (Olho Vivo /Posicao) =====================================
-- Uma linha por (veículo, instante do GPS). A Olho Vivo repete o mesmo "ta" em
-- consultas seguidas (o GPS atualiza a cada ~45 s e consultamos a cada 30 s);
-- a chave primária descarta a repetição: INSERT ... ON CONFLICT DO NOTHING.
--
-- Campos por linha da resposta ("l") que são iguais para todos os veículos
-- daquela linha (c, lt0, lt1, qv) NÃO se repetem aqui: c/lt0/lt1 ficam em
-- referencia.linhas_olhovivo e qv é contagem. A resposta íntegra fica no
-- JSON .gz apontado por ciclos_coleta.arquivo_bruto.
--
-- Os CHECKs rejeitam o lote inteiro: o coletor filtra antes as linhas
-- inválidas e conta em ciclos_coleta.n_rejeitados.
CREATE TABLE bruto.posicoes_gps (
  horario_gps        timestamptz      NOT NULL,   -- "ta" (a API manda em UTC)
  prefixo            integer          NOT NULL,   -- "p" (5 dígitos nos dados atuais)
  cod_linha_sentido  integer          NOT NULL,   -- "cl": a Olho Vivo tem um cl por linha E sentido
  sentido_api        smallint         NOT NULL CHECK (sentido_api IN (1, 2)),  -- "sl"
  acessivel          boolean,                     -- "a" (o coletor em R descartava)
  latitude           double precision NOT NULL CHECK (latitude  BETWEEN -90  AND 90),   -- "py"
  longitude          double precision NOT NULL CHECK (longitude BETWEEN -180 AND 180),  -- "px"
  ciclo_id           uuid             NOT NULL
                     REFERENCES bruto.ciclos_coleta (id) DEFERRABLE INITIALLY DEFERRED,
  PRIMARY KEY (prefixo, horario_gps)              -- também serve "um ônibus num período"
) PARTITION BY RANGE (horario_gps);

-- Leitura típica do worker e do app: "uma linha num dia".
CREATE INDEX posicoes_gps_linha_horario_idx ON bruto.posicoes_gps (cod_linha_sentido, horario_gps);
-- Mapa ao vivo: "últimos minutos". BRIN é minúsculo e serve bem a dados que
-- chegam quase em ordem de tempo.
CREATE INDEX posicoes_gps_horario_brin ON bruto.posicoes_gps USING brin (horario_gps);

-- O trigger de linha é clonado em toda partição, inclusive nas criadas depois.
CREATE TRIGGER posicoes_gps_so_insercao BEFORE UPDATE OR DELETE ON bruto.posicoes_gps
  FOR EACH ROW EXECUTE FUNCTION bruto.bloquear_alteracao();
CREATE TRIGGER posicoes_gps_sem_truncate BEFORE TRUNCATE ON bruto.posicoes_gps
  FOR EACH STATEMENT EXECUTE FUNCTION bruto.bloquear_alteracao();

-- Partição mensal. A partição default recebe horários fora das faixas criadas
-- (GPS com relógio errado): monitorar em operacao.saude_particoes, porque dado
-- nela impede criar a partição daquele mês.
SELECT partman.create_parent(
  p_parent_table    => 'bruto.posicoes_gps',
  p_control         => 'horario_gps',
  p_interval        => '1 month',
  p_premake         => 3,
  p_start_partition => COALESCE(NULLIF(:'inicio_particoes', ''),
                                to_char(date_trunc('month', now()) - interval '3 months', 'YYYY-MM-DD'))
);
-- Meses antigos: a manutenção só DESANEXA (retention_keep_table); o job de
-- arquivo exporta a tabela desanexada para Parquet, confere e então faz o DROP.
UPDATE partman.part_config
   SET retention = '3 months', retention_keep_table = true, infinite_time_partitions = true
 WHERE parent_table = 'bruto.posicoes_gps';

-- == T5/T8: catálogos de linhas ===============================================
-- Linha canônica: uma por código. Alimentada pelo catálogo da API de
-- itinerários; operacao e indicadores apontam para cá.
CREATE TABLE referencia.linhas (
  -- '701A-10', mas o catálogo também tem '209P-1', 'CPTM L07-0' e 'METRÔ L1-0':
  -- não dá para impor um formato; só barra vazio e espaço nas pontas.
  codigo_linha  text        PRIMARY KEY CHECK (codigo_linha <> '' AND codigo_linha = btrim(codigo_linha)),
  criada_em     timestamptz NOT NULL DEFAULT now()
);

-- O que a Olho Vivo diz de cada "cl". Atualizada pelo coletor quando aparece
-- um cl novo (e, no máximo uma vez por hora, o visto_ultimo_em).
CREATE TABLE referencia.linhas_olhovivo (
  cod_linha_sentido  integer     PRIMARY KEY,     -- "cl"
  letreiro           text        NOT NULL,        -- "c", ex.: '701A-10'
  sentido_api        smallint    NOT NULL CHECK (sentido_api IN (1, 2)),
  letreiro_destino   text,                        -- "lt0"
  letreiro_origem    text,                        -- "lt1"
  visto_primeiro_em  timestamptz NOT NULL,
  visto_ultimo_em    timestamptz NOT NULL,
  CHECK (visto_ultimo_em >= visto_primeiro_em)
);
CREATE INDEX linhas_olhovivo_letreiro_idx ON referencia.linhas_olhovivo (letreiro);

-- Feriados: hoje é uma lista fixa em 00_config.R. Viram dado.
CREATE TABLE referencia.feriados (
  data               date PRIMARY KEY,
  nome               text NOT NULL,
  tipo_dia_aplicado  text NOT NULL DEFAULT 'D' CHECK (tipo_dia_aplicado IN ('U', 'S', 'D'))
);

-- == T8: GTFS versionado ======================================================
-- Cada .zip capturado vira uma versão (hash garante que não se repete). Todas
-- as tabelas levam gtfs_versao_id na chave, então várias versões convivem.
-- Apagar uma versão apaga tudo dela (ON DELETE CASCADE).
CREATE TABLE referencia.gtfs_versoes (
  id              integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  nome            text        NOT NULL UNIQUE,    -- ex.: '20261104'
  hash_sha256     text        NOT NULL UNIQUE CHECK (hash_sha256 ~ '^[0-9a-f]{64}$'),
  capturado_em    timestamptz NOT NULL,
  arquivo_origem  text        NOT NULL,           -- caminho/chave do .zip guardado
  ciclo_id        uuid REFERENCES bruto.ciclos_coleta (id)
);

CREATE TABLE referencia.gtfs_rotas (               -- routes.txt
  gtfs_versao_id    integer NOT NULL REFERENCES referencia.gtfs_versoes (id) ON DELETE CASCADE,
  route_id          text    NOT NULL,
  agency_id         text,
  nome_curto        text,                          -- route_short_name
  nome_longo        text,                          -- route_long_name
  tipo_rota         smallint,                      -- route_type (3 = ônibus)
  cor               text,
  cor_texto         text,
  PRIMARY KEY (gtfs_versao_id, route_id)
);

CREATE TABLE referencia.gtfs_calendario (          -- calendar.txt
  gtfs_versao_id  integer NOT NULL REFERENCES referencia.gtfs_versoes (id) ON DELETE CASCADE,
  service_id      text    NOT NULL,
  segunda boolean NOT NULL, terca boolean NOT NULL, quarta boolean NOT NULL,
  quinta  boolean NOT NULL, sexta boolean NOT NULL, sabado boolean NOT NULL,
  domingo boolean NOT NULL,
  data_inicio     date    NOT NULL,
  data_fim        date    NOT NULL,
  PRIMARY KEY (gtfs_versao_id, service_id),
  CHECK (data_fim >= data_inicio)
);

-- shapes.txt vira UMA LineString por shape (em vez de um ponto por linha):
-- menos linhas e operações espaciais diretas (ST_LineLocatePoint etc.).
CREATE TABLE referencia.gtfs_tracados (
  gtfs_versao_id  integer NOT NULL REFERENCES referencia.gtfs_versoes (id) ON DELETE CASCADE,
  shape_id        text    NOT NULL,
  geom            geometry(LineString, 4326) NOT NULL CHECK (ST_IsValid(geom)),
  comprimento_m   double precision GENERATED ALWAYS AS (ST_Length(geom::geography)) STORED,
  PRIMARY KEY (gtfs_versao_id, shape_id)
);
CREATE INDEX gtfs_tracados_geom_idx ON referencia.gtfs_tracados USING gist (geom);

CREATE TABLE referencia.gtfs_paradas (             -- stops.txt
  gtfs_versao_id  integer NOT NULL REFERENCES referencia.gtfs_versoes (id) ON DELETE CASCADE,
  stop_id         text    NOT NULL,
  nome            text,
  descricao       text,
  geom            geometry(Point, 4326) NOT NULL,  -- ST_X = lon, ST_Y = lat
  PRIMARY KEY (gtfs_versao_id, stop_id)
);
CREATE INDEX gtfs_paradas_geom_idx ON referencia.gtfs_paradas USING gist (geom);

CREATE TABLE referencia.gtfs_viagens (             -- trips.txt
  gtfs_versao_id  integer  NOT NULL,
  trip_id         text     NOT NULL,
  route_id        text     NOT NULL,
  service_id      text,
  letreiro        text,                            -- trip_headsign
  sentido         smallint NOT NULL CHECK (sentido IN (0, 1)),   -- direction_id
  shape_id        text,
  PRIMARY KEY (gtfs_versao_id, trip_id),
  FOREIGN KEY (gtfs_versao_id, route_id) REFERENCES referencia.gtfs_rotas ON DELETE CASCADE,
  FOREIGN KEY (gtfs_versao_id, shape_id) REFERENCES referencia.gtfs_tracados ON DELETE CASCADE
);
CREATE INDEX gtfs_viagens_rota_idx ON referencia.gtfs_viagens (gtfs_versao_id, route_id);

-- stop_times.txt. No GTFS da SPTrans o horário é relativo ao início da viagem, com
-- uma base qualquer por viagem (478P-10-0 começa em 14:00:00): só as diferenças
-- valem. Aqui fica o deslocamento até a primeira parada, em segundos (zero nela),
-- normalizado na carga: arrival_time menos o arrival_time da primeira parada.
-- Não é hora do relógio e não se converte para UTC (ADR-001, P6).
CREATE TABLE referencia.gtfs_horarios_paradas (
  gtfs_versao_id  integer  NOT NULL,
  trip_id         text     NOT NULL,
  sequencia       integer  NOT NULL,               -- stop_sequence
  stop_id         text     NOT NULL,
  chegada_s       integer CHECK (chegada_s >= 0),  -- arrival_time
  partida_s       integer CHECK (partida_s >= 0),  -- departure_time
  PRIMARY KEY (gtfs_versao_id, trip_id, sequencia),
  FOREIGN KEY (gtfs_versao_id, trip_id) REFERENCES referencia.gtfs_viagens ON DELETE CASCADE,
  FOREIGN KEY (gtfs_versao_id, stop_id) REFERENCES referencia.gtfs_paradas ON DELETE CASCADE,
  CHECK (partida_s IS NULL OR chegada_s IS NULL OR partida_s >= chegada_s)
);

-- frequencies.txt: o GTFS da SPTrans descreve a operação por intervalo.
CREATE TABLE referencia.gtfs_frequencias (
  gtfs_versao_id  integer NOT NULL,
  trip_id         text    NOT NULL,
  inicio_s        integer NOT NULL CHECK (inicio_s >= 0),   -- start_time
  fim_s           integer NOT NULL,                         -- end_time
  intervalo_s     integer NOT NULL CHECK (intervalo_s > 0), -- headway_secs
  PRIMARY KEY (gtfs_versao_id, trip_id, inicio_s),
  FOREIGN KEY (gtfs_versao_id, trip_id) REFERENCES referencia.gtfs_viagens ON DELETE CASCADE,
  CHECK (fim_s > inicio_s)
);

-- == T8: programação (API de itinerários da SPTrans) ===========================
-- Cada coleta é uma "fotografia" da programação; o histórico fica guardado,
-- porque a programação muda e a auditoria de um dia precisa da que valia nele.
CREATE TABLE referencia.coletas_programacao (
  id            integer GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  data_coleta   date        NOT NULL,
  fonte         text        NOT NULL CHECK (fonte IN ('api_itinerarios', 'raspagem_navegador')),
  iniciada_em   timestamptz NOT NULL,
  concluida_em  timestamptz,
  observacao    text,
  UNIQUE (data_coleta, fonte),
  CHECK (concluida_em IS NULL OR concluida_em >= iniciada_em)
);

-- Catálogo + cabeçalho do detalhe de cada linha, por coleta.
CREATE TABLE referencia.linhas_programacao (
  coleta_id         integer  NOT NULL REFERENCES referencia.coletas_programacao (id) ON DELETE CASCADE,
  codigo_linha      text     NOT NULL REFERENCES referencia.linhas (codigo_linha),
  cod_planejamento  integer  NOT NULL,             -- CdPjOID (vai no corpo do detalhe)
  area              smallint,                      -- AreCodVig / area
  nome              text,                          -- os acentos vêm corrompidos da API (METR�)
  consorcio         text,
  empresa           text,
  letreiro_ida      text,
  letreiro_volta    text,
  circular          boolean,
  exibida_em        text,                          -- linha principal onde o site exibe a auxiliar
  erro_api          text,                          -- preenchido quando a API respondeu erro
  PRIMARY KEY (coleta_id, codigo_linha)
);

-- Uma linha por horário de partida. minuto_servico deixa a ordem correta e a
-- montagem do instante trivial: horários antes de 03:00 pertencem ao dia de
-- serviço anterior e somam 1440 ('00:10' -> 1450).
CREATE TABLE referencia.partidas_programadas (
  coleta_id          integer  NOT NULL,
  codigo_linha       text     NOT NULL,
  sentido            smallint NOT NULL CHECK (sentido IN (0, 1)),
  tipo_dia           text     NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  ordem              smallint NOT NULL,            -- ordem na página
  horario            text     NOT NULL CHECK (horario ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  minuto_servico     smallint GENERATED ALWAYS AS (
                       (split_part(horario, ':', 1)::int * 60 + split_part(horario, ':', 2)::int
                        + CASE WHEN split_part(horario, ':', 1)::int < 3 THEN 1440 ELSE 0 END)::smallint
                     ) STORED,
  faixa_horaria      smallint NOT NULL CHECK (faixa_horaria BETWEEN 0 AND 23),
  veiculo_acessivel  boolean  NOT NULL,
  PRIMARY KEY (coleta_id, codigo_linha, sentido, tipo_dia, ordem),
  FOREIGN KEY (coleta_id, codigo_linha) REFERENCES referencia.linhas_programacao ON DELETE CASCADE
);

CREATE TABLE referencia.tempos_estimados (
  coleta_id     integer  NOT NULL,
  codigo_linha  text     NOT NULL,
  tipo_dia      text     NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  sentido       smallint NOT NULL CHECK (sentido IN (0, 1)),   -- inicial* = 0, final* = 1
  periodo       text     NOT NULL CHECK (periodo IN ('manha', 'entrepico', 'tarde')),
  minutos       smallint CHECK (minutos > 1),       -- NULL quando a API traz <= 1 (o site mostra "-")
  PRIMARY KEY (coleta_id, codigo_linha, tipo_dia, sentido, periodo),
  FOREIGN KEY (coleta_id, codigo_linha) REFERENCES referencia.linhas_programacao ON DELETE CASCADE
);

-- "horarios" da API: "04:00-23:20" separado em início e fim.
CREATE TABLE referencia.horarios_operacao (
  coleta_id     integer NOT NULL,
  codigo_linha  text    NOT NULL,
  tipo_dia      text    NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  inicio_ponto_inicial  text CHECK (inicio_ponto_inicial ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  fim_ponto_inicial     text CHECK (fim_ponto_inicial    ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  inicio_ponto_final    text CHECK (inicio_ponto_final   ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  fim_ponto_final       text CHECK (fim_ponto_final      ~ '^([01][0-9]|2[0-3]):[0-5][0-9]$'),
  PRIMARY KEY (coleta_id, codigo_linha, tipo_dia),
  FOREIGN KEY (coleta_id, codigo_linha) REFERENCES referencia.linhas_programacao ON DELETE CASCADE
);

CREATE TABLE referencia.itinerarios_vias (
  coleta_id     integer  NOT NULL,
  codigo_linha  text     NOT NULL,
  sentido       smallint NOT NULL CHECK (sentido IN (0, 1)),
  tipo_dia      text     NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  ordem         smallint NOT NULL,
  nome_via      text     NOT NULL,                 -- nomeVia
  numeracao     text,                              -- numVia, ex.: '353 - 523'
  PRIMARY KEY (coleta_id, codigo_linha, sentido, tipo_dia, ordem),
  FOREIGN KEY (coleta_id, codigo_linha) REFERENCES referencia.linhas_programacao ON DELETE CASCADE
);

CREATE TABLE referencia.eventos_via (
  coleta_id       integer  NOT NULL,
  codigo_linha    text     NOT NULL,
  sentido         smallint NOT NULL,
  tipo_dia        text     NOT NULL,
  ordem_via       smallint NOT NULL,
  evento_id       integer  NOT NULL,               -- id
  evento          text,                            -- ex.: 'Feira-livre - Rua Mourato Coelho'
  descricao       text,
  tipo_evento     text,                            -- ex.: 'Feira Semanal'
  periodicidade   smallint,
  dia_semana      smallint CHECK (dia_semana BETWEEN 0 AND 7),
  horario_inicio  text,
  horario_fim     text,
  itinerario_alternativo text,                     -- campo "itinerario"
  PRIMARY KEY (coleta_id, codigo_linha, sentido, tipo_dia, ordem_via, evento_id),
  FOREIGN KEY (coleta_id, codigo_linha, sentido, tipo_dia, ordem_via)
    REFERENCES referencia.itinerarios_vias ON DELETE CASCADE
);

-- Instante (UTC) de uma partida programada: data de serviço + minutos, no fuso
-- de São Paulo. STABLE porque depende das regras de fuso.
CREATE FUNCTION referencia.instante_programado(data_servico date, minuto_servico integer)
RETURNS timestamptz LANGUAGE sql STABLE PARALLEL SAFE AS $$
  SELECT (data_servico + make_interval(mins => minuto_servico)) AT TIME ZONE 'America/Sao_Paulo'
$$;

-- == operacao: resultado do worker (validado com as saídas do pipeline R) =====
CREATE TABLE operacao.execucoes (
  id                uuid DEFAULT public.uuid_v7() PRIMARY KEY,
  etapa             text        NOT NULL CHECK (etapa IN ('mapmatching', 'auditoria', 'frota', 'icv')),
  iniciada_em       timestamptz NOT NULL,
  concluida_em      timestamptz,
  status            text        NOT NULL CHECK (status IN ('rodando', 'ok', 'erro')),
  versao_algoritmo  text        NOT NULL,          -- ex.: 'mm-ciclo-0.1.0'
  versao_regra      text,                          -- ex.: 'ipp-0.2.0'
  parametros        jsonb       NOT NULL,          -- os limiares usados (tolerância, raio...)
  codigo_linha      text REFERENCES referencia.linhas (codigo_linha),
  data_inicio       date,
  data_fim          date,
  erro              text,
  CHECK (concluida_em IS NULL OR concluida_em >= iniciada_em),
  CHECK (data_fim IS NULL OR data_inicio IS NULL OR data_fim >= data_inicio),
  CHECK (status <> 'erro' OR erro IS NOT NULL)
);

-- A programação que vale em cada dia: o worker escolhe a coleta válida mais
-- próxima e materializa aqui. É a base da auditoria.
CREATE TABLE operacao.saidas_programadas (
  id                       uuid DEFAULT public.uuid_v7() PRIMARY KEY,
  codigo_linha             text     NOT NULL REFERENCES referencia.linhas (codigo_linha),
  data_servico             date     NOT NULL,
  sentido                  smallint NOT NULL CHECK (sentido IN (0, 1)),
  tipo_dia                 text     NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  ordem                    smallint NOT NULL,
  partida_prevista         timestamptz NOT NULL,
  coleta_id                integer  NOT NULL REFERENCES referencia.coletas_programacao (id),
  coleta_posterior_a_data  boolean  NOT NULL,       -- era schedule_quality_flag
  UNIQUE (codigo_linha, data_servico, sentido, ordem),
  UNIQUE (id, codigo_linha, data_servico)           -- alvo da FK composta de saidas_auditadas
);

CREATE TABLE operacao.viagens (
  id                  uuid DEFAULT public.uuid_v7() PRIMARY KEY,
  codigo_linha        text     NOT NULL REFERENCES referencia.linhas (codigo_linha),
  prefixo             integer  NOT NULL,
  data_servico        date     NOT NULL,
  sentido             smallint NOT NULL CHECK (sentido IN (0, 1)),   -- circular = 0
  inicio_obs          timestamptz NOT NULL,
  fim_obs             timestamptz NOT NULL,
  partida_em          timestamptz,
  chegada_em          timestamptz,
  duracao_min         double precision CHECK (duracao_min >= 0),
  distancia_km        double precision CHECK (distancia_km >= 0),
  vel_media_kmh       double precision CHECK (vel_media_kmh >= 0),
  cobertura           double precision CHECK (cobertura BETWEEN 0 AND 1),        -- observation_coverage
  frac_tracado        double precision CHECK (frac_tracado BETWEEN 0 AND 1),     -- frac_observada
  lacuna_max_s        integer CHECK (lacuna_max_s >= 0),
  status_trajetoria   text NOT NULL CHECK (status_trajetoria IN
                        ('viagem_confirmada', 'viagem_provavel', 'trajetoria_parcial', 'observacao_inconclusiva')),
  nivel_confianca     text NOT NULL CHECK (nivel_confianca IN ('alta', 'media', 'baixa', 'muito_baixa')),
  aderencia_rota      text CHECK (aderencia_rota IN ('aderente', 'aderente_com_ruido', 'baixa_aderencia')),
  evidencia_origem    text CHECK (evidencia_origem IN ('observado', 'estimado_inicio_cadeia',
                        'iniciou_apos_origem', 'interpolado_lacuna', 'estimado_terminal')),
  evidencia_destino   text CHECK (evidencia_destino IN ('observado', 'terminou_antes_destino',
                        'interpolado_lacuna', 'estimado_fim_cadeia', 'estimado_terminal')),
  alertas             text[] NOT NULL DEFAULT '{}',  -- ex.: {sem_partida_confirmada}
  gtfs_versao_id      integer NOT NULL REFERENCES referencia.gtfs_versoes (id),
  execucao_id         uuid  NOT NULL REFERENCES operacao.execucoes (id),
  CHECK (fim_obs >= inicio_obs),
  CHECK (chegada_em IS NULL OR partida_em IS NULL OR chegada_em >= partida_em)
);
CREATE INDEX viagens_linha_dia_idx   ON operacao.viagens (codigo_linha, data_servico);
CREATE INDEX viagens_prefixo_dia_idx ON operacao.viagens (prefixo, data_servico);
CREATE INDEX viagens_execucao_idx    ON operacao.viagens (execucao_id);  -- apagar uma execução refeita

-- ATENÇÃO ao volume: nos dados de teste são ~168 mil passagens por linha-mês;
-- no sistema inteiro passaria de 200 milhões por mês. Decidir particionamento
-- ou retenção curta antes de ligar para todas as linhas.
CREATE TABLE operacao.passagens (
  viagem_id   uuid     NOT NULL REFERENCES operacao.viagens (id) ON DELETE CASCADE,
  sequencia   smallint NOT NULL,
  stop_id     text     NOT NULL,
  passou_em   timestamptz NOT NULL,
  metodo      text     NOT NULL CHECK (metodo IN ('interpolado', 'interpolado_sobre_lacuna')),
  PRIMARY KEY (viagem_id, sequencia)
);

CREATE TABLE operacao.lacunas_veiculo (
  id                uuid DEFAULT public.uuid_v7() PRIMARY KEY,
  codigo_linha      text    NOT NULL REFERENCES referencia.linhas (codigo_linha),
  prefixo           integer NOT NULL,
  data_servico      date    NOT NULL,
  inicio_em         timestamptz NOT NULL,
  fim_em            timestamptz,                    -- NULL = não voltou a transmitir
  km_inicio         double precision,
  km_fim            double precision,
  viagens_cabiveis  double precision CHECK (viagens_cabiveis >= 0),
  tipo              text NOT NULL CHECK (tipo IN ('parou_de_transmitir', 'deslocou_sem_sinal',
                      'parado_sem_sinal', 'cabe_viagem_sem_sinal')),
  execucao_id       uuid NOT NULL REFERENCES operacao.execucoes (id),
  CHECK (fim_em IS NULL OR fim_em >= inicio_em)
);
CREATE INDEX lacunas_veiculo_linha_dia_idx ON operacao.lacunas_veiculo (codigo_linha, data_servico);
CREATE INDEX lacunas_veiculo_execucao_idx  ON operacao.lacunas_veiculo (execucao_id);

-- Mapa ao vivo (US22): última posição de cada veículo nos últimos 5 minutos.
-- A API lê esta view; ela não precisa de acesso direto ao bruto (a view lê com
-- os direitos do dono). O limite no futuro tira GPS com relógio adiantado.
CREATE VIEW operacao.posicoes_atuais AS
SELECT DISTINCT ON (p.prefixo)
       p.prefixo,
       l.letreiro                     AS codigo_linha,
       p.sentido_api,
       p.acessivel,
       p.horario_gps,
       ST_SetSRID(ST_MakePoint(p.longitude, p.latitude), 4326) AS geom
  FROM bruto.posicoes_gps p
  LEFT JOIN referencia.linhas_olhovivo l USING (cod_linha_sentido)
 WHERE p.horario_gps >  now() - interval '5 minutes'
   AND p.horario_gps <= now() + interval '2 minutes'
 ORDER BY p.prefixo, p.horario_gps DESC;

-- Lacunas de coleta (US13): intervalos sem ciclo ok acima de 90 s.
CREATE VIEW operacao.lacunas_coleta AS
SELECT fonte, anterior AS inicio_lacuna, iniciado_em AS fim_lacuna, iniciado_em - anterior AS duracao
  FROM (SELECT fonte, iniciado_em,
               lag(iniciado_em) OVER (PARTITION BY fonte ORDER BY iniciado_em) AS anterior
          FROM bruto.ciclos_coleta
         WHERE status = 'ok' AND fonte = 'olhovivo_posicao') s
 WHERE iniciado_em - anterior > interval '90 seconds';

-- Saúde da coleta (US30): último sucesso e último erro por fonte.
CREATE VIEW operacao.saude_coleta AS
SELECT fonte,
       max(concluido_em) FILTER (WHERE status = 'ok')  AS ultimo_ok,
       max(concluido_em) FILTER (WHERE status <> 'ok') AS ultimo_erro,
       count(*) FILTER (WHERE status <> 'ok' AND concluido_em > now() - interval '1 hour') AS erros_ultima_hora
  FROM bruto.ciclos_coleta
 GROUP BY fonte;

-- Saúde das partições: alertar quando ok = false. Dado na default quer dizer
-- GPS com horário fora das partições criadas; cobertura curta quer dizer que a
-- manutenção do pg_partman não está rodando.
CREATE VIEW operacao.saude_particoes AS
SELECT d.linhas_na_default, p.particoes, p.cobertura_ate,
       d.linhas_na_default = 0 AND p.cobertura_ate > now() + interval '1 month' AS ok
  FROM (SELECT count(*) AS linhas_na_default FROM bruto.posicoes_gps_default) d,
       (SELECT count(*) AS particoes,
               max(substring(pg_get_expr(c.relpartbound, c.oid) FROM 'TO \(''([^'']+)''\)')::timestamptz) AS cobertura_ate
          FROM pg_inherits i JOIN pg_class c ON c.oid = i.inhrelid
         WHERE i.inhparent = 'bruto.posicoes_gps'::regclass
           AND c.relname <> 'posicoes_gps_default') p;

-- == indicadores ==============================================================
-- Uma linha por saída programada auditada. Junta auditoria_saidas e
-- diagnostico_saidas do R (eram 1:1 pela mesma chave). codigo_linha e
-- data_servico se repetem para filtrar sem JOIN; a FK composta garante que
-- batem com a saída.
CREATE TABLE indicadores.saidas_auditadas (
  saida_id            uuid    PRIMARY KEY,
  codigo_linha        text    NOT NULL,
  data_servico        date    NOT NULL,
  viagem_id           uuid    REFERENCES operacao.viagens (id) ON DELETE SET NULL,
  prefixo_observado   integer,
  partida_observada   timestamptz,
  -- double, não real: em float4 uma diferença de 3,00000001 vira 3,0 e muda o
  -- status (atraso -> no horário) em relação ao que o worker calculou.
  diferenca_min       double precision,
  status              text NOT NULL CHECK (status IN (
                        'realizada_no_horario', 'realizada_com_atraso', 'realizada_antecipadamente',
                        'viagem_quase_completa', 'partida_sem_viagem_completa',
                        'possivelmente_nao_realizada', 'inconclusiva',
                        'fora_do_periodo_coletado', 'programacao_invalida')),
  nivel_confianca     text CHECK (nivel_confianca IN ('alta', 'media', 'baixa', 'muito_baixa')),
  cobertura_dados     text CHECK (cobertura_dados IN ('boa', 'parcial', 'insuficiente', 'lacuna_gps_veiculo')),
  frac_min_dados      double precision CHECK (frac_min_dados BETWEEN 0 AND 1),
  pct_tracado         double precision CHECK (pct_tracado BETWEEN 0 AND 100),
  pct_cobertura       double precision CHECK (pct_cobertura BETWEEN 0 AND 100),
  evidencia           text,
  so_lacuna_aberta    boolean,
  diagnostico_frota   text CHECK (diagnostico_frota IN (
                        'sem_dados_da_linha', 'nenhum_veiculo_sem_gps_poderia_fazer',
                        'possivel_viagem_sem_gps_no_horario', 'possivel_viagem_sem_gps_com_atraso',
                        'viagem_interrompida_sem_lacuna', 'viagem_quase_completa',
                        'conclusao_compativel_com_lacuna_gps', 'lacuna_gps_curta_demais_para_concluir')),
  detalhe_frota       text,
  veiculos_possiveis  integer[] NOT NULL DEFAULT '{}',   -- era "21013,21035" em texto
  tolerancia_min           double precision NOT NULL CHECK (tolerancia_min > 0),   -- IPP (a) e (b): 3 min
  tolerancia_ampliada_min  double precision NOT NULL DEFAULT 4,                    -- IPP (c)
  execucao_id         uuid  NOT NULL REFERENCES operacao.execucoes (id),
  FOREIGN KEY (saida_id, codigo_linha, data_servico)
    REFERENCES operacao.saidas_programadas (id, codigo_linha, data_servico) ON DELETE CASCADE,
  CHECK (tolerancia_ampliada_min >= tolerancia_min),
  -- o status de uma saída realizada precisa bater com a diferença (regra ipp-0.2.0)
  CHECK (status <> 'realizada_no_horario'      OR abs(diferenca_min) <= tolerancia_min),
  CHECK (status <> 'realizada_com_atraso'      OR diferenca_min >  tolerancia_min),
  CHECK (status <> 'realizada_antecipadamente' OR diferenca_min < -tolerancia_min),
  -- uma saída realizada sempre tem a diferença calculada: sem isto, NULL passaria nos CHECKs acima
  CHECK (status NOT IN ('realizada_no_horario', 'realizada_com_atraso', 'realizada_antecipadamente')
         OR diferenca_min IS NOT NULL),
  CHECK (status NOT IN ('possivelmente_nao_realizada', 'fora_do_periodo_coletado', 'programacao_invalida')
         OR viagem_id IS NULL)
);
CREATE INDEX saidas_auditadas_linha_dia_idx ON indicadores.saidas_auditadas (codigo_linha, data_servico);
-- ranking de todas as linhas num mês (a expressão é a mesma das views)
CREATE INDEX saidas_auditadas_mes_idx ON indicadores.saidas_auditadas
  ((date_trunc('month', data_servico::timestamp)::date));
CREATE INDEX saidas_auditadas_execucao_idx ON indicadores.saidas_auditadas (execucao_id);

-- Era o texto "partida espúria descartada: 82409 às 09:36:58 percorreu 7% (1.2 km)".
CREATE TABLE indicadores.partidas_descartadas (
  saida_id     uuid    NOT NULL REFERENCES indicadores.saidas_auditadas (saida_id) ON DELETE CASCADE,
  prefixo      integer NOT NULL,
  partida_em   timestamptz NOT NULL,
  frac_tracado double precision    NOT NULL CHECK (frac_tracado BETWEEN 0 AND 1),
  km           double precision    NOT NULL CHECK (km >= 0),
  PRIMARY KEY (saida_id, prefixo, partida_em)
);

-- Era a coluna lacunas_ids em texto.
CREATE TABLE indicadores.saidas_lacunas (
  saida_id   uuid NOT NULL REFERENCES indicadores.saidas_auditadas (saida_id) ON DELETE CASCADE,
  lacuna_id  uuid NOT NULL REFERENCES operacao.lacunas_veiculo (id) ON DELETE CASCADE,
  PRIMARY KEY (saida_id, lacuna_id)
);
CREATE INDEX saidas_lacunas_lacuna_idx ON indicadores.saidas_lacunas (lacuna_id);

-- ICV (IQT.GSO.001.05): contagens por faixa horária. hora é a hora do RELÓGIO
-- local da partida programada (como no R/13_icv.R); 0–2h são o fim do dia de
-- serviço. O período sai da hora, então não tem como discordar dela.
CREATE TABLE indicadores.icv_faixas (
  codigo_linha     text     NOT NULL REFERENCES referencia.linhas (codigo_linha),
  data_servico     date     NOT NULL,
  sentido          smallint NOT NULL CHECK (sentido IN (0, 1)),   -- circular = 0
  hora             smallint NOT NULL CHECK (hora BETWEEN 0 AND 23),
  tipo_dia         text     NOT NULL CHECK (tipo_dia IN ('U', 'S', 'D')),
  periodo          text GENERATED ALWAYS AS (CASE
                     WHEN hora BETWEEN 3  AND 5  THEN 'APM'
                     WHEN hora BETWEEN 6  AND 8  THEN 'PM'
                     WHEN hora BETWEEN 9  AND 16 THEN 'EP'
                     WHEN hora BETWEEN 17 AND 19 THEN 'PT'
                     WHEN hora BETWEEN 20 AND 23 THEN 'PN'
                     ELSE 'PPN' END) STORED,
  programadas      smallint NOT NULL CHECK (programadas > 0),     -- só saídas avaliáveis
  realizadas       smallint NOT NULL CHECK (realizadas >= 0),     -- pode passar do programado
  quase_completas  smallint NOT NULL DEFAULT 0 CHECK (quase_completas >= 0),
  inconclusivas    smallint NOT NULL DEFAULT 0 CHECK (inconclusivas BETWEEN 0 AND programadas),
  execucao_id      uuid     NOT NULL REFERENCES operacao.execucoes (id),
  PRIMARY KEY (codigo_linha, data_servico, sentido, hora)
);
CREATE INDEX icv_faixas_mes_idx ON indicadores.icv_faixas
  ((date_trunc('month', data_servico::timestamp)::date));
CREATE INDEX icv_faixas_execucao_idx ON indicadores.icv_faixas (execucao_id);

-- Percentual com 2 casas; NULL quando não há denominador.
CREATE FUNCTION indicadores.pct(parte numeric, total numeric) RETURNS numeric
LANGUAGE sql IMMUTABLE PARALLEL SAFE AS $$ SELECT round(100 * parte / NULLIF(total, 0), 2) $$;

-- Base das views de IPP: cada saída com as marcações da regra ipp-0.2.0
-- (R/07_auditoria.R, indicadores_ipp):
--   (a) ipp:   realizada_no_horario / válidas
--   (b) ipp_b: saiu (completa ou não) com |dif| <= tolerancia_min, + inconclusivas
--   (c) ipp_c: como (b), com tolerancia_ampliada_min
CREATE VIEW indicadores.saidas_classificadas AS
SELECT a.saida_id, a.codigo_linha, a.data_servico,
       date_trunc('month', a.data_servico::timestamp)::date                        AS mes,
       s.sentido, s.tipo_dia,
       extract(hour FROM s.partida_prevista AT TIME ZONE 'America/Sao_Paulo')::smallint AS hora_prevista,
       a.status,
       a.status NOT IN ('programacao_invalida', 'fora_do_periodo_coletado')      AS valida,
       a.status = 'realizada_no_horario'                                          AS no_horario,
       a.status = 'realizada_com_atraso'                                          AS com_atraso,
       a.status = 'realizada_antecipadamente'                                     AS antecipada,
       a.status = 'viagem_quase_completa'                                         AS quase_completa,
       a.status = 'partida_sem_viagem_completa'                                   AS sem_viagem_completa,
       a.status = 'possivelmente_nao_realizada'                                   AS nao_realizada,
       a.status = 'inconclusiva'                                                  AS inconclusiva,
       coalesce(a.status IN ('realizada_no_horario', 'realizada_com_atraso', 'realizada_antecipadamente',
                             'partida_sem_viagem_completa', 'viagem_quase_completa')
                AND abs(a.diferenca_min) <= a.tolerancia_min, false)
         OR a.status = 'inconclusiva'                                             AS pontual_b,
       coalesce(a.status IN ('realizada_no_horario', 'realizada_com_atraso', 'realizada_antecipadamente',
                             'partida_sem_viagem_completa', 'viagem_quase_completa')
                AND abs(a.diferenca_min) <= a.tolerancia_ampliada_min, false)
         OR a.status = 'inconclusiva'                                             AS pontual_c
  FROM indicadores.saidas_auditadas a
  JOIN operacao.saidas_programadas s ON s.id = a.saida_id;

-- As cinco views de IPP têm as mesmas colunas que os CSVs do R; só muda o
-- agrupamento. Filtre sempre por linha e/ou período.
CREATE VIEW indicadores.ipp_diario_sentido AS            -- ipp_diario_por_sentido.csv
SELECT codigo_linha, data_servico, tipo_dia, sentido,
       count(*) AS programadas_total, count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario, count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas, count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b, count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario), count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),  count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),  count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso OR antecipada), count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada), count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),  count(*) FILTER (WHERE valida)) AS inconclusivas_pct
  FROM indicadores.saidas_classificadas
 GROUP BY codigo_linha, data_servico, tipo_dia, sentido;

CREATE VIEW indicadores.ipp_diario AS                    -- ipp_diario.csv
SELECT codigo_linha, data_servico, tipo_dia,
       count(*) AS programadas_total, count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario, count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas, count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b, count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario), count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),  count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),  count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso OR antecipada), count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada), count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),  count(*) FILTER (WHERE valida)) AS inconclusivas_pct
  FROM indicadores.saidas_classificadas
 GROUP BY codigo_linha, data_servico, tipo_dia;

CREATE VIEW indicadores.ipp_mensal_sentido AS            -- ipp_mensal_por_sentido.csv
SELECT codigo_linha, mes, sentido,
       count(*) AS programadas_total, count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario, count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas, count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b, count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario), count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),  count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),  count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso OR antecipada), count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada), count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),  count(*) FILTER (WHERE valida)) AS inconclusivas_pct
  FROM indicadores.saidas_classificadas
 GROUP BY codigo_linha, mes, sentido;

CREATE VIEW indicadores.ipp_mensal AS                    -- ranking_linhas.csv (sem as viagens)
SELECT codigo_linha, mes,
       count(*) AS programadas_total, count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario, count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas, count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b, count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario), count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),  count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),  count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso OR antecipada), count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada), count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),  count(*) FILTER (WHERE valida)) AS inconclusivas_pct
  FROM indicadores.saidas_classificadas
 GROUP BY codigo_linha, mes;

CREATE VIEW indicadores.ipp_por_hora AS                  -- ranking_linhas_horario.csv (por mês)
SELECT codigo_linha, mes, tipo_dia, hora_prevista,
       count(*) AS programadas_total, count(*) FILTER (WHERE valida) AS programadas_validas,
       count(*) FILTER (WHERE no_horario) AS no_horario, count(*) FILTER (WHERE com_atraso) AS com_atraso,
       count(*) FILTER (WHERE antecipada) AS antecipadas, count(*) FILTER (WHERE quase_completa) AS quase_completas,
       count(*) FILTER (WHERE sem_viagem_completa) AS partida_sem_viagem_completa,
       count(*) FILTER (WHERE nao_realizada) AS possivelmente_nao_realizadas,
       count(*) FILTER (WHERE inconclusiva) AS inconclusivas,
       count(*) FILTER (WHERE pontual_b) AS pontuais_b, count(*) FILTER (WHERE pontual_c) AS pontuais_c,
       indicadores.pct(count(*) FILTER (WHERE no_horario), count(*) FILTER (WHERE valida)) AS ipp_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_b),  count(*) FILTER (WHERE valida)) AS ipp_b_pct,
       indicadores.pct(count(*) FILTER (WHERE pontual_c),  count(*) FILTER (WHERE valida)) AS ipp_c_pct,
       indicadores.pct(count(*) FILTER (WHERE no_horario OR com_atraso OR antecipada), count(*) FILTER (WHERE valida)) AS realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE nao_realizada), count(*) FILTER (WHERE valida)) AS nao_realizadas_pct,
       indicadores.pct(count(*) FILTER (WHERE inconclusiva),  count(*) FILTER (WHERE valida)) AS inconclusivas_pct
  FROM indicadores.saidas_classificadas
 GROUP BY codigo_linha, mes, tipo_dia, hora_prevista;

-- ICV (R/13_icv.R): realizadas limitadas ao programado em cada faixa;
-- período = soma das limitadas / soma das programadas; dia = média dos
-- períodos; mês do sentido = média dos dias; linha = média dos sentidos.
CREATE VIEW indicadores.icv_periodo_dia AS
SELECT codigo_linha, data_servico, tipo_dia, sentido, periodo,
       sum(programadas)                                                      AS programadas,
       sum(LEAST(realizadas, programadas))                                   AS realizadas_lim,
       sum(LEAST(realizadas + quase_completas + inconclusivas, programadas)) AS ampliadas_lim,
       sum(LEAST(realizadas, programadas))::numeric / sum(programadas)       AS icv,
       sum(LEAST(realizadas + quase_completas + inconclusivas, programadas))::numeric
         / sum(programadas)                                                  AS icv_ampliado
  FROM indicadores.icv_faixas
 GROUP BY codigo_linha, data_servico, tipo_dia, sentido, periodo;

CREATE VIEW indicadores.icv_diario_sentido AS
SELECT codigo_linha, data_servico, tipo_dia, sentido,
       avg(icv) AS icv, avg(icv_ampliado) AS icv_ampliado,
       sum(programadas) AS programadas, sum(realizadas_lim) AS realizadas_lim, sum(ampliadas_lim) AS ampliadas_lim
  FROM indicadores.icv_periodo_dia
 GROUP BY codigo_linha, data_servico, tipo_dia, sentido;

CREATE VIEW indicadores.icv_mensal_sentido AS
SELECT codigo_linha, date_trunc('month', data_servico::timestamp)::date AS mes, sentido,
       avg(icv) AS icv, avg(icv_ampliado) AS icv_ampliado, count(*) AS dias,
       sum(programadas) AS programadas, sum(realizadas_lim) AS realizadas_lim
  FROM indicadores.icv_diario_sentido
 GROUP BY 1, 2, 3;

CREATE VIEW indicadores.icv_mensal AS                    -- icv_mensal.csv
SELECT codigo_linha, mes,
       round(100 * avg(icv), 2)          AS icv_pct,
       round(100 * avg(icv_ampliado), 2) AS icv_ampliado_pct,
       sum(programadas)                  AS programadas,
       sum(realizadas_lim)               AS realizadas_lim
  FROM indicadores.icv_mensal_sentido
 GROUP BY codigo_linha, mes;

-- == T5: papéis e permissões ==================================================
-- Papéis de grupo (NOLOGIN), um por serviço, cada um com o mínimo:
--   onibus_coletor  grava o bruto (só INSERT) e mantém catálogos e programação;
--   onibus_worker   lê bruto e referência, escreve operacao e indicadores;
--   onibus_leitura  a API: só SELECT, e nada do bruto (o mapa usa a view).
-- As migrações, a manutenção das partições e o arquivo rodam com o dono do
-- banco. Os usuários de login são criados por ambiente (modelo no fim).
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'onibus_coletor') THEN CREATE ROLE onibus_coletor NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'onibus_worker')  THEN CREATE ROLE onibus_worker  NOLOGIN; END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'onibus_leitura') THEN CREATE ROLE onibus_leitura NOLOGIN; END IF;
END $$;

-- Ninguém cria objetos no public nem conecta sem ter recebido o direito.
-- (O banco deve ser só deste sistema: isto tira o CONNECT de quem não for um
-- dos papéis acima.)
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
DO $$ BEGIN
  EXECUTE format('REVOKE ALL ON DATABASE %I FROM PUBLIC', current_database());
  EXECUTE format('GRANT CONNECT, TEMPORARY ON DATABASE %I TO onibus_coletor, onibus_worker', current_database());
  EXECUTE format('GRANT CONNECT ON DATABASE %I TO onibus_leitura', current_database());
END $$;

GRANT USAGE ON SCHEMA bruto, referencia                        TO onibus_coletor;
GRANT USAGE ON SCHEMA bruto, referencia, operacao, indicadores TO onibus_worker;
GRANT USAGE ON SCHEMA referencia, operacao, indicadores        TO onibus_leitura;

GRANT SELECT, INSERT         ON ALL TABLES IN SCHEMA bruto      TO onibus_coletor;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA referencia TO onibus_coletor;
GRANT USAGE ON ALL SEQUENCES IN SCHEMA referencia               TO onibus_coletor;

GRANT SELECT ON ALL TABLES IN SCHEMA bruto, referencia TO onibus_worker;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA operacao, indicadores TO onibus_worker;

GRANT SELECT ON ALL TABLES IN SCHEMA referencia, operacao, indicadores TO onibus_leitura;

-- Tabelas criadas depois PELO MESMO USUÁRIO que roda este arquivo herdam os
-- mesmos direitos (rode as migrações sempre com ele).
ALTER DEFAULT PRIVILEGES IN SCHEMA bruto GRANT SELECT, INSERT ON TABLES TO onibus_coletor;
ALTER DEFAULT PRIVILEGES IN SCHEMA bruto GRANT SELECT ON TABLES TO onibus_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA referencia GRANT SELECT, INSERT, UPDATE ON TABLES TO onibus_coletor;
ALTER DEFAULT PRIVILEGES IN SCHEMA referencia GRANT USAGE ON SEQUENCES TO onibus_coletor;
ALTER DEFAULT PRIVILEGES IN SCHEMA referencia GRANT SELECT ON TABLES TO onibus_worker, onibus_leitura;
ALTER DEFAULT PRIVILEGES IN SCHEMA operacao, indicadores
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onibus_worker;
ALTER DEFAULT PRIVILEGES IN SCHEMA operacao, indicadores GRANT SELECT ON TABLES TO onibus_leitura;

COMMIT;

-- =============================================================================
-- Depois de rodar (fora deste arquivo, por ambiente):
--
-- 1. Usuários de login, um por serviço, com senha fora do repositório:
--      CREATE ROLE coletor_app LOGIN PASSWORD '...' IN ROLE onibus_coletor;
--      CREATE ROLE worker_app  LOGIN PASSWORD '...' IN ROLE onibus_worker;
--      CREATE ROLE api_app     LOGIN PASSWORD '...' IN ROLE onibus_leitura;
--      -- configurações valem para o LOGIN, não para o grupo:
--      ALTER ROLE api_app SET statement_timeout = '10s';
--      ALTER ROLE api_app SET default_transaction_read_only = on;
--    Em produção, exija SSL (no RDS: parâmetro rds.force_ssl = 1).
--
-- 2. Manutenção do pg_partman (cria os meses seguintes e desanexa os antigos),
--    de hora em hora, com o dono do banco. Sem ela, a partir do 4º mês os
--    dados caem na partição default. Uma das opções:
--      a) background worker: postgres -c shared_preload_libraries=pg_partman_bgw
--         -c pg_partman_bgw.dbname=onibus -c pg_partman_bgw.role=<dono>
--         -c pg_partman_bgw.interval=3600
--      b) RDS: extensão pg_cron e
--         SELECT cron.schedule('partman', '0 * * * *', 'CALL partman.run_maintenance_proc()');
--      c) cron da máquina: psql -c 'CALL partman.run_maintenance_proc()'
--
-- 3. Monitorar: SELECT * FROM operacao.saude_particoes;  (ok deve ser true)
-- =============================================================================
