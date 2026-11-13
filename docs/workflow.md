# Fluxo de trabalho: plano, issue, commit, versão e fechamento

Todo trabalho versionado neste repositório nasce de uma issue do GitHub e termina com ela fechada depois de aprovado, com os commits e as evidências ligados a ela. Toda versão é calculada e escrita pela automação, a partir do que se acumulou desde a última tag. A regra normativa está no [AGENTS.md](../AGENTS.md#rastreabilidade-plano-issue-commit-versão-e-fechamento).

O fluxo é o skill portátil `dev-workflow`, em [.claude/skills/dev-workflow/](../.claude/skills/dev-workflow/SKILL.md). Ele traz o procedimento, os scripts, os modelos e os workflows, e serve a projetos de qualquer tecnologia. Este guia resume o ciclo e registra o que é próprio deste repositório. Os detalhes ficam nas referências do skill:

- [O fluxo completo, passo a passo, e as decisões](../.claude/skills/dev-workflow/references/flow.md)
- [Versionamento: sinais, 0.x, análise, sobreposição, pré-release, hotfix, reversão](../.claude/skills/dev-workflow/references/versioning.md)
- [Configuração](../.claude/skills/dev-workflow/references/configuration.md) e [instalação](../.claude/skills/dev-workflow/references/installation.md)
- [Modelos de issue, PR e comentários](../.claude/skills/dev-workflow/references/templates.md)

Nos comandos abaixo, `S` é `.claude/skills/dev-workflow/scripts` e `python3` é `py -3` no Windows.

## Ciclo

1. **Planejar.** No modo de planejamento do Claude Code, o plano termina com a seção `Itens do GitHub`: para cada item, o label de tipo, o título, os critérios de aceite, o plano de testes e a documentação a atualizar. Revisar o plano é também revisar essa decomposição.
2. **Aceitar → issues.** Quando o plano é aceito, o agente cria as issues antes de qualquer edição: uma por entrega independente e revisável, cada uma detalhada pelo modelo do tipo. Com duas ou mais, cria também uma issue `epic` com a lista ordenada das filhas e as vincula a ele como sub-issues nativos, com as dependências como "blocked by" (veja [Epics](#epics-sub-issues-e-dependências)). Sem modo de planejamento, a issue é identificada ou criada antes da primeira edição.
3. **Desenvolver.** Branch `<tipo>/<N>-<slug>` a partir de `main` (`python3 S/github_issues.py start <N>` a cria pelo tipo e título da issue). Commits em Conventional Commits, com trailer `Refs #N`. Mudança de escopo atualiza a issue, com o comentário de mudança de escopo, ou abre outra.
4. **Revisar.** PR para `main` pelo [modelo](../.github/pull_request_template.md), com `Closes #N` (entrega completa) ou `Refs #N` (parcial), somente os testes executados de fato e, **se o merge deve publicar versão, o label `release`**; sem ele, as mudanças ficam acumuladas. O check "Verificação do PR" confere tudo e comenta a prévia da versão.
5. **Aprovar e fechar.** O merge do PR fecha as issues citadas com `Closes`, e cada issue citada recebe sozinha o comentário de evidências. Se o PR era `release`, o workflow "Versão e release" publica a versão. Sem PR, a issue só fecha com aprovação explícita do responsável, depois de um comentário com commits, testes executados, documentação atualizada e follow-ups. Quem implementa não fecha a própria issue por conta própria.

## Classificação

Cada issue tem exatamente um label de tipo. O tipo decide o prefixo de commit e de branch, o incremento de versão que a entrega pede e a seção do changelog. O CI recusa PR que cite issue sem tipo ou com dois tipos.

| Label | Quando usar | Prefixo de commit e branch | Versão |
|---|---|---|---|
| `bug` | Comportamento diverge do esperado ou documentado; regressão | `fix` | patch |
| `feature` | Capacidade nova | `feat` | minor |
| `enhancement` | Melhoria de algo existente | `feat` ou `perf` | minor |
| `documentation` | Só documentação | `docs` | — |
| `refactor` | Reestruturação sem mudança observável | `refactor` | — |
| `test` | Só testes, fixtures ou evidência | `test` | — |
| `ci` | Pipeline e checks | `ci` | — |
| `chore` | Dependências, ferramentas, configuração | `chore` ou `build` | — |

Labels adicionais: `security` para qualquer impacto em tenant, RLS, autorização, autenticação, auditoria, segredos, CSRF ou CAS; `epic` para a issue que acompanha um plano. Documentação e testes que acompanham uma feature ou um bug ficam na mesma issue, como parte da definição de pronto.

Labels de PR: `release` (o merge publica versão; sem ele, o PR fica acumulado), `breaking-change` (quebra de compatibilidade) e `semver:major`, `semver:minor`, `semver:patch` (sobreposição do nível pelo responsável).

## Conteúdo obrigatório de uma issue

Os formulários em [.github/ISSUE_TEMPLATE](../.github/ISSUE_TEMPLATE) e os modelos do skill usam as mesmas seções. Além das seções genéricas do skill, este repositório exige **"Segurança e contrato do AGENTS.md"** nas issues e nos PRs (configurado em `issues.sections` e `pr.required_sections`):

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

## Verificação do PR

O workflow [pr-check](../.github/workflows/pr-check.yml) roda a cada push, edição do corpo e troca de label, e reprova quando:

- o corpo não cita uma issue existente deste repositório com exatamente um label de tipo;
- falta uma seção do modelo, ou ela está só com o texto do modelo;
- há `semver:*` sem `release`, sem motivo, ou mais de um;
- o título não segue Conventional Commits;
- algum arquivo de versão diverge da versão corrente (edição manual).

Ele avisa, sem reprovar: critérios de aceite sem marcar na issue que o PR fecha, seções faltando na issue e tipo do título diferente do tipo da issue. As severidades estão em `[pr.severity]`. PRs do Dependabot e o PR de release ficam isentos.

O mesmo workflow comenta no PR a **prévia de versão**: o número que o merge publicaria, as mudanças acumuladas que entrariam e os sinais de cada uma. É um comentário único, atualizado a cada execução.

**Checks obrigatórios da `main` (#249).** A proteção da `main` é um ruleset do repositório (Settings → Rules → Rulesets), não um arquivo: exigir pull request, exigir os checks `CI` e `Verificação do PR` e bloquear force-push e exclusão. `CI` é o job agregador de `.github/workflows/ci.yml`: ele só passa quando lint, assets, locks, testes, cobertura, typecheck, `check --deploy` e build passaram, então o nome continua estável se a matriz de testes mudar. Os alertas e as atualizações de segurança do Dependabot e o relato privado de vulnerabilidades também são configuração do repositório; o canal de relato está em [SECURITY.md](../SECURITY.md). Para conferir o que está aplicado, com acesso de leitura:

```sh
gh api repos/alaorneto/skeleton/rules/branches/main --jq '.[].type'   # pull_request, required_status_checks, non_fast_forward, deletion
gh api repos/alaorneto/skeleton/vulnerability-alerts -i | head -1      # HTTP/2.0 204
gh api repos/alaorneto/skeleton --jq '.security_and_analysis.dependabot_security_updates.status'
gh api repos/alaorneto/skeleton/private-vulnerability-reporting --jq .enabled
```


## Fechamento com evidências

No merge, o workflow [pr-merged](../.github/workflows/pr-merged.yml) comenta em cada issue citada pelo PR: os commits, as seções "Testes executados" e "Documentação" do PR, os critérios de aceite que ficaram sem marcar e a situação da versão. Reexecutar não duplica o comentário. PR sem `release` não recebe marcação: o comentário na issue diz que a entrega fica acumulada para a próxima versão.

## Versão

### De onde vem o número

A versão do projeto é a versão do core (`django-skeleton`). A fonte é a **última tag `vX.Y.Z` alcançável**; enquanto não há tag, vale `versioning.initial_version` (0.5.0). Os arquivos abaixo repetem o número e são escritos pelo workflow de release, nunca à mão:

| Arquivo | O que muda |
|---|---|
| `pyproject.toml` | `version` do core |
| `deployment_agent/pyproject.toml` | `version` do agente e o pin `django-skeleton==X.Y.Z`: o agente anda no mesmo número do core |
| `skeleton/contrib/calendar/manifest.json` | `version` (uma app first-party tem de ter a versão do core; o startup recusa outro número) e a faixa `compatibility.skeleton`, presa ao minor corrente |
| `skeleton/contrib/calendar/docs/service-api.md` | A faixa recomendada para quem consome a agenda |
| `CHANGELOG.md` | Uma seção por versão, agrupada por tipo, com links para issues e PRs |

Uma app first-party nova em `skeleton/contrib` acrescenta as entradas dela em `[[versioning.files]]` de [.github/dev-workflow.toml](../.github/dev-workflow.toml), no mesmo PR.

### Versão do projeto e pisos de core

São duas coisas diferentes:

- **Versão do core**: o número acima. Sobe a cada release.
- **Piso de funcionalidade**: "disponível desde a versão X". São as constantes `*_CORE_MINIMUM` e `*_UPGRADE_CORE`, o `since="X"` dos métodos da API de serviços e o `compatibility.skeleton` dos manifestos de apps de terceiros (`>=0.4.0,<1.0.0`). **Não acompanham o bump**: `>=0.4.0` continua verdadeiro em 0.7.0.

Ao criar uma capacidade com piso, escreva nele a versão em que ela será publicada. Uma capacidade nova é uma `feature`, então é o próximo minor: `python3 S/release.py next --level minor`. A automação confere o inverso: se o código declara um piso maior que a versão que sairia (por exemplo `since="0.6.0"` numa release calculada como 0.5.1), o nível sobe até alcançá-lo, e isso fica registrado nas notas; se nem assim alcança, a release para.

Apps de terceiros (`applications/`, `examples/notes`) têm versão própria, no manifesto delas, cuidada por `package build`. Não entram no versionamento do projeto.

### Como o nível é decidido

O nível é o maior entre os sinais de **todas as mudanças acumuladas desde a última tag**, inclusive os PRs que entraram sem `release`:

- o tipo de cada issue citada (tabela acima);
- o tipo de cada commit e do título do PR (`feat` minor; `fix` e `perf` patch);
- `!` no tipo, rodapé `BREAKING CHANGE:` ou label `breaking-change`: quebra de compatibilidade;
- arquivos das superfícies públicas declaradas (API de serviços e kit de testes, bases de modelo, contexto e autorização, manifesto e pacotes de apps, API HTTP, migrations do núcleo): removido pede major; migration alterada pede major; o resto fica marcado "exige análise";
- a análise do diff acumulado, feita pelo agente ao abrir o PR `release` e gravada no PR. Ela só eleva o nível;
- os pisos de funcionalidade.

**0.x.** Enquanto o core está em `0.y.z`, quebra de compatibilidade sobe o minor (0.5.x → 0.6.0), que é o que a faixa `>=0.5.0,<0.6.0` das apps first-party espera. A entrada aparece em "Mudanças incompatíveis" do mesmo jeito. `1.0.0` só sai por decisão explícita: `semver:major` no PR de release, com o motivo.

**Sobreposição.** O responsável tem a palavra final com `semver:major|minor|patch` no PR de release e a linha `Motivo da sobreposição: ...` na seção "Versão". O nível calculado, o aplicado e o motivo ficam nas notas da release.

### O que o workflow de release faz

[release.yml](../.github/workflows/release.yml) roda a cada push na `main` e em `release/**`. Sem PR `release` desde a última tag, termina dizendo quantas mudanças estão acumuladas. Com um, calcula o número, escreve os arquivos e o changelog, commita `chore(release): vX.Y.Z` na branch, cria a tag anotada e a GitHub Release, comenta a versão em cada PR e issue incluídos e os reúne na milestone `vX.Y.Z`. Rodar de novo depois de concluir não faz nada; rodar depois de uma falha conclui o que faltou.

```sh
python3 S/release.py status                 # o que está acumulado e o número que sairia
python3 S/release.py plan --pr 123          # idem, contando um PR aberto como mesclado
python3 S/release.py check                  # arquivos coerentes com a versão corrente?
gh workflow run release.yml -f force=true   # publica o acumulado sem esperar um PR release
gh workflow run release.yml -f dry_run=true -f force=true
```

Pré-release (`-f prerelease=rc`), hotfix numa versão antiga (branch `release/X.Y`) e reversão de uma release errada estão na [referência de versionamento](../.claude/skills/dev-workflow/references/versioning.md). Uma ressalva deste repositório para pré-releases: a faixa `>=0.6.0,<0.7.0` da agenda não aceita o core `0.6.0rc1`; antes da primeira pré-release, ajuste a entrada da faixa em `[[versioning.files]]`.

## Epics, sub-issues e dependências

O vínculo oficial entre um epic e suas filhas é o relacionamento nativo de **sub-issue** do GitHub, e as dependências entre issues são relações **"blocked by"** nativas. Assim, o epic mostra o progresso sozinho, a filha mostra o parent no painel lateral e o vínculo pode ser conferido pela API. O texto também continua no corpo das issues, como resumo para quem lê: a seção "Itens" do epic tem a lista ordenada `- [ ] #N título — depende de #M | independente`, e cada filha diz `Parte de #E`.

`github_issues.py sync-epic` reconcilia esse texto com o GitHub:

```sh
python3 S/github_issues.py sync-epic 112 --dry-run   # mostra o que faria
python3 S/github_issues.py sync-epic 112             # cria os vínculos que faltam
python3 S/github_issues.py sync-epic --all           # todos os issues com o label epic
```

O comando:

- vincula cada item da seção "Itens" como sub-issue, na ordem da lista, e cria um "blocked by" para cada "depende de";
- é idempotente: não duplica vínculos que já existem;
- nunca troca um parent existente: uma issue tem no máximo um parent, e o conflito aparece como divergência;
- termina com erro quando há divergência: issue inexistente ou de outro repositório, item listado em dois epics, sub-issue nativo fora da lista ou epic sem o label;
- ignora as linhas de checklist sem número de issue, como os candidatos dos epics de ciclo 2, e mostra quantas são.

O `sync-epic` também acerta as caixas da seção "Itens". No dia a dia, quem faz isso é o workflow [epic-checklist](../.github/workflows/epic-checklist.yml): quando uma issue fecha (por merge de PR com `Closes` ou à mão, como concluída ou dispensada), ele marca `[x]` no item dela no epic pai nativo, e quando ela é reaberta, volta a `[ ]`. Não marque as caixas à mão. Cada execução reconcilia o epic inteiro com o estado atual das filhas e relê o corpo depois de gravar, então filhas do mesmo epic fechadas ao mesmo tempo convergem. Quando o epic também é sub-issue (como o #112 do #95), o fechamento dele atualiza o epic de cima. Para reconciliar à mão, sem criar vínculos:

```sh
python3 S/github_issues.py sync-checklist 112 --dry-run
python3 S/github_issues.py sync-checklist --all
```

Quando a última sub-issue nativa aberta de um epic fecha, o mesmo workflow fecha o epic (`close-epic`), com um comentário que lista cada filha e se foi concluída ou dispensada. O epic fecha como concluído se ao menos uma filha foi concluída, e como `not planned` se todas foram dispensadas. A fonte de verdade são as sub-issues nativas, que o GitHub usa no progresso, não a lista em "Itens". Nada é fechado quando:

- ainda há filha aberta, ou o epic não tem sub-issues;
- o pai não tem o label `epic` ou já está fechado;
- alguma sub-issue é de outro repositório, que o token do workflow não consegue verificar; feche à mão.
- o epic tem mais de 100 sub-issues ou bloqueios: a leitura viria incompleta, então `close-epic`, `sync-epic` e `sync-checklist` falham com erro (#331); feche à mão.

Um fechamento feito com o token do workflow não dispara outro evento `issues: closed`. Por isso, ao fechar um epic que também é sub-issue (como o #112 do #95), o mesmo run atualiza as caixas do epic de cima e verifica se ele também pode fechar. Reabrir uma filha não reabre o epic: ele pode ter sido fechado à mão por outro motivo, então reabra-o à mão se for o caso. A seção "Critérios de conclusão" do epic não é verificada; o comentário pede para reabrir o epic se algum critério faltar. Para conferir sem fechar:

```sh
python3 S/github_issues.py close-epic --parent-of 101 --dry-run
```

Nenhum dos comandos remove vínculos. Para desfazer um vínculo, use `gh issue edit <N> --remove-parent` ou `--remove-blocked-by <M>`. Os issue types do GitHub (Epic, Task) não são usados, porque só existem em repositórios de organização.

## Verificação do PR e fechamento

O check "Verificação do PR" recusa o PR sem issue vinculada com exatamente um label de tipo, com uma seção obrigatória (`Issues`, `Resumo`, `Versão`, `Testes executados`, `Documentação`) vazia ou só com o texto do modelo, com título fora de Conventional Commits ou com um `semver:*` sem o label `release`, sem `Motivo: ...` ou repetido. Ele avisa, sem recusar, sobre critério de aceite não marcado na issue fechada, seção do modelo faltando na issue e tipo do título diferente do tipo da issue. PRs do Dependabot e o PR de release ficam isentos. O check roda o script e a configuração da branch base, não os do PR: um PR que altera o validador é conferido pela versão anterior, e a nova vale depois do merge (#331). Um segundo job comenta no PR, num comentário único e atualizado, a versão que o merge publicaria.

**Checks obrigatórios da `main` (#249).** A proteção da `main` é um ruleset do repositório (Settings → Rules → Rulesets), não um arquivo: exigir pull request, exigir os checks `CI` e `Verificação do PR` e bloquear force-push e exclusão. `CI` é o job agregador de `.github/workflows/ci.yml`: ele só passa quando lint, assets, locks, testes, cobertura, typecheck, `check --deploy` e build passaram, então o nome continua estável se a matriz de testes mudar. Os alertas e as atualizações de segurança do Dependabot e o relato privado de vulnerabilidades também são configuração do repositório; o canal de relato está em [SECURITY.md](../SECURITY.md). Para conferir o que está aplicado, com acesso de leitura:

```sh
gh api repos/alaorneto/skeleton/rules/branches/main --jq '.[].type'   # pull_request, required_status_checks, non_fast_forward, deletion
gh api repos/alaorneto/skeleton/vulnerability-alerts -i | head -1      # HTTP/2.0 204
gh api repos/alaorneto/skeleton --jq '.security_and_analysis.dependabot_security_updates.status'
gh api repos/alaorneto/skeleton/private-vulnerability-reporting --jq .enabled
```

Ao mesclar, o workflow "Evidências no merge" comenta em cada issue fechada pelo PR os commits, o PR, os testes executados, a documentação, os critérios pendentes e a situação da versão, uma única vez mesmo com reexecução. Mudança de escopo durante o trabalho atualiza o corpo da issue e é registrada com o modelo de comentário de mudança de escopo do skill.

## Versão e release

A versão do projeto segue o versionamento semântico e é decidida por ferramenta, nunca editada à mão ([referência](../.claude/skills/dev-workflow/references/versioning.md)). A fonte única é a última tag de versão alcançável; os arquivos declarados em `versioning.files` são projeções reescritas a cada release. Só o label `release` faz um merge publicar versão: PRs sem ele se acumulam e entram, todos, na análise do próximo que tiver o label. O nível é o maior entre os sinais (labels de tipo das issues, Conventional Commits, marcador de breaking, superfícies públicas, pisos de versão no código e a análise por agente registrada no PR), e um label `semver:*` com motivo sobrepõe o cálculo. Em 0.x, uma quebra sobe o minor, e 1.0.0 só sai por decisão explícita (`semver:major`).

Neste repositório, a versão do projeto é a do core (`django-skeleton`): o agente de deployment (`deployment_agent/pyproject.toml`, inclusive o pino `django-skeleton==`) e as apps first-party (manifesto e faixa recomendada da agenda) seguem o mesmo número, todos declarados em `.github/dev-workflow.toml`. Os pisos de core (`SERVICE_API_CORE_MINIMUM`, `MANIFEST_V2_CORE_MINIMUM`, `FIELD_DIMENSIONS_CORE_MINIMUM`, a faixa do manifesto do exemplo e os `since=` dos serviços) são pisos de funcionalidade: não acompanham o bump, e `release.py check` só confere que nenhum é maior que a versão publicada. Um piso novo acima da versão corrente é sinal de que a próxima versão precisa alcançá-lo.

O workflow de release roda depois do merge, nunca no PR, para dois PRs simultâneos não disputarem o mesmo número. Ele escreve a versão, atualiza o `CHANGELOG.md`, cria a tag anotada e a GitHub Release e marca os PRs e issues incluídos (comentário e milestone). É idempotente: reexecutar depois de concluir não faz nada, e depois de uma falha parcial conclui o que faltou. Há modo direto e modo por PR de release, pré-release (`--prerelease rc`), branch de manutenção `maint/X.Y` e reversão documentada.

## Mecanismos

| Camada | Arquivo | O que garante |
|---|---|---|
| Norma | `AGENTS.md` | Regras lidas por qualquer agente e pessoa |
| Procedimento | `.claude/skills/dev-workflow/` | Passo a passo do agente, scripts, modelos e referências |
| Configuração | `.github/dev-workflow.toml` | Tipos, seções obrigatórias, arquivos de versão, superfícies públicas, pisos, modo de release |
| Hook do Claude Code | `.claude/settings.json`, `.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs` | Ao aceitar um plano, instrui o agente a criar as issues antes de editar. Bloqueia `git commit` sem o hook `commit-msg` instalado ou com `--no-verify`, e `gh pr create` sem `Closes`/`Refs #N` |
| Git | `.pre-commit-config.yaml`, `github_issues.py commit-msg` | Recusa commit sem referência a issue. Na branch `<tipo>/<N>-<slug>`, acrescenta `Refs: #N` sozinho. Merges, reverts, fixups e o commit de release passam |
| Verificação do PR | `.github/workflows/pr-check.yml`, `github_issues.py check-pr`, `release.py check` e `preview` | Issue classificada, seções, sobreposição de versão, título, arquivos de versão coerentes; prévia de versão comentada |
| Evidências | `.github/workflows/pr-merged.yml`, `github_issues.py pr-merged` | Comentário de evidências em cada issue do PR |
| Release | `.github/workflows/release.yml`, `release.py run` | Número, arquivos, changelog, tag, GitHub Release e marcações |
| Análise de versão | o agente, pelo skill; opcionalmente `.github/workflows/semver-analysis.yml` | Nível proposto a partir do diff acumulado, gravado no PR |
| Epics | `.github/workflows/epic-checklist.yml`, `github_issues.py sync-epic`, `sync-checklist`, `close-epic` | Sub-issues e "blocked by" nativos, caixas e fechamento do epic |
| Autoteste | `.github/workflows/dev-workflow-selftest.yml` | A suíte do skill em Linux, Windows e macOS quando ele ou a configuração mudam |
| Locks do Dependabot | `.github/workflows/dependabot-locks.yml`, `tools/workflow/dependabot_locks.py` | Regenera com uv os locks dos PRs de pip do Dependabot e commita no branch do PR, com `Refs #140`. Ver [testes](testing.md#locks-dos-prs-do-dependabot) |
| GitHub | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md` | Formulários com as seções obrigatórias. Issue em branco desabilitada |

O bloqueio de `git commit` por falta do hook `commit-msg` só vale nas branches cujo `HEAD` já versiona `.github/dev-workflow.toml`. Os hooks do Claude Code são lembretes fortes, não uma barreira de segurança: eles analisam o texto do comando. As barreiras finais são o hook `commit-msg` e o check do PR. Workflows de eventos de issue e o `pull_request_target` usam a versão da branch padrão, então só valem depois do merge na `main`.

## Preparação de um checkout

```sh
uv tool install pre-commit
pre-commit install            # instala os hooks pre-commit e commit-msg
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor
```

`doctor` confere configuração, workflows, hook, labels, arquivos de versão e a tag de base, e lista o que falta. `python3 S/github_issues.py sync-labels` recria os labels; é idempotente. Prefira `uv tool install` a `uvx pre-commit install`: o hook gravado em `.git/hooks` aponta para o Python que o instalou, e o ambiente efêmero do `uvx` pode ser removido do cache. Os hooks do Claude Code exigem `node` no `PATH`.

A suíte do skill não depende do Django nem do banco:

```sh
python3 -m unittest discover -s .claude/skills/dev-workflow/tests
```

## Passos únicos do responsável

1. **Tag de base**, depois que este fluxo estiver na `main`: `python3 S/release.py baseline --push` cria `v0.5.0` na ponta da `main`. Ela declara que tudo até ali é a 0.5.0; a primeira release automática cobre o que vier depois.
2. **Proteção da `main`** (opcional, recomendado): "Verificação do PR" como check obrigatório. O modo de release configurado é `direct`, em que o workflow faz push na `main`; se a proteção passar a exigir PR, troque `release.mode` para `pull-request` e marque "Allow GitHub Actions to create and approve pull requests", ou crie o segredo `RELEASE_TOKEN`.
3. **Segredos opcionais**: `ANTHROPIC_API_KEY` ou `CLAUDE_CODE_OAUTH_TOKEN` ligam a análise de versão no CI; sem eles o agente a faz localmente ao abrir o PR, e a release sai mesmo sem análise. `RELEASE_TOKEN` só é preciso para branch protegida ou para a tag disparar outros workflows.

## Exceções

- Merges, reverts e fixups gerados pelo git, e o commit de release, não precisam de referência própria.
- PRs do Dependabot dispensam issue; sem `release`, entram como mudança acumulada. Uma atualização de dependência feita à mão segue o fluxo com `chore`.
- Perguntas, leituras e investigações sem mudança de arquivo não precisam de issue. Se a investigação levar a uma correção, a issue é criada antes da correção, com o diagnóstico no contexto.
- Commits anteriores à adoção deste fluxo não são reescritos, e o que veio antes da tag de base não entra em changelog.
