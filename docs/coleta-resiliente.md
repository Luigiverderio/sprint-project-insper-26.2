# Coleta resiliente da Olho Vivo (além do retry)

- **Status:** Rascunho, para o time ler e aprovar (card T15)
- **Data:** 2026-11-21
- **Base:** T12 (cliente com retry e medição do ciclo), ADR-002, T8 (`bruto.ciclos_coleta`) e o plano de deploy do T13 (`docs/deploy-aws.md`)

A Olho Vivo só devolve a posição do momento: o que não foi coletado não dá para
buscar depois. Repetir a chamada resolve falhas curtas, mas não resolve queda
longa, resposta vazia nem falha do nosso lado. Este estudo diz o que o coletor
faz hoje em cada falha, o que se perde e o que vale melhorar, em ordem de valor.

## 1. O que já existe (develop, fa4cf10)

| Proteção | Onde | Como |
|---|---|---|
| Tempo limite por chamada | `olho_vivo.py` | 10 s |
| Retry dentro do ciclo | `coletor.py` | Até 5 tentativas, esperas de 1, 2, 4 e 8 s, só para erro de rede, 5xx, resposta inválida e sessão expirada |
| Prazo do ciclo | `coletor.py` | 25 s; ao estourar, a requisição é cancelada e o ciclo termina como falha |
| Reautenticação | `olho_vivo.py` | Um 401 conta como tentativa, e a seguinte faz login de novo |
| Sem ciclos sobrepostos | `scheduler.py` | `max_instances=1`; disparo pulado vai para o log |
| Toda falha registrada | `gravacao.py` | O ciclo com falha entra em `bruto.ciclos_coleta` com `status` e `erro` |
| Alerta de falha longa | `coletor.py` | `logger.critical` depois de 10 min seguidos de falha |
| Lacunas | `operacao.lacunas_coleta` | Intervalos de mais de 90 s entre ciclos `ok` |
| Duplicatas | `bruto.posicoes_gps` | Chave `(prefixo, horario_gps)` com `ON CONFLICT DO NOTHING` |

## 2. Os números do T12 (coleta real de 1 h, 20/11, 14:16 a 15:19)

- 122 ciclos: 117 `ok` e 5 `erro_rede` (2 DNS, 2 `ConnectTimeout`, 1 estouro do prazo de 25 s).
- Duração do ciclo: mediana de 0,57 s, p95 de 5,61 s, máxima de 25,00 s (o prazo
  segurou). Nos ciclos `ok`, a máxima foi 16,1 s.
- Média das etapas: login 0,20 s, chamada 0,74 s, leitura 0,02 s, gravação 0,06 s.
- Nenhum ciclo sobreposto. Uma lacuna de 4,5 min (14:33:45 a 14:38:15), com um
  "Collection tick missed" no log, provavelmente por pausa da máquina ou da rede.

**Conclusão:** o retry já cobre bem as falhas curtas. Das 5 falhas, 4 foram
de rede e se resolveram no ciclo seguinte. A lacuna maior não veio da API,
veio da máquina, e o coletor não tinha como evitá-la.

## 3. Quanto custa perder um ciclo

Pela ADR-002, cada veículo gera de 1.100 a 1.250 posições por dia, ou seja, uma
posição nova a cada ~70 s. O ciclo é de 30 s. Então:

- **Perder um ciclo isolado custa pouco.** O ciclo seguinte traz a última
  posição de cada veículo, e só se perde a posição de quem atualizou duas
  vezes dentro do buraco (algo raro com ~70 s entre atualizações).
- **Perder vários ciclos seguidos custa tudo.** A partir de ~1 min de buraco, cada
  minuto a mais é quase um minuto inteiro de trajetória perdido para a frota toda.

Por isso o valor está em **detectar e encurtar falhas longas** e em **não perder o
que já foi coletado**, e não em insistir mais dentro de um ciclo.

## 4. Cenários de falha

| Cenário | O que acontece hoje | Como detectamos | Perda |
|---|---|---|---|
| Rede ou DNS falha por segundos | Retry dentro do ciclo; se não der, `erro_rede` | Linha de falha em `ciclos_coleta` | Quase nenhuma (seção 3) |
| API fora por minutos | Cada ciclo gasta 5 tentativas e grava `erro_rede` | Falhas em `ciclos_coleta`, lacuna na view, `critical` no log após 10 min | Tudo no período, mas visível |
| API lenta ou travada | O prazo de 25 s corta o ciclo, gravado como `erro_rede` | Igual ao anterior | O ciclo (ou vários) |
| Erro 5xx | Retry; se persistir, `erro_http` | Linha de falha | Igual à API fora |
| Limite de uso (429) ou outro 4xx | **Sem retry**: o ciclo termina na hora como `erro_http` | Linha de falha | O ciclo; se persistir, tudo |
| Sessão expirada (401) | Reautentica na tentativa seguinte | Linha `ok` (tentativas > 1) | Nenhuma |
| Token recusado | `erro_autenticacao` em todo ciclo; tentar de novo não resolve | Linha de falha e `critical` após 10 min | Tudo até alguém trocar o token |
| **Resposta vazia** (0 veículos) | **Gravada como `ok`** | **Nada**: a view de lacunas só olha ciclos `ok` | Tudo, e invisível |
| **Dado antigo** (mesmo `hr`, mesmos `ta`) | Gravado como `ok`; as posições repetidas são descartadas pela chave | `n_inseridos = 0` no ciclo, mas ninguém olha | Tudo, e invisível |
| **Banco fora** | A coleta dá certo, a gravação falha, o erro vai só para o log | **Nada no banco**: o ciclo não deixa linha | Tudo, só aparece no log |
| Banco lento | A gravação (até 3 s de conexão + 5 s de comando) roda depois dos 25 s do ciclo; pode passar de 30 s e o disparo seguinte é pulado | Só no log ("skipping this tick") | O ciclo pulado |
| Coletor cai ou a máquina reinicia | Hoje nada o traz de volta (ele não está no `docker-compose`) | Nenhum alerta externo | Tudo até alguém perceber |
| Máquina pausa (notebook dorme) | O agendador perde os disparos e registra "tick missed" só no log | Lacuna na view, sem o motivo | O período da pausa |

## 5. Teste com falhas simuladas

`tests/test_coleta_resiliente.py` roda 4 ciclos com o coletor de verdade contra
uma Olho Vivo simulada (2 veículos por ciclo) e conta o que chegaria ao banco.
Não precisa de Docker: o "banco fora" é uma porta fechada em `127.0.0.1`.
Rodado em 2026-11-21, no Windows, com `uv run pytest tests/test_coleta_resiliente.py`: 5 testes passaram.

| Cenário | Status gravado | Posições recebidas | Posições gravadas | Ciclos sem linha | Visível no banco? | Tempo do teste |
|---|---|---|---|---|---|---|
| Normal | `ok` × 4 | 8 | 8 | 0 | n/a | < 0,01 s |
| API fora | `erro_rede` × 4 (5 tentativas cada) | 0 | 0 | 0 | Sim | 0,01 s |
| API lenta (prazo de 0,3 s) | `erro_rede` × 4, com "exceeded" | 0 | 0 | 0 | Sim | 1,25 s |
| Resposta vazia | `ok` × 4 | 0 | 0 | 0 | **Não** | < 0,01 s |
| Banco fora | nenhuma | 8 | 0 | **4** | **Não** (só log) | 8,3 s |

No cenário do banco fora, cada gravação levou ~2 s para falhar (conexão recusada
no Windows), dentro do limite de 3 s de conexão. Com um banco que não responde
em vez de recusar, cada gravação pode levar até 8 s.

Os testes descrevem o comportamento de hoje, inclusive as perdas. Quando uma
melhoria da seção 7 entrar, o teste do cenário dela muda junto e passa a mostrar
o que deixou de se perder.

## 6. Técnicas avaliadas

**Escolhidas**

- **Buffer em disco quando o banco falha.** Guardar o JSON de cada ciclo
  (`.json.gz`) num diretório local antes de gravar, preencher a coluna
  `ciclos_coleta.arquivo_bruto`, que já existe e nunca é preenchida, e
  reenviar os arquivos pendentes quando o banco voltar. Os mesmos arquivos
  podem ir para o S3 depois, como arquivo do dado original. Resolve a pior
  falha da tabela (perda total e invisível) com pouco código, porque a chave da
  `posicoes_gps` já torna o reenvio seguro.
- **Validar a resposta e marcar o ciclo como suspeito.** Comparar com os
  ciclos anteriores: 0 veículos, queda brusca no número de veículos ou o mesmo
  `hr` de antes. Gravar as posições normalmente, mas com um `status` novo
  (`suspeito`), para a view de lacunas e o `/health` enxergarem. Exige migração
  nova, porque o `CHECK` de `status` só aceita os valores atuais.
- **Registrar o ciclo pulado ou perdido (H13).** Quando o agendador pula ou
  perde um disparo, gravar uma linha em `ciclos_coleta` com um `status` próprio
  (`pulado`) e o motivo, em vez de só logar. Assim a lacuna mostra por que
  aconteceu.
- **Gravação dentro do orçamento de 30 s.** Hoje são 25 s de coleta mais até 8 s
  de gravação. Ou o prazo da coleta desce para ~20 s, ou a gravação passa a usar
  só o tempo que sobrou do ciclo, mandando o resto para o buffer.
- **Reinício automático e sinal de vida externo.** O coletor no
  `docker-compose.prod.yml` com `restart: unless-stopped` e o alarme "minutos
  desde o último ciclo ok" no CloudWatch. **Já estão no plano do T13** (seções
  1 e 7 do `docs/deploy-aws.md`); o T15 só confirma a prioridade.
- **Espera com variação aleatória (*jitter*).** Barato (`wait_random_exponential`
  no lugar de `wait_exponential`), mas de valor baixo com um coletor só. Entra
  junto com outra mudança no coletor, não como card próprio.

**Descartadas por ora**

- **Mais tentativas por ciclo ou prazo maior.** Pela seção 3, insistir num ciclo
  vale pouco: o ciclo seguinte, 30 s depois, traz quase a mesma informação.
- **Disjuntor (*circuit breaker*).** Numa queda longa, o coletor faz no máximo
  ~10 chamadas por minuto, uma carga que não "bombardeia" a API. Não há limite
  de uso documentado nem 429 nas medições. Reavaliar se aparecer `erro_http`
  com 429 em `ciclos_coleta`.
- **Segundo coletor em outra máquina.** A chave da `posicoes_gps` já descarta a
  duplicata, então funcionaria sem mudar o banco. Mas dobra o custo de máquina
  (seção 3 do `docs/deploy-aws.md`) e, enquanto o banco for um só, não protege
  contra a queda do banco. Só faz sentido depois do buffer e do alarme externo,
  e se as lacunas continuarem relevantes.

## 7. Melhorias propostas, em ordem de valor

| # | Melhoria | Resolve | Tamanho | Onde |
|---|---|---|---|---|
| 1 | Buffer em disco do JSON de cada ciclo, com reenvio, preenchendo `arquivo_bruto` | Banco fora (perda total e invisível) | M | Card novo (H29) |
| 2 | Coletor no compose de produção, reinício automático e alarme externo | Coletor ou máquina caídos | — | Já no T13/T14 |
| 3 | `status` novo `suspeito` e validação da resposta (0 veículos, queda brusca, `hr` repetido) | Resposta vazia ou velha (perda invisível) | M | Card novo (H13, H29), com migração |
| 4 | `status` novo `pulado` para o disparo pulado ou perdido, com motivo | Lacuna sem motivo (H13) | P | Card novo, pode entrar na mesma migração do item 3 |
| 5 | Gravação dentro do orçamento de 30 s (prazo de ~20 s ou gravação com o tempo restante) | Banco lento pulando o ciclo seguinte | P | Ajuste no coletor (T12 ou card novo) |
| 6 | *Jitter* na espera | Nada urgente | P | Junto com o item 5 |
| 7 | Reavaliar o disjuntor e o segundo coletor | Queda longa da API ou da máquina | — | Depois de 1 mês de coleta em produção |

## 8. Limites deste estudo

- Os cenários foram simulados, não provocados na API real. A tabela da seção 4
  vem do código e das medições do T12.
- Não medimos quantas posições de cada resposta são repetidas (`n_recebidos`
  contra `n_inseridos`). Esse número confirmaria a estimativa da seção 3 e dá para
  tirar com uma consulta simples em `bruto.ciclos_coleta` depois de um dia de coleta.
- O tempo de falha da gravação com o banco fora depende do sistema (no Windows,
  ~2 s por conexão recusada).
