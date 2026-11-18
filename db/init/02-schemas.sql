CREATE SCHEMA bruto;
COMMENT ON SCHEMA bruto IS
    'Posições e respostas da API Olho Vivo como chegaram. Imutável: só recebe INSERT.';

CREATE SCHEMA referencia;
COMMENT ON SCHEMA referencia IS
    'GTFS versionado e programação das linhas.';

CREATE SCHEMA operacao;
COMMENT ON SCHEMA operacao IS
    'Viagens reconstruídas e passagens por parada, geradas pelo worker.';

CREATE SCHEMA indicadores;
COMMENT ON SCHEMA indicadores IS
    'Indicadores calculados pelo worker e lidos pela API.';

