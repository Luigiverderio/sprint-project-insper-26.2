---
name: processo-trello
description: Processo Scrum + Kanban do time no quadro Trello "Sprint 2ºSemestre". Use SEMPRE antes de qualquer ação de um agente no Trello (ler o quadro; criar, editar, mover, comentar ou arquivar cards e listas), seja por conector/MCP do Trello, navegador ou API. Use também ao iniciar, revisar ou concluir uma tarefa, ao escolher a próxima tarefa ou ao nomear branch e commit a partir de um card.
---

# Processo no Trello (Scrum + Kanban)

Quadro: https://trello.com/b/qypssKqv/sprint-2semestre · sprint de 1 semana · a meta da sprint fica no topo do quadro e orienta a prioridade.

## Papéis
- PO: Vitor (vitoros1). Organiza e prioriza o Product Backlog e o Sprint Backlog e define o critério de aceite.
- SM: Luigi (luigimv). Garante o processo (fluxo de branches, commits, card por mudança), vigia o WIP, conduz as cerimônias e remove impedimentos.
- Dúvida de escopo ou prioridade → PO. Dúvida de processo ou bloqueio → SM.

## Colunas
`Product Backlog` → `Sprint Backlog` → `To - Do` → `Doing (WIP 4)` → `Revision (WIP 2)` → `Done`.

Esses são os nomes exatos das listas no Trello; use-os ao procurar ou mover cards.

O WIP (4 em Doing, 2 em Revision) fica só no nome da lista: o plano do Trello não tem limite nativo e nada bloqueia. Conte os cards antes de mover. Lista cheia → não mova; reporte o gargalo ao SM.

## Card de tarefa
- Título: `<id> – <descrição>`. `H<nn>` = história do documento de requisitos (H1–H32); `T<nn>` = tarefa técnica sem história. O id liga card, branch (`feat/h22-...`) e commit (`Refs: H22`).
- Responsável: membro do card.
- Etiquetas, uma de cada grupo:
  - tamanho: `P` (≤2h) · `M` (meio dia) · `G` (≥1 dia → quebrar em cards menores);
  - categoria: `CÓDIGO` · `DADOS` · `DESIGN` · `DOCUMENTAÇÃO`;
  - prioridade: `ALTA` · `MÉDIA` · `BAIXA`.
- Datas planejadas (opcional): início e entrega nos campos de data do card, definidos por quem planeja (ex.: PO).
- Datas reais (criada/iniciada/pronta) vêm do histórico do card (criação e movimentos); não as registre à mão.
- Card sem id (`H<nn>`/`T<nn>`), responsável ou etiquetas não sai de To - Do.

## Movimentos
- Responsável: `To - Do` → `Doing (WIP 4)` ao começar; `Doing (WIP 4)` → `Revision (WIP 2)` quando a branch está pronta para revisão.
- O merge na `develop` pode vir antes da revisão (quando o humano pedir); o card continua em `Revision (WIP 2)`.
- Revisor (um colega, nunca o responsável): `Revision (WIP 2)` → `Done` após revisar, com o merge na `develop` verde e o critério de aceite atendido.

## Agentes
- Carregue esta skill antes de qualquer ação no Trello, mesmo só leitura.
- Com acesso ao Trello: atualize só o card da tarefa atual. Sem acesso: diga ao humano o que mover.
- Não renomeie listas nem mude o WIP sem pedido do humano; se mudarem, atualize esta skill na mesma tarefa.
- Sem card para a tarefa: pergunte antes de começar.
