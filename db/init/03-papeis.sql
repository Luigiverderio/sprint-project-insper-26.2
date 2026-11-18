\getenv senha_escrita DB_WRITER_PASSWORD
\getenv senha_leitura DB_READER_PASSWORD


CREATE ROLE onibus_escrita LOGIN PASSWORD :'senha_escrita';
COMMENT ON ROLE onibus_escrita IS 'Coletor e worker: grava dados (em bruto, só INSERT).';


CREATE ROLE onibus_leitura LOGIN PASSWORD :'senha_leitura';
COMMENT ON ROLE onibus_leitura IS 'API: somente leitura.';

