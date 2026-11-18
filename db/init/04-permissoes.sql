-- Permissões dos papéis da aplicação em cada camada.
--
-- Ainda não há tabelas: elas chegam com as migrações (T6, T7). Por isso usamos
-- ALTER DEFAULT PRIVILEGES: toda tabela que o administrador (quem roda este
-- script e as migrações) criar nesses schemas já nasce com estas permissões.
-- Ninguém recebe UPDATE, DELETE ou TRUNCATE em bruto.

-- Os dois papéis podem acessar os objetos dos quatro schemas. Isso não dá
-- acesso a nenhuma tabela, só permite chegar até elas.
GRANT USAGE ON SCHEMA bruto, referencia, operacao, indicadores
    TO onibus_escrita, onibus_leitura;

-- API: só lê, em todas as camadas.
ALTER DEFAULT PRIVILEGES IN SCHEMA bruto, referencia, operacao, indicadores
    GRANT SELECT ON TABLES TO onibus_leitura;

-- Coletor e worker em bruto: leem e inserem. O bruto é imutável.
ALTER DEFAULT PRIVILEGES IN SCHEMA bruto
    GRANT SELECT, INSERT ON TABLES TO onibus_escrita;

-- Coletor e worker nas camadas tratadas: leem e gravam.
ALTER DEFAULT PRIVILEGES IN SCHEMA referencia, operacao, indicadores
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO onibus_escrita;

-- Colunas serial usam uma sequência: sem USAGE nela, o INSERT falha.
ALTER DEFAULT PRIVILEGES IN SCHEMA bruto, referencia, operacao, indicadores
    GRANT USAGE ON SEQUENCES TO onibus_escrita;
