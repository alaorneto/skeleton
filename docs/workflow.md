# Fluxo de trabalho: plano, issue, commit e fechamento

Todo trabalho versionado neste repositório nasce de uma issue do GitHub e termina com ela fechada depois de aprovado, com os commits e as evidências ligados a ela. A regra normativa está no [AGENTS.md](../AGENTS.md#rastreabilidade-plano-issue-commit-e-fechamento). Este guia explica o ciclo e os mecanismos que o garantem, para pessoas e agentes.

## Ciclo

1. **Planejar.** No modo de planejamento do Claude Code, o plano termina com a seção `Itens do GitHub`: para cada item, o label de tipo, o título, os critérios de aceite, o plano de testes e a documentação a atualizar. Revisar o plano é também revisar essa decomposição.
2. **Aceitar → issues.** Quando o plano é aceito, o agente cria as issues antes de qualquer edição: uma por entrega independente e revisável, cada uma detalhada pelo modelo do tipo. Com duas ou mais, cria também uma issue `epic` com a lista ordenada das filhas e as vincula a ele como sub-issues nativos, com as dependências como "blocked by" (veja [Epics](#epics-sub-issues-e-dependências)). Sem modo de planejamento, a issue é identificada ou criada antes da primeira edição.
3. **Desenvolver.** Branch `<tipo>/<N>-<slug>` a partir de `main` (por exemplo `feat/42-platform-users`). Commits em Conventional Commits, com trailer `Refs #N`. Mudança de escopo atualiza a issue ou abre outra.
4. **Revisar.** PR para `main` pelo [modelo](../.github/pull_request_template.md), com `Closes #N` (entrega completa) ou `Refs #N` (parcial) e somente os testes executados de fato.
5. **Aprovar e fechar.** O merge do PR fecha as issues citadas com `Closes`. Sem PR, a issue só fecha com aprovação explícita do responsável, depois de um comentário com commits, testes executados, documentação atualizada e follow-ups. Quem implementa não fecha a própria issue por conta própria.

## Classificação

Cada issue tem exatamente um label de tipo. O CI recusa PR que cite issue sem tipo ou com dois tipos.

| Label | Quando usar | Prefixo de commit e branch |
|---|---|---|
| `bug` | Comportamento diverge do esperado ou documentado; regressão | `fix` |
| `feature` | Capacidade nova | `feat` |
| `enhancement` | Melhoria de algo existente | `feat` ou `perf` |
| `documentation` | Só documentação | `docs` |
| `refactor` | Reestruturação sem mudança observável | `refactor` |
| `test` | Só testes, fixtures ou evidência | `test` |
| `ci` | Pipeline e checks | `ci` |
| `chore` | Dependências, ferramentas, configuração | `chore` ou `build` |

Labels adicionais: `security` para qualquer impacto em tenant, RLS, autorização, autenticação, auditoria, segredos, CSRF ou CAS; `epic` para a issue que acompanha um plano. Documentação e testes que acompanham uma feature ou um bug ficam na mesma issue, como parte da definição de pronto.

## Conteúdo obrigatório de uma issue

Os formulários em [.github/ISSUE_TEMPLATE](../.github/ISSUE_TEMPLATE) e os modelos do skill usam as mesmas seções:

- **Contexto** e **Objetivo** (em bugs: comportamento observado e esperado, reprodução, ambiente e causa provável).
- **Escopo**, com o que fica fora dele.
- **Abordagem técnica**: módulos e arquivos, decisões, fronteiras do AGENTS.md envolvidas.
- **Critérios de aceite** verificáveis, incluindo os negativos.
- **Plano de testes**: unitários, integração em PostgreSQL com a role runtime, negativos de segurança, smoke manual e os comandos. Bugs incluem um teste de regressão.
- **Documentação a atualizar**, incluindo a evidência em [testing](testing.md).
- **Segurança e contrato do AGENTS.md**.
- **Riscos e dependências**: epic, issues bloqueantes, migrations, rollback. O epic e as bloqueantes também ficam registrados como parent e "blocked by" nativos.
- **Definição de pronto**.

Uma seção sem conteúdo leva "Não se aplica — motivo". Ela não é removida.

## Epics, sub-issues e dependências

O vínculo oficial entre um epic e suas filhas é o relacionamento nativo de **sub-issue** do GitHub, e as dependências entre issues são relações **"blocked by"** nativas. Assim, o epic mostra o progresso sozinho, a filha mostra o parent no painel lateral e o vínculo pode ser conferido pela API. O texto também continua no corpo das issues, como resumo para quem lê: a seção "Itens" do epic tem a lista ordenada `- [ ] #N título — depende de #M | independente`, e cada filha diz `Parte de #E`.

`.claude/skills/dev-workflow/scripts/github_issues.py sync-epic` reconcilia esse texto com o GitHub:

```sh
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-epic 112 --dry-run   # mostra o que faria
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-epic 112             # cria os vínculos que faltam
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-epic --all           # todos os issues com o label epic
```

O comando:

- vincula cada item da seção "Itens" como sub-issue, na ordem da lista, e cria um "blocked by" para cada "depende de";
- é idempotente: não duplica vínculos que já existem;
- nunca troca um parent existente: uma issue tem no máximo um parent, e o conflito aparece como divergência;
- termina com erro quando há divergência: issue inexistente ou de outro repositório, item listado em dois epics, sub-issue nativo fora da lista ou epic sem o label;
- ignora as linhas de checklist sem número de issue, como os candidatos dos epics de ciclo 2, e mostra quantas são.

O `sync-epic` também acerta as caixas da seção "Itens". No dia a dia, quem faz isso é o workflow [epic-checklist](../.github/workflows/epic-checklist.yml): quando uma issue fecha (por merge de PR com `Closes` ou à mão, como concluída ou dispensada), ele marca `[x]` no item dela no epic pai nativo, e quando ela é reaberta, volta a `[ ]`. Não marque as caixas à mão. Cada execução reconcilia o epic inteiro com o estado atual das filhas e relê o corpo depois de gravar, então filhas do mesmo epic fechadas ao mesmo tempo convergem. Quando o epic também é sub-issue (como o #112 do #95), o fechamento dele atualiza o epic de cima. Para reconciliar à mão, sem criar vínculos:

```sh
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-checklist 112 --dry-run
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-checklist --all
```

Quando a última sub-issue nativa aberta de um epic fecha, o mesmo workflow fecha o epic (`close-epic`), com um comentário que lista cada filha e se foi concluída ou dispensada. O epic fecha como concluído se ao menos uma filha foi concluída, e como `not planned` se todas foram dispensadas. A fonte de verdade são as sub-issues nativas, que o GitHub usa no progresso, não a lista em "Itens". Nada é fechado quando:

- ainda há filha aberta, ou o epic não tem sub-issues;
- o pai não tem o label `epic` ou já está fechado;
- alguma sub-issue é de outro repositório, que o token do workflow não consegue verificar; feche à mão.

Um fechamento feito com o token do workflow não dispara outro evento `issues: closed`. Por isso, ao fechar um epic que também é sub-issue (como o #112 do #95), o mesmo run atualiza as caixas do epic de cima e verifica se ele também pode fechar. Reabrir uma filha não reabre o epic: ele pode ter sido fechado à mão por outro motivo, então reabra-o à mão se for o caso. A seção "Critérios de conclusão" do epic não é verificada; o comentário pede para reabrir o epic se algum critério faltar. Para conferir sem fechar:

```sh
python3 .claude/skills/dev-workflow/scripts/github_issues.py close-epic --parent-of 101 --dry-run
```

O workflow só existe depois do merge na `main`, porque eventos de issue usam a versão do workflow da branch padrão.

Nenhum dos dois comandos remove vínculos. Para desfazer um vínculo, use `gh issue edit <N> --remove-parent` ou `--remove-blocked-by <M>`. Os issue types do GitHub (Epic, Task) não são usados, porque só existem em repositórios de organização.

## Verificação do PR e fechamento

O check "Verificação do PR" recusa o PR sem issue vinculada com exatamente um label de tipo, com uma seção obrigatória (`Issues`, `Resumo`, `Versão`, `Testes executados`, `Documentação`) vazia ou só com o texto do modelo, com título fora de Conventional Commits ou com um `semver:*` sem o label `release`, sem `Motivo: ...` ou repetido. Ele avisa, sem recusar, sobre critério de aceite não marcado na issue fechada, seção do modelo faltando na issue e tipo do título diferente do tipo da issue. PRs do Dependabot e o PR de release ficam isentos. Um segundo job comenta no PR, num comentário único e atualizado, a versão que o merge publicaria.

Ao mesclar, o workflow "Evidências no merge" comenta em cada issue fechada pelo PR os commits, o PR, os testes executados, a documentação, os critérios pendentes e a situação da versão, uma única vez mesmo com reexecução. Mudança de escopo durante o trabalho atualiza o corpo da issue e é registrada com o modelo de comentário de mudança de escopo do skill.

## Versão e release

A versão do projeto segue o versionamento semântico e é decidida por ferramenta, nunca editada à mão ([referência](../.claude/skills/dev-workflow/references/versioning.md)). A fonte única é a última tag de versão alcançável; os arquivos declarados em `versioning.files` são projeções reescritas a cada release. Só o label `release` faz um merge publicar versão: PRs sem ele se acumulam e entram, todos, na análise do próximo que tiver o label. O nível é o maior entre os sinais (labels de tipo das issues, Conventional Commits, marcador de breaking, superfícies públicas, pisos de versão no código e a análise por agente registrada no PR), e um label `semver:*` com motivo sobrepõe o cálculo. Em 0.x, uma quebra sobe o minor, e 1.0.0 só sai por decisão explícita (`semver:major`).

O workflow de release roda depois do merge, nunca no PR, para dois PRs simultâneos não disputarem o mesmo número. Ele escreve a versão, atualiza o `CHANGELOG.md`, cria a tag anotada e a GitHub Release e marca os PRs e issues incluídos (comentário e milestone). É idempotente: reexecutar depois de concluir não faz nada, e depois de uma falha parcial conclui o que faltou. Há modo direto e modo por PR de release, pré-release (`--prerelease rc`), branch de manutenção `maint/X.Y` e reversão documentada.

## Mecanismos

| Camada | Arquivo | O que garante |
|---|---|---|
| Norma | `AGENTS.md` | Regras lidas por qualquer agente e pessoa |
| Procedimento | `.claude/skills/dev-workflow/` | Skill portátil: passo a passo do agente, modelos de issue, PR e fechamento, scripts e modelos de workflow ([mecanismos](../.claude/skills/dev-workflow/references/mechanisms.md)) |
| Configuração | `.github/dev-workflow.toml` | Labels de tipo, seções, branch padrão e regras de PR deste repositório; os scripts recusam chave inválida ([configuração](../.claude/skills/dev-workflow/references/configuration.md)) |
| Hook do Claude Code | `.claude/settings.json`, `.claude/skills/dev-workflow/scripts/claude_hook.mjs` | Ao aceitar um plano, instrui o agente a criar as issues antes de editar. Bloqueia `git commit` sem o hook `commit-msg` instalado ou com `--no-verify`, e `gh pr create` sem `Closes`/`Refs #N` |
| Git | `.pre-commit-config.yaml`, `.claude/skills/dev-workflow/scripts/github_issues.py commit-msg` | Recusa commit sem referência a issue. Na branch `<tipo>/<N>-<slug>`, acrescenta `Refs: #N` sozinho. Merges, reverts e fixups gerados pelo git passam |
| Branch | `.claude/skills/dev-workflow/scripts/github_issues.py start N` | Cria `<tipo>/<N>-<slug>` a partir da issue e de `origin/main` |
| Epics | `.claude/skills/dev-workflow/scripts/github_issues.py sync-epic` | Cria os sub-issues e os "blocked by" nativos a partir da seção "Itens" do epic e mostra as divergências |
| Caixas do epic | `.github/workflows/epic-checklist.yml`, `github_issues.py sync-checklist` | Ao fechar ou reabrir uma issue, marca ou desmarca o item dela na seção "Itens" do epic pai |
| Fechamento do epic | `.github/workflows/epic-checklist.yml`, `github_issues.py close-epic` | Ao fechar a última sub-issue aberta, fecha o epic com comentário-resumo e sobe para o epic de cima |
| CI | `.github/workflows/issue-link.yml` | O PR precisa citar uma issue existente deste repositório com exatamente um label de tipo. PRs do Dependabot ficam isentos |
| Locks do Dependabot | `.github/workflows/dependabot-locks.yml`, `tools/workflow/dependabot_locks.py` | Regenera com uv os locks dos PRs de pip do Dependabot e commita no branch do PR, com `Refs #140`. Ver [testes](testing.md#locks-dos-prs-do-dependabot) |
| GitHub | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md` | Formulários com as seções obrigatórias. Issue em branco desabilitada |

O bloqueio de `git commit` por falta do hook `commit-msg` só vale nas branches cujo `HEAD` já versiona `.github/dev-workflow.toml` ou o hook `issue-reference` em `.pre-commit-config.yaml`. Branches anteriores à adoção não são bloqueadas. Os hooks do Claude Code são lembretes fortes, não uma barreira de segurança: eles analisam o texto do comando. As barreiras finais são o hook `commit-msg` e o check do PR. Para que o check impeça o merge, marque "PR vinculado a issue classificada" como check obrigatório na proteção da branch `main`.

## Preparação de um checkout

```sh
uv tool install pre-commit
pre-commit install            # instala os hooks pre-commit e commit-msg
python3 .claude/skills/dev-workflow/scripts/github_issues.py sync-labels   # Windows: py -3 .claude/skills/dev-workflow/scripts/github_issues.py sync-labels
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor  # confere configuração, modelos, workflows, hook e labels
```

`sync-labels` é idempotente e só precisa rodar de novo quando a lista de labels de `.github/dev-workflow.toml` mudar. O skill tem testes próprios, em `unittest`, que viajam com ele: `py -3 -m unittest discover -s .claude/skills/dev-workflow/tests -t .claude/skills/dev-workflow`. Prefira `uv tool install` a `uvx pre-commit install`: o hook gravado em `.git/hooks` aponta para o Python que o instalou, e o ambiente efêmero do `uvx` pode ser removido do cache. Os hooks do Claude Code exigem `node` no `PATH`.

## Exceções

- Merges, reverts e fixups gerados pelo git não precisam de referência própria.
- PRs do Dependabot dispensam issue. Uma atualização de dependência feita à mão segue o fluxo com `chore`.
- Perguntas, leituras e investigações sem mudança de arquivo não precisam de issue. Se a investigação levar a uma correção, a issue é criada antes da correção, com o diagnóstico no contexto.
- Commits anteriores à adoção deste fluxo não são reescritos.
