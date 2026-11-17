---
name: processo-trello
description: Processo Scrum + Kanban do time no quadro Trello "Sprint 2ºSemestre". Use ao iniciar, revisar ou concluir uma tarefa, ao criar ou mover cards, ao escolher a próxima tarefa ou ao nomear branch e commit a partir de um card.
---

# Processo no Trello (Scrum + Kanban)

Quadro: https://trello.com/b/qypssKqv/sprint-2semestre · sprint de 1 semana · a meta da sprint fica no topo do quadro e orienta a prioridade.

## Colunas
Product Backlog → Sprint Backlog → To - Do → Doing (WIP 4) → Revision (WIP 2) → Done.

O WIP não está configurado no Trello: conte os cards antes de mover. Coluna cheia → não mova; reporte o gargalo.

## Card de tarefa
- Título: `H<n> – <verbo + tarefa>` (ex.: `H1 – Criar repositórios back/frontend`). O `H<n>` liga card, branch (`feat/h1-...`) e commit (`Refs: H1`).
- Responsável: membro do card.
- Etiquetas, uma de cada grupo:
  - tamanho: `P` (≤2h) · `M` (meio dia) · `G` (≥1 dia → quebrar em cards menores);
  - categoria: `CÓDIGO` · `DADOS` · `DESIGN` · `DOCUMENTAÇÃO`;
  - prioridade: Alta · Média · Baixa.
- Datas criada/iniciada/pronta vêm do histórico do card (criação e movimentos); não preencha à mão.
- Card sem `H<n>`, responsável ou etiquetas não sai de To - Do.

## Movimentos
- Responsável: To - Do → Doing ao começar; Doing → Revision quando a branch está pronta para revisão.
- Revisor (um colega): Revision → Done após o merge na `develop` com testes e lint verdes e o critério de aceite atendido.

## Agentes
- Com acesso ao Trello: atualize só o card da tarefa atual. Sem acesso: diga ao humano o que mover.
- Sem card para a tarefa: pergunte antes de começar.
