# Deploy na AWS: banco, coletor, worker e API

- **Status:** Rascunho, para o time ler e aprovar (card T13)
- **Data:** 2026-11-21
- **Base:** ADR-001, ADR-002 e ADR-003 (banco num EC2, em Docker)

Este documento diz onde roda cada peça da plataforma na AWS, quanto custa e como
fazer o deploy e voltar atrás. A infra só é criada depois de o time aprovar este
plano. Quem cria é o T14.

O motivo do plano é o coletor da Olho Vivo. A API só devolve a posição do momento:
o que não foi coletado não dá para buscar depois. Enquanto a coleta roda no notebook
de alguém, cada desligamento vira um buraco nos dados (H29).

## Resumo das decisões propostas

| Tema | Proposta | Situação |
|---|---|---|
| Banco | EC2 em Docker, imagem de `db/Dockerfile`, dados num volume EBS só para eles | Decidido (ADR-003) |
| Coletor, worker e API | **No mesmo EC2 do banco**, num `docker-compose.prod.yml` | Proposta |
| Instância | `m7i.large` (2 vCPU, 8 GiB, x86_64) | Proposta |
| Disco | EBS gp3 de **300 GB** só para os dados do Postgres, além do disco do sistema | Proposta |
| Região | `sa-east-1` (São Paulo), se o orçamento permitir. Senão, `us-east-1` | **O time decide** |
| Acesso do time | SSM Session Manager, sem porta 22 nem 5432 abertas | Proposta |
| Segredos | SSM Parameter Store (`SecureString`) | Proposta |
| TLS no Postgres | Não, por enquanto (o banco não sai da máquina) | Proposta |
| Backup | Snapshot diário do EBS (DLM) e `pg_dump` diário dos schemas pequenos no S3 | Proposta |
| Monitoramento | CloudWatch: coleta parada há 10 min, instância com falha, disco acima de 80% | Proposta |
| Deploy | Manual por SSM no começo, GitHub Actions depois | Proposta |

## 1. Onde roda cada peça

```
                    Internet
                       │  (só 443, quando o frontend precisar da API)
┌──────────────────────┼──────────────────────────────────────────┐
│ EC2 m7i.large        │                    sa-east-1, subnet      │
│                      ▼                    pública, IP fixo       │
│  ┌────────────┐  ┌───────┐   ┌────────┐   ┌──────────┐           │
│  │ proxy HTTPS│→ │  api  │   │ worker │   │ coletor  │──→ Olho Vivo
│  └────────────┘  └───┬───┘   └───┬────┘   └────┬─────┘           │
│                      │ api_app   │ worker_app  │ coletor_app     │
│                      ▼           ▼             ▼                 │
│                 ┌──────────────────────────────────┐             │
│                 │ db (Postgres 16 + PostGIS +       │             │
│                 │ pg_partman), rede interna do      │             │
│                 │ Docker, sem porta publicada       │             │
│                 └───────────────┬──────────────────┘             │
│                                 │ /srv/onibus/postgres           │
└─────────────────────────────────┼────────────────────────────────┘
                                  ▼
                   EBS gp3 300 GB ──snapshot diário──→ EBS snapshots
                   pg_dump diário (schemas pequenos) ─→ S3
```

**Por que tudo numa máquina só**

- O coletor não tem buffer (ainda é tema do T15). Se o banco cair, ele não grava,
  esteja onde estiver. Separar as máquinas não evita o buraco de coleta.
- A carga de hoje é leve. Na medição do T12 (1 h, 20/11), a média por ciclo foi de
  0,74 s de chamada e 0,06 s de gravação, com ~4.400 posições a cada 30 s.
- É uma máquina para cuidar, um só custo e o mesmo `docker compose` que o time já
  usa no `make up`.

**Quando separar:** quando o worker passar a calcular os indicadores e disputar
CPU e memória com o Postgres, ou quando o T15 propuser um segundo coletor. Aí a
API e o worker vão para outra instância, e a conexão com o banco passa a exigir
TLS (ver seção 4).

**Alternativas descartadas por ora**

- **ECS Fargate para a API, o worker e o coletor:** cobra por vCPU e GB por hora
  em cada serviço que fica ligado 24 h, pede ECR, task definitions e um load
  balancer (~US$ 16/mês ou mais só ele). Faz sentido com mais serviços ou com
  escala automática, o que não é o caso agora.
- **Uma instância pequena para os serviços e outra para o banco:** custa mais e,
  como dito acima, não melhora a coleta sem o buffer do T15.

### O que falta no repositório para isso funcionar (T14)

1. **`docker-compose.prod.yml`**: o compose de hoje é para o ambiente local.
   Para produção, é preciso:
   - um serviço `coletor` (`python -m onibus.coleta --schedule`), que hoje não
     está no compose;
   - `restart: unless-stopped` em todos os serviços (hoje é `on-failure`, que não
     volta depois de um reboot) e o Docker habilitado no boot (`systemctl enable docker`);
   - o volume do banco num diretório do disco EBS de dados
     (`/srv/onibus/postgres`), e não num volume nomeado no disco do sistema;
   - as portas do `db` e da `api` publicadas só em `127.0.0.1` do host (para o
     túnel do SSM e o alarme da seção 7), nunca em `0.0.0.0`;
   - os mesmos parâmetros do `pg_partman_bgw` do compose local.
2. **Login do worker** (`worker_app`): o card T14 cita, mas o `Settings` ainda
   só tem os logins da API e do coletor.
3. **Arquitetura da imagem:** usar instância **x86_64** (família `m7i`/`t3`).
   A imagem do banco fixa o digest da base e as versões dos pacotes do PGDG para
   dar o mesmo banco em qualquer máquina. Uma instância ARM (Graviton, ~20% mais
   barata) exigiria refazer essa fixação e os testes para `arm64`.

## 2. Tamanho da instância e do disco

**Volume medido.** A coleta de 1 h do T12 gravou 536.568 posições, ou ~12,9
milhões por dia e **~390 milhões por mês**. Isso confirma a estimativa da ADR-002
(~400 milhões, ~64 GB por mês com índices, a 160 bytes por linha).

**Disco de dados: gp3 de 300 GB.**

| Item | GB |
|---|---|
| `bruto.posicoes_gps`, 3 meses de retenção | ~190 |
| Partições já criadas para os meses seguintes (`p_premake => 3`) e WAL | ~10 |
| `operacao.passagens` e indicadores (ainda não medidos) | reserva |
| Folga para o disco não passar de 80% | o resto |

> **Atenção à retenção:** a migração 0003 configura `retention = '3 months'` com
> `retention_keep_table = true`. Ou seja, o `pg_partman` **desanexa** o mês velho,
> mas a tabela continua no disco até o job mensal da ADR-002 exportá-la para
> Parquet no S3 e apagá-la. Sem esse job, o disco cresce ~64 GB por mês sem
> parar. O gp3 aumenta de tamanho sem desligar a máquina, mas alguém precisa
> acompanhar. Proposta: abrir um card para o job de arquivamento antes do 3º mês
> de coleta.

**Instância: `m7i.large`** (2 vCPU, 8 GiB). A `t3.large` tem o mesmo tamanho e
custa ~16% menos, mas é burstable: com o coletor e o Postgres ligados 24 h ela
pode gastar os créditos de CPU e passar a cobrar extra (modo *unlimited*) ou
ficar lenta. Para começar com o menor custo, dá para usar a `t3.large` e olhar a
métrica `CPUCreditBalance` na primeira semana.

## 3. Custo mensal estimado

Preços on-demand, 730 h por mês, sem impostos. **Conferir na
[AWS Pricing Calculator](https://calculator.aws/) antes de aprovar**: os valores
de `sa-east-1` vieram de agregadores e a coluna dos EUA é a tabela pública de
`us-east-1`.

| Item | `sa-east-1` (São Paulo) | `us-east-1` (Virgínia) |
|---|---|---|
| EC2 `m7i.large` (US$ 0,1606/h · US$ 0,1008/h) | ~US$ 117 | ~US$ 74 |
| EBS gp3 300 GB de dados + 30 GB do sistema | ~US$ 50 | ~US$ 26 |
| Snapshots do EBS (incrementais, ~100 GB em uso) | ~US$ 7 | ~US$ 5 |
| S3 dos dumps (~20 GB) | ~US$ 1 | ~US$ 0,50 |
| IPv4 público | ~US$ 3,65 | ~US$ 3,65 |
| CloudWatch (métrica própria + 3 alarmes) e transferência | ~US$ 3 | ~US$ 3 |
| **Total com `m7i.large`** | **~US$ 180** | **~US$ 112** |
| **Total com `t3.large`** | ~US$ 160 | ~US$ 99 |

O disco cresce ~US$ 10 (São Paulo) ou ~US$ 5 (EUA) por mês a cada 64 GB a mais,
se o arquivamento em Parquet não rodar.

**São Paulo ou EUA?**

- São Paulo fica ~60% mais caro, mas o coletor fica perto da Olho Vivo e os dados
  ficam no Brasil.
- Nos EUA, cada chamada à Olho Vivo ganha ~120 ms de ida e volta. Com p95 de
  5,6 s por ciclo, isso não muda nada. **Mas é preciso testar se a Olho Vivo
  aceita chamadas de fora do Brasil** antes de escolher os EUA (ver seção 9).
- Antes de decidir, conferir se o Insper ou a AWS Educate dão créditos ao time.

**Alerta de orçamento:** criar no AWS Budgets um orçamento mensal com alertas por
e-mail em 50%, 80% e 100% do valor aprovado, e outro alerta de *forecast* acima
de 100%.
## 4. Rede, acesso e segurança

- **VPC padrão, subnet pública**, com IP fixo (Elastic IP) para a Olho Vivo e,
  depois, para o domínio da API. Uma subnet privada pediria NAT Gateway
  (~US$ 35/mês) só para o coletor sair para a internet.
- **Security group da instância:**
  - entrada: nada, enquanto a API não for publicada. Depois, só a 443 (HTTPS).
  - **Nunca abrir a 5432 nem a 22.**
  - saída: liberada (Olho Vivo, SPTrans, S3, SSM).
- **Acesso do time pelo SSM Session Manager**, com o próprio usuário IAM de cada
  um. Não há chave SSH para compartilhar, e cada sessão fica registrada. Para
  usar o `psql` ou o DBeaver do notebook, um túnel do SSM até a porta do banco:
  ```bash
  aws ssm start-session --target <id-da-instancia> \
    --document-name AWS-StartPortForwardingSessionToRemoteHost \
    --parameters host=127.0.0.1,portNumber=5432,localPortNumber=15432
  ```
  Por isso o compose de produção publica a porta do banco em `127.0.0.1` do host
  (seção 1).
- **IAM:**
  - cada pessoa do time com usuário próprio, MFA e permissão só para o que usa
    (EC2, SSM, S3 do projeto, CloudWatch, Budgets). Ninguém usa a conta root;
  - a instância com uma *role* que lê os parâmetros do projeto, grava no bucket
    de backup e envia métricas ao CloudWatch, e nada além disso.
- **TLS no Postgres: não exigir por enquanto.** O banco só aceita conexões da
  rede interna do Docker e do túnel do SSM, que já é criptografado. Isso resolve
  a pendência da ADR-003 sem mudar o `MigrationSettings`. Quando o banco ficar em
  outra máquina (seção 1), aí sim: certificado no Postgres e `sslmode=require` no
  `Settings` e no `MigrationSettings`, num card próprio.
- **API na internet:** quando o frontend precisar dela, colocar um proxy HTTPS
  (Caddy, com certificado automático do Let's Encrypt) na frente, o que exige um
  domínio. Onde o frontend vai rodar não faz parte deste card.

## 5. Segredos

- Ficam no **SSM Parameter Store**, como `SecureString`, sob `/onibus/prod/`:
  `POSTGRES_PASSWORD`, `APP_API_DATABASE_PASSWORD`,
  `APP_COLETOR_DATABASE_PASSWORD`, a senha do `worker_app` e
  `APP_OLHO_VIVO_TOKEN`. O tier padrão é gratuito. O Secrets Manager cobra
  US$ 0,40 por segredo por mês e a rotação automática dele não é necessária agora.
- No deploy, um script na instância lê os parâmetros com a *role* da instância e
  gera o `.env` com permissão `600`, fora do diretório do git. Nada de segredo no
  git, na imagem Docker ou no *user data* da instância.
- Trocar uma senha: atualizar o parâmetro, rodar o `ALTER ROLE ... PASSWORD` no
  banco e reiniciar o serviço.

## 6. Backup e restauração

| O quê | Como | Guarda | Para quê |
|---|---|---|---|
| Disco de dados inteiro | Snapshot do EBS pelo Data Lifecycle Manager, todo dia às 03:00 (horário de Brasília) | 7 diários + 4 semanais | Voltar a máquina inteira depois de pane, disco corrompido ou erro grave |
| Schemas `referencia`, `operacao` e `indicadores` | `pg_dump -Fc` diário, enviado ao S3 | 30 dias (regra de ciclo de vida no bucket) | Voltar uma tabela apagada sem restaurar o disco todo |
| Meses fechados de `bruto.posicoes_gps` | Parquet no S3 (job mensal da ADR-002) | Sem prazo | Arquivo e download (H9) |

- **Por que não um `pg_dump` diário do banco todo:** o `bruto` cresce ~2 GB por
  dia e passa de 150 GB no 3º mês. Um dump completo por dia seria lento, pesaria
  no banco e guardaria a mesma posição várias vezes. O snapshot é incremental e
  cobre o `bruto`.
- O snapshot de um Postgres ligado é consistente como depois de uma queda de
  energia. O Postgres se recupera disso pelo WAL, **desde que os dados e o WAL
  estejam no mesmo volume**, o que é o caso aqui.
- **Restauração testada** (critério do T14): criar um volume a partir do último
  snapshot, ligá-lo a uma instância de teste, subir a mesma imagem do banco e
  conferir as contagens de `bruto.ciclos_coleta` e de `referencia.gtfs_versoes`.
  Depois, apagar a instância e o volume de teste. Repetir a cada mês.

## 7. Monitoramento e alertas

Os alertas vão para um tópico do SNS, com o e-mail do time inscrito.

| Alarme | Como | Dispara quando |
|---|---|---|
| **Coleta parada** (H29) | Um timer do systemd na instância lê o `/health` (que mostra o último ciclo com sucesso, T10) a cada minuto e envia ao CloudWatch a métrica "minutos desde o último ciclo ok" | Acima de 10 por 10 min seguidos, ou métrica ausente (a própria máquina caiu) |
| Instância com falha | Métrica `StatusCheckFailed_System`, com a ação de *recover* do EC2 | 2 min seguidos |
| Disco de dados cheio | Agente do CloudWatch (`disk_used_percent` em `/srv/onibus`) | Acima de 80% |
| Partições | `operacao.saude_particoes` entra na mesma rotina do timer | Falta a partição do mês seguinte |

O alerta de 10 min do T12 fica dentro do coletor e só aparece no log. O alarme
acima é o que de fato avisa alguém, inclusive quando o próprio coletor caiu.

## 8. Deploy e como voltar atrás

**Primeira vez (T14)**

1. Criar a instância, o disco de dados, o Elastic IP, o security group, a *role*
   e os parâmetros (pelo console, anotando cada passo no fim deste documento).
2. Instalar o Docker, montar o disco em `/srv/onibus` e clonar o repositório em
   `/opt/onibus`.
3. Gerar o `.env` a partir do Parameter Store.
4. `docker compose -f docker-compose.prod.yml up -d db`.
5. Rodar as migrações como o dono do banco, fixando a primeira partição:
   `alembic -x inicio_particoes=AAAA-MM-01 upgrade head` (ADR-003).
6. Criar `api_app`, `coletor_app` e `worker_app` como no README, com as senhas do
   Parameter Store.
7. Carregar o GTFS atual (`make load-gtfs GTFS=...`).
8. Subir o resto (`docker compose -f docker-compose.prod.yml up -d`) e conferir o
   `/health` e um ciclo gravado em `bruto.ciclos_coleta`.

**Cada deploy, no começo (manual)**

1. Fazer o merge na `main`, que é a release (AGENTS.md), e marcar uma tag
   (`v0.N.0`).
2. Se a versão tiver migração nova, **tirar um snapshot do disco de dados antes**.
3. Abrir sessão pelo SSM: `git fetch --tags && git checkout v0.N.0`.
4. `make migrate`, se houver migração.
5. `docker compose -f docker-compose.prod.yml up -d --build`.
6. Conferir o `/health` e o próximo ciclo de coleta.

**Voltar atrás**

- Código sem migração: `git checkout` da tag anterior e o mesmo `up -d --build`.
- Com migração: `alembic downgrade` até a revisão anterior, se a migração tiver
  `downgrade` (todas devem ter, pelo AGENTS.md). Se o banco ficou inconsistente,
  restaurar o snapshot do passo 2. Nesse caso, as posições coletadas entre o
  snapshot e a restauração se perdem, então isso é só para emergência.

**Depois (card próprio):** um GitHub Actions que, a cada tag, gera a imagem,
envia ao ECR e manda o comando de deploy pelo SSM (`aws ssm send-command`), sem
abrir porta nem guardar chave no GitHub (OIDC).

## 9. Testes práticos antes de aprovar

O card pede pelo menos um teste prático anotado. Ainda não foi feito, porque falta
a conta da AWS.

- [ ] Subir a imagem de `db/Dockerfile` numa instância x86_64 e conferir que o
  `pg_partman_bgw` cria as partições (`operacao.saude_particoes`).
- [ ] Rodar as migrações 0001 e 0002 nesse banco e conferir os papéis do T5
  (escrita para o coletor/worker, leitura para a API, o papel da API não consegue
  escrever em `bruto`).
- [ ] Rodar um ciclo do coletor (`python -m onibus.coleta`, sem gravar) **a partir
  da região escolhida**, para saber se a Olho Vivo responde de lá.
- [ ] Tirar um snapshot, restaurar em outro volume e subir o banco a partir dele.

Resultado: _(preencher com data, quem fez e o que deu)_

## 10. Pendências para o time decidir

1. Região: `sa-east-1` (~US$ 180/mês) ou `us-east-1` (~US$ 112/mês), e qual é o
   teto de gasto do time.
2. `m7i.large` ou `t3.large`.
3. Quem tem acesso à conta da AWS e quem recebe os alertas.
4. Abrir os cards seguintes: `docker-compose.prod.yml` e login do worker (pode
   entrar no T14), job de arquivamento em Parquet (ADR-002) e deploy pelo GitHub
   Actions.
