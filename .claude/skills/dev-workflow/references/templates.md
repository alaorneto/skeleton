# Modelos de issue, PR e comentários

Os títulos de seção (`##`) são os que a configuração do projeto exige (`issues.sections` e `pr.required_sections` em `.github/dev-workflow.toml`); os formulários em `.github/ISSUE_TEMPLATE/` usam os mesmos nomes, e o check do PR procura por eles. Se o projeto tiver seções próprias (segurança, conformidade), acrescente-as aqui do mesmo jeito. Troque o texto entre `<>`; mantenha toda seção, usando "Não se aplica — <motivo>" quando for o caso.

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
- Módulos e arquivos: `<src/...>`, `<tests/...>`
- Decisões e alternativas descartadas: <...>
- Limites de arquitetura envolvidos: <módulos que não podem depender um do outro, contratos públicos tocados>

## Critérios de aceite
- [ ] <comportamento observável e verificável, com entrada e resultado>
- [ ] <negativo: o que deve ser recusado/omitido>

## Plano de testes
- [ ] Unitários: <o que provar>
- [ ] Integração: <o que provar | Não se aplica — motivo>
- [ ] Negativos: <o que deve ser recusado ou omitido>
- [ ] Smoke manual ou no navegador: <roteiro curto | Não se aplica>

Comandos:
```sh
<comandos exatos para reproduzir a verificação>
```

## Documentação a atualizar
- [ ] `<docs/....md>` — <o que muda>
- [ ] `README.md` — <se mudar instalação, uso ou contrato | Não se aplica>

## Riscos e dependências
- Parte de / depende de: <#E, #N | nenhuma> (também como parent e "blocked by" nativos)
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
<branch/commit, versão do projeto, sistema, navegador, configuração>

## Causa provável
<diagnóstico, se conhecido; senão "A investigar">
~~~

O "Plano de testes" de um bug sempre inclui um teste de regressão que falha antes da correção e passa depois. Em bug de segurança, não publique dados sensíveis, segredos ou passos de exploração além do necessário.

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
<!-- Resumo legível. O vínculo oficial são os sub-issues e os "blocked by" nativos; confira com `github_issues.py sync-epic <E> --dry-run`. As caixas são marcadas pelo workflow de epics quando a filha fecha; não marque à mão. -->
- [ ] #<N> <título> — <depende de #M | independente>

## Critérios de conclusão
- [ ] Todas as issues filhas fechadas como concluídas ou dispensadas pelo responsável
- [ ] Documentação de visão geral atualizada: <docs/...>
~~~

## Corpo de PR

O título do PR segue Conventional Commits (`<tipo>(<escopo>): <assunto>`). O PR leva exatamente um label entre `release` e `no-release`.

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

## Versão
- <impacto para quem usa o projeto: nenhum | correção | funcionalidade nova | quebra de compatibilidade + caminho de migração>
<só com label semver:*:>
- Motivo da sobreposição: <por que o nível calculado não vale aqui>
~~~

## Comentário de mudança de escopo

Na issue, no momento em que o escopo muda, depois de atualizar o corpo dela:

~~~markdown
**Mudança de escopo**

- Antes: <o que a issue previa>
- Agora: <o que passa a incluir ou deixa de incluir>
- Motivo: <o que se descobriu>
- Efeito: <critérios de aceite acrescentados ou removidos; tipo e nível de versão mudam? | excedente movido para #M>
~~~

## Comentário de fechamento (sem PR)

Quando há PR, o comentário de evidências é escrito sozinho no merge. Este modelo é para a entrega aprovada no chat, sem PR:

~~~markdown
Concluído e aprovado por <quem>.

**Commits**
- <sha curto> <assunto>

**Testes executados**
- `<comando>` — <resultado>

**Documentação**
- <arquivos>

**Critérios de aceite:** todos verificados <ou: dispensados por <quem>: <quais e motivo>>.

**Follow-ups:** <#N | nenhum>
~~~

## Decisão ou bloqueio durante o trabalho

~~~markdown
**Decisão:** <o que foi decidido> — <motivo e alternativa descartada>
~~~

~~~markdown
**Bloqueio:** <o que impede> — <o que já foi tentado; do que depende>
~~~
