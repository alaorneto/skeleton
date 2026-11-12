# O fluxo completo e as decisões por trás dele

Conteúdo: [ciclo](#ciclo) · [PR sem versão](#o-que-acontece-num-pr-sem-versão) · [PR com versão](#o-que-acontece-num-pr-com-versão) · [mecanismos](#mecanismos) · [decisões](#decisões-e-alternativas-descartadas) · [limites](#limites-conhecidos)

## Ciclo

```
plano aceito ─► issues (+ epic) ─► branch <tipo>/<N>-<slug> ─► commits "Refs #N"
      ─► PR "Closes #N" + label release | no-release ─► check + prévia de versão
      ─► merge ─► evidências nas issues ─► [release] versão, changelog, tag, GitHub Release
```

1. **Planejar.** O plano termina com "Itens do GitHub". Revisar o plano é revisar a decomposição.
2. **Issues.** Uma por entrega revisável, um label de tipo cada, todas as seções do modelo. Com duas ou mais, um epic com sub-issues e "blocked by" nativos.
3. **Desenvolver.** Branch a partir da issue, commits em Conventional Commits com `Refs #N`. Mudança de escopo atualiza a issue ou abre outra.
4. **Revisar.** PR pelo modelo, classificado como `release` ou `no-release`. O check confere e comenta a prévia.
5. **Aprovar.** O merge é a aprovação. Evidências, versão e fechamento acontecem sozinhos.

## O que acontece num PR sem versão

1. O PR é aberto com `Closes #N` e o label `no-release`.
2. **"Verificação do PR"** roda a cada push, edição e troca de label: issue existe e tem um tipo; seções do modelo preenchidas; exatamente um label de classificação; título em Conventional Commits; arquivos de versão coerentes. Avisa (sem reprovar) sobre critérios de aceite não marcados, seções faltando na issue e tipo do título diferente do tipo da issue.
3. O mesmo workflow comenta a **prévia**: "este PR não publica versão; fica acumulado com K mudanças; se a versão saísse depois deste merge, seria vX (nível)". É um comentário único, atualizado no lugar.
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
8. **Marca**: comentário "publicado na versão vX.Y.Z" em cada PR incluído (inclusive os `no-release` antigos) e em cada issue entregue; milestone `vX.Y.Z` com todos eles, fechada.
9. Dispara o workflow de publicação de artefatos, se configurado.

No modo `pull-request` (branch protegida), os passos 6 a 8 viram: abre ou atualiza o PR `release/next` com o commit de release; a publicação acontece quando esse PR é mesclado.

## Mecanismos

| Camada | Onde | O que garante |
|---|---|---|
| Procedimento | `SKILL.md` | Passo a passo do agente |
| Configuração | `.github/dev-workflow.toml` | Tudo que é do projeto: tipos, seções, arquivos de versão, superfícies, modo de release |
| Hook do git | `commit-msg` (pre-commit ou `bootstrap.py install-hook`) | Commit sem referência a issue é recusado; `Refs: #N` sai do nome da branch |
| Hook do Claude Code | `assets/claude/dev-workflow-hook.mjs` | Lembra de criar as issues ao aceitar um plano; recusa `--no-verify` e PR sem referência. Lembrete forte, não barreira |
| Check do PR | `pr-check.yml` → `github_issues.py check-pr`, `release.py check`, `release.py preview` | Vínculo, seções, classificação, título, coerência dos arquivos de versão, prévia |
| Evidências | `pr-merged.yml` → `github_issues.py pr-merged` | Comentário de evidências; classificação padrão de PR não classificado |
| Release | `release.yml` → `release.py run` | Versão, arquivos, changelog, tag, GitHub Release, marcações |
| Análise | `semver-analysis.yml` (opcional) ou o agente local | Nível proposto a partir do diff acumulado |
| Epics | `epic-checklist.yml` → `sync-checklist`, `close-epic` | Caixas e fechamento do epic |
| Conferência | `bootstrap.py doctor` | Instalação íntegra |

## Decisões e alternativas descartadas

**A tag é a fonte da versão; os arquivos são projeções.** Alternativa: um arquivo como fonte. Com a tag, uma branch de manutenção tem a versão certa sem configuração, o "pendente" é um intervalo do git que não depende de marcação mutável, e desfazer uma release é apagar a tag. O custo é o projeto precisar de uma tag de base.

**Lista declarativa de arquivos, com texto literal ou regex.** Alternativa: suporte embutido a cada ecossistema. O skill não sabe o que é `pyproject.toml` ou `Cargo.toml` na hora do bump; ele troca texto com contagem conferida. Funciona em qualquer tecnologia e falha alto quando o arquivo diverge. O bootstrap reconhece os arquivos mais comuns só para sugerir a configuração.

**Release depois do merge, não dentro do PR.** Alternativa (a sugestão original lida ao pé da letra): commitar o bump no PR antes do merge. Descartada porque: dois PRs `release` abertos ao mesmo tempo calculam o mesmo número e o segundo conflita em changelog e arquivos; o número calculado envelhece se outro PR entra antes; commit feito pelo token do Actions não redispara os checks, e o PR ficaria sem check na ponta; PR de fork não aceita push. Depois do merge há uma fila só (um grupo de concorrência por branch) e um estado só. O que se preserva da sugestão: a classificação é do PR, e a prévia mostra no PR o número que vai sair.

**Dois modos de escrita na branch.** `direct` (padrão) commita na branch e publica na mesma execução: zero passos manuais, mas exige que o token possa escrever nela. `pull-request` abre um PR de release e publica no merge dele: convive com branch que exige PR, ao custo de um clique. O estado é o mesmo nos dois; só muda quem leva o commit à branch.

**Idempotência por estado, não por memória.** Cada execução deriva tudo de tags, do commit de release e do GitHub: tag sem release → cria a release; commit de release sem tag → cria a tag naquele commit; nada pendente → não faz nada. Cada marcação confere antes se já existe. Não há arquivo de estado nem "último PR processado".

**O maior nível vence; a análise só eleva.** Um modelo de linguagem reduzindo um nível que um humano declarou (o label de tipo) é o erro caro. Reduzir é sobreposição do dono, com motivo registrado.

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
