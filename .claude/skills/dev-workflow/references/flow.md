# O fluxo completo e as decisões por trás dele

Conteúdo: [ciclo](#ciclo) · [PR sem versão](#o-que-acontece-num-pr-sem-versão) · [PR com versão](#o-que-acontece-num-pr-com-versão) · [epics](#epics-sub-issues-dependências-e-fechamento) · [mecanismos](#mecanismos) · [decisões](#decisões-e-alternativas-descartadas) · [limites](#limites-conhecidos)

## Ciclo

```
plano aceito ─► issues (+ epic) ─► branch <tipo>/<N>-<slug> ─► commits "Refs #N"
      ─► PR "Closes #N" (+ label release, se publica) ─► check + prévia de versão
      ─► merge ─► evidências nas issues ─► [release] versão, changelog, tag, GitHub Release
```

1. **Planejar.** O plano termina com "Itens do GitHub". Revisar o plano é revisar a decomposição.
2. **Issues.** Uma por entrega revisável, um label de tipo cada, todas as seções do modelo. Com duas ou mais, um epic com sub-issues e "blocked by" nativos.
3. **Desenvolver.** Branch a partir da issue, commits em Conventional Commits com `Refs #N`. Mudança de escopo atualiza a issue ou abre outra.
4. **Revisar.** PR pelo modelo, com o label `release` se o merge deve publicar versão. O check confere e comenta a prévia.
5. **Aprovar.** O merge é a aprovação. Evidências, versão e fechamento acontecem sozinhos.

## O que acontece num PR sem versão

1. O PR é aberto com `Closes #N` e sem o label `release`.
2. **"Verificação do PR"** roda a cada push, edição e troca de label: issue existe e tem um tipo; seções do modelo preenchidas; sobreposição de versão (`semver:*`) só com `release` e com motivo; título em Conventional Commits; arquivos de versão coerentes. Avisa (sem reprovar) sobre critérios de aceite não marcados, seções faltando na issue e tipo do título diferente do tipo da issue.
   O check roda o skill e a configuração **da branch base**, nunca os do PR: um PR que alterasse o validador poderia se aprovar. Do PR só se leem dados (corpo, labels, commits e, numa pasta à parte, os arquivos de versão). Um PR que melhora o validador é conferido pela versão anterior; a nova vale a partir do merge. Enquanto a base não tem o skill, o check passa avisando.
3. O mesmo workflow comenta a **prévia**: "este PR não publica versão; fica acumulado com K mudanças; se a versão saísse depois deste merge, seria vX (nível)". É um comentário único, atualizado no lugar. Como escreve no PR, esse job só roda em PR do próprio repositório (fork recebe token de leitura) e também usa só o código da base.
4. Merge.
5. **"Evidências do PR mesclado"** comenta em cada issue citada: commits, testes e documentação declarados no PR, critérios de aceite pendentes, e que a entrega aguarda a próxima versão. O GitHub fecha as issues com `Closes`; a caixa no epic é marcada.
6. **"Versão e release"** roda no push, vê que nenhum PR `release` entrou desde a última tag e termina com "Nada a versionar: K mudanças acumuladas". Nenhum arquivo muda.

O PR não recebe marca de "pendente": pendente é tudo que está entre a última tag e a ponta da branch.

## O que acontece num PR com versão

Passos 1 a 5 iguais, com o label `release`. A prévia diz "ao mesclar, este PR publica vX (nível); entram este PR e K mudanças acumuladas", com a tabela de sinais de cada uma. Se o agente (local ou no CI) registrou a análise do diff acumulado, ela aparece na tabela.

Depois do merge, "Versão e release":

1. Faz checkout da ponta atual da branch com todo o histórico e as tags.
2. **Conclui o que ficou pela metade**: tag sem GitHub Release, ou commit de release sem tag.
3. **Monta o plano**: última tag de versão final → lista os commits do primeiro pai até a ponta → identifica o PR de cada um (merge commit, sufixo `(#N)` do squash, ou consulta à API para rebase) → lê labels, corpo e issues de cada PR → inclui os commits que entraram sem PR.
4. **Decide o nível**: o maior entre os sinais de todas as mudanças, mais a análise por agente, mais o piso de versão; aplica a sobreposição do dono, se houver; aplica a regra de 0.x.
5. **Escreve**: a versão em cada arquivo de `[[versioning.files]]` e a seção nova no topo do `CHANGELOG.md`, agrupada por tipo, uma linha por issue entregue com links para a issue e o PR.
6. **Commita** `chore(release): vX.Y.Z`, com a justificativa no corpo, e faz push na branch. Se a branch andou nesse meio-tempo, descarta, atualiza e recalcula.
7. **Publica**: tag anotada com as notas, GitHub Release com changelog e "Por que este número".
8. **Marca**: comentário "publicado na versão vX.Y.Z" em cada PR incluído (inclusive os antigos, sem `release`) e em cada issue entregue; milestone `vX.Y.Z` com todos eles, fechada.
9. Dispara o workflow de publicação de artefatos, se configurado.

No modo `pull-request` (branch protegida), os passos 6 a 8 viram: abre ou atualiza o PR `release/next` com o commit de release; a publicação acontece quando esse PR é mesclado.

## Epics: sub-issues, dependências e fechamento

O vínculo oficial entre um epic e as filhas é o relacionamento nativo de **sub-issue** do GitHub, e as dependências são relações **"blocked by"** nativas: o epic mostra o progresso sozinho, a filha mostra o parent no painel lateral e o vínculo pode ser conferido pela API. O texto continua no corpo, como resumo para quem lê: a seção "Itens" do epic tem a lista ordenada `- [ ] #N título — depende de #M | independente`, e cada filha diz `Parte de #E`.

`github_issues.py sync-epic <E>` (ou `--all`, para todas as issues com o label de epic; `--dry-run` mostra o que faria) reconcilia esse texto com o GitHub:

- vincula cada item da seção "Itens" como sub-issue, na ordem da lista, e cria um "blocked by" para cada "depende de";
- é idempotente e nunca troca um parent existente: uma issue tem no máximo um parent, e o conflito aparece como divergência;
- termina com erro quando há divergência: issue inexistente ou de outro repositório, item listado em dois epics, sub-issue nativo fora da lista, epic sem o label;
- ignora as linhas de checklist sem número de issue e mostra quantas são;
- também acerta as caixas da seção "Itens".

No dia a dia, quem marca as caixas é o workflow `epic-checklist`: quando uma issue fecha (por PR com `Closes` ou à mão, como concluída ou dispensada), ele marca `[x]` no item dela no epic pai nativo; quando é reaberta, volta a `[ ]`. Cada execução reconcilia o epic inteiro e relê o corpo depois de gravar, então filhas fechadas ao mesmo tempo convergem. Para reconciliar à mão, sem criar vínculos: `sync-checklist <E>` (ou `--all`, `--parent-of <N>`).

Quando a última sub-issue nativa aberta fecha, o mesmo workflow fecha o epic (`close-epic --parent-of <N>`), com um comentário que lista cada filha e se foi concluída ou dispensada: como concluído se ao menos uma filha foi concluída, como `not planned` se todas foram dispensadas. A fonte de verdade são as sub-issues nativas, não a lista em "Itens". Nada é fechado quando ainda há filha aberta ou o epic não tem sub-issues, quando o pai não tem o label de epic ou já está fechado, quando alguma sub-issue é de outro repositório (o token do workflow não a verifica) ou quando a lista vem truncada (mais de 100 sub-issues ou bloqueios); nesses casos, feche à mão.

Um fechamento feito com o token do workflow não dispara outro evento `issues: closed`; por isso, ao fechar um epic que também é sub-issue, o mesmo run atualiza o epic de cima e verifica se ele também pode fechar. Reabrir uma filha não reabre o epic. Os "Critérios de conclusão" do epic não são verificados: o comentário pede para reabrir se algum faltar. Nenhum comando remove vínculos; para desfazer, `gh issue edit <N> --remove-parent` ou `--remove-blocked-by <M>`. Os issue types do GitHub (Epic, Task) não são usados, porque só existem em repositórios de organização.

## Mecanismos

| Camada | Onde | O que garante |
|---|---|---|
| Procedimento | `SKILL.md` | Passo a passo do agente |
| Configuração | `.github/dev-workflow.toml` | Tudo que é do projeto: tipos, seções, arquivos de versão, superfícies, modo de release |
| Hook do git | `commit-msg` (pre-commit ou `bootstrap.py install-hook`) | Commit sem referência a issue é recusado; `Refs: #N` sai do nome da branch |
| Hook do Claude Code | `assets/claude/dev-workflow-hook.mjs` | Lembra de criar as issues ao aceitar um plano; recusa `--no-verify` e PR sem referência. Lembrete forte, não barreira |
| Check do PR | `pr-check.yml` → `github_issues.py check-pr`, `release.py check --tree`, `release.py preview`, todos da branch base | Vínculo, seções, sobreposição de versão, título, coerência dos arquivos de versão, prévia |
| Evidências | `pr-merged.yml` → `github_issues.py pr-merged` | Comentário de evidências nas issues |
| Release | `release.yml` → `release.py run` | Versão, arquivos, changelog, tag, GitHub Release, marcações |
| Análise | `semver-analysis.yml` (opcional) ou o agente local | Nível proposto a partir do diff acumulado |
| Epics | `epic-checklist.yml` → `sync-checklist`, `close-epic` | Caixas e fechamento do epic |
| Conferência | `bootstrap.py doctor` | Instalação íntegra |

## Decisões e alternativas descartadas

**A tag é a fonte da versão; os arquivos são projeções.** Alternativa: um arquivo como fonte. Com a tag, uma branch de manutenção tem a versão certa sem configuração, o "pendente" é um intervalo do git que não depende de marcação mutável, e desfazer uma release é apagar a tag. O custo é o projeto precisar de uma tag de base.

**Lista declarativa de arquivos, com texto literal ou regex.** Alternativa: suporte embutido a cada ecossistema. O skill não sabe o que é `pyproject.toml` ou `Cargo.toml` na hora do bump; ele troca texto com contagem conferida. Funciona em qualquer tecnologia e falha alto quando o arquivo diverge. O bootstrap reconhece os arquivos mais comuns só para sugerir a configuração.

**Release depois do merge, não dentro do PR.** Alternativa (a sugestão original lida ao pé da letra): commitar o bump no PR antes do merge. Descartada porque: dois PRs `release` abertos ao mesmo tempo calculam o mesmo número e o segundo conflita em changelog e arquivos; o número calculado envelhece se outro PR entra antes; commit feito pelo token do Actions não redispara os checks, e o PR ficaria sem check na ponta; PR de fork não aceita push. Depois do merge há uma fila só (um grupo de concorrência por branch) e um estado só. O que se preserva da sugestão: a decisão de versionar é do PR, e a prévia mostra no PR o número que vai sair.

**Só o PR que publica leva label.** Primeiro desenho: um label obrigatório entre `release` e `no-release`, com o check reprovando PR sem nenhum. Descartado a pedido do dono: o caso comum é não publicar, e exigir um label para ele é atrito sem informação. A ausência de `release` é a decisão; o risco de esquecer o label é baixo, porque nada se perde (a mudança fica acumulada) e a prévia em todo PR mostra o que sairia.

**Dois modos de escrita na branch.** `direct` (padrão) commita na branch e publica na mesma execução: zero passos manuais, mas exige que o token possa escrever nela. `pull-request` abre um PR de release e publica no merge dele: convive com branch que exige PR, ao custo de um clique. O estado é o mesmo nos dois; só muda quem leva o commit à branch.

**Idempotência por estado, não por memória.** Cada execução deriva tudo de tags, do commit de release e do GitHub: tag sem release → cria a release; commit de release sem tag → cria a tag naquele commit; nada pendente → não faz nada. Cada marcação confere antes se já existe. Não há arquivo de estado nem "último PR processado".

**O maior nível vence; a análise só eleva.** Um modelo de linguagem reduzindo um nível que um humano declarou (o label de tipo) é o erro caro. Reduzir é sobreposição do dono, com motivo registrado.

**O check confia na base, não no PR.** Alternativa: rodar o validador do próprio PR, como qualquer CI de testes. Descartada porque o check é a barreira do processo, não um teste do código: se o PR pode trocar o validador, ele se aprova. O custo é que uma melhoria do validador só vale depois do merge dela. O mesmo vale para a prévia e para a análise por agente, que leem o diff do PR sem executar nada dele.

**Leitura incompleta não autoriza ação.** O GitHub devolve até 100 sub-issues ou bloqueios por página. Se a lista vier truncada, `sync-epic`, `sync-checklist` e `close-epic` param com erro em vez de fechar um epic ou criar vínculos com base em dados parciais.

**Sinais determinísticos primeiro, agente como reforço.** A release nunca depende de um serviço externo estar disponível. Quando a análise falta, as notas dizem; quando o projeto exige (`require_analysis`), a release espera.

**PRs incluídos são marcados com comentário e milestone, não com label.** A fonte da verdade do "já saiu" é o git; a marca é informação para quem lê. Milestone agrupa PRs e issues de uma versão numa tela nativa do GitHub; um label por versão poluiria a lista.

**Um skill, não dois.** O fluxo de issues e o de versão compartilham a classificação (o tipo da issue decide o nível), o PR e o fechamento. Separados, divergiriam.

**Entrada do changelog por issue, não por commit.** No fluxo issue-first a unidade de entrega é a issue, e o título dela é escrito para quem usa. Commits e PRs sem issue entram com o próprio título.

## Limites conhecidos

- Mensagens dos scripts e textos dos comentários estão em português; títulos de seção e labels são configuráveis.
- Só GitHub (`gh`). O cálculo funciona sem ele (`DEV_WORKFLOW_OFFLINE=1`), com os sinais dos commits.
- Um repositório, uma versão. Monorepo com versões independentes por pacote não é coberto.
- O token padrão do Actions não redispara workflows: o PR de release (modo `pull-request`) não roda checks sozinho, e a tag criada não dispara workflows `on: push: tags`. Use `release.dispatch_workflow` ou o segredo `RELEASE_TOKEN`.
- A análise de import dinâmico, reflexão e contratos implícitos continua sendo julgamento: o agente pode errar, e por isso a sobreposição existe.
