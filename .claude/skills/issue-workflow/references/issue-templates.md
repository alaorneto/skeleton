# Modelos de issue, PR e fechamento

Os títulos de seção (`##`) são fixos: os formulários em `.github/ISSUE_TEMPLATE/` usam os mesmos nomes, e revisores procuram por eles. Troque o texto entre `<>`; mantenha toda seção, usando "Não se aplica — <motivo>" quando for o caso.

## feature / enhancement / refactor / test / ci / chore

~~~markdown
## Contexto
<problema atual, quem é afetado e por quê agora; link para o plano, discussão ou issue de origem>

## Objetivo
<resultado esperado em 1–3 frases, do ponto de vista de quem usa, opera ou estende o projeto>

## Escopo
**Inclui**
- <entrega 1>

**Fora do escopo**
- <o que não será feito aqui e onde será tratado>

## Abordagem técnica
- Módulos e arquivos: `<skeleton/...>`, `<tests/...>`
- Decisões e alternativas descartadas: <...>
- Fronteiras do AGENTS.md envolvidas: <núcleo x `applications`, app labels, `/platform/`, specs, migrations de infraestrutura...>

## Critérios de aceite
- [ ] <comportamento observável e verificável, com entrada e resultado>
- [ ] <negativo: o que deve ser recusado/omitido>

## Plano de testes
- [ ] Unitários (`tests/unit/...`): <o que provar>
- [ ] Integração SQLite (`tests/integration_sqlite/...`): <o que provar>
- [ ] PostgreSQL com role runtime (`tests/integration_postgres/...`): <o que provar | Não se aplica — motivo>
- [ ] Negativos de segurança: <tenant A/B, default deny, CAS, auditoria, segredos redigidos...>
- [ ] Smoke manual ou no navegador: <roteiro curto | Não se aplica>

Comandos:
```sh
<comandos exatos para reproduzir a verificação>
```

## Documentação a atualizar
- [ ] `<docs/....md>` — <o que muda>
- [ ] `docs/testing.md` — evidência dos testes executados
- [ ] `README.md` / `AGENTS.md` — <se mudar contrato, setup ou navegação | Não se aplica>

## Segurança e contrato do AGENTS.md
- [ ] <itens pertinentes do "Checklist mínimo de nova tabela" ou de "Contexto, autorização e concorrência">
<ou: Sem impacto — motivo>

## Riscos e dependências
- Parte de / depende de: <#E, #N | nenhuma>
- Migrations, compatibilidade e rollback: <...>

## Definição de pronto
- [ ] Critérios de aceite verificados
- [ ] Plano de testes executado e registrado (somente o que rodou de fato)
- [ ] Documentação atualizada
- [ ] CI verde
- [ ] Commits com `Refs #<N>` e PR com `Closes #<N>`
- [ ] Aprovado pelo responsável
~~~

## bug

Use o modelo acima com estas seções no lugar de "Contexto" e "Objetivo":

~~~markdown
## Comportamento observado
<o que acontece, com mensagem de erro, URL, status HTTP ou log redigido>

## Comportamento esperado
<o que deveria acontecer e onde isso está documentado>

## Passos para reproduzir
1. <...>

## Ambiente
<branch/commit, banco (SQLite/PostgreSQL), settings, navegador, versão do core/agente>

## Causa provável
<diagnóstico, se conhecido; senão "A investigar">
~~~

O "Plano de testes" de um bug sempre inclui um teste de regressão que falha antes da correção e passa depois. Bug de segurança recebe também o label `security`; não publique dados sensíveis, segredos ou passos de exploração além do necessário.

## documentation

~~~markdown
## Contexto
<lacuna ou divergência entre documentação e código>

## Objetivo
<o que o leitor saberá fazer depois>

## Documentos afetados
- [ ] `<docs/....md>` — <seção e mudança>

## Público
<desenvolvedor do projeto derivado, operador, agente, revisor de segurança>

## Critérios de aceite
- [ ] Conteúdo confere com o código em `<arquivos>`
- [ ] Links e âncoras válidos; comandos testados quando houver
- [ ] Nenhuma regra do AGENTS.md contradita

## Verificação
<como conferir: comandos executados, arquivos lidos, renderização>

## Definição de pronto
- [ ] Critérios de aceite verificados
- [ ] Commits com `Refs #<N>` e PR com `Closes #<N>`
- [ ] Aprovado pelo responsável
~~~

## epic

~~~markdown
## Contexto
<resumo do plano aceito e do problema>

## Objetivo
<resultado do conjunto>

## Itens
- [ ] #<N> <título> — <depende de #M | independente>

## Critérios de conclusão
- [ ] Todas as issues filhas fechadas como concluídas ou dispensadas pelo responsável
- [ ] Documentação de visão geral atualizada: <docs/...>
~~~

## Corpo de PR

~~~markdown
## Issues
Closes #<N>

## Resumo
- <mudança 1>

## Testes executados
- `<comando>` — <resultado observado>
<serviços indisponíveis: declare como bloqueio, não como aprovação>

## Documentação
- <arquivos atualizados | Não se aplica — motivo>

## Segurança e contrato
- <itens do AGENTS.md verificados | Sem impacto — motivo>

🤖 Generated with [Claude Code](https://claude.com/claude-code)
~~~

## Comentário de fechamento

~~~markdown
Concluído e aprovado por <quem / "merge do PR #P">.

**Commits**
- <sha curto> <assunto>

**Testes executados**
- `<comando>` — <resultado>

**Documentação**
- <arquivos>

**Critérios de aceite:** todos verificados <ou: dispensados por <quem>: <quais e motivo>>.

**Follow-ups:** <#N | nenhum>
~~~
