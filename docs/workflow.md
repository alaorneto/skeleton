# Fluxo de trabalho: plano, issue, commit, versão e fechamento

Todo trabalho versionado neste repositório nasce de uma issue do GitHub e termina com ela fechada depois de aprovado, com os commits e as evidências ligados a ela. Toda versão é calculada e escrita pela automação, a partir do que se acumulou desde a última tag.

O procedimento é o skill `dev-workflow`, em [.claude/skills/dev-workflow/](../.claude/skills/dev-workflow/SKILL.md). Quando ele é obrigatório está no [AGENTS.md](../AGENTS.md#rastreabilidade-plano-issue-commit-versão-e-fechamento): modo de planejamento, plano aceito, começo de qualquer mudança, issue, branch, commit, PR, versão, release e fechamento. No Claude Code, carregue-o pela ferramenta Skill; os hooks do projeto o lembram nesses momentos. Outro agente, ou uma pessoa, lê o `SKILL.md` e as referências.

Este guia registra só o que é próprio deste repositório. O resto está no skill, e não é repetido aqui:

| Tema | Onde |
|---|---|
| Ciclo completo, o que acontece num PR com e sem versão, decisões de desenho | [flow.md](../.claude/skills/dev-workflow/references/flow.md) |
| Tipos de issue e o que cada um decide (prefixo, versão, changelog) | [SKILL.md, fase 1](../.claude/skills/dev-workflow/SKILL.md#1-classificação-da-issue) e `[types]` de [.github/dev-workflow.toml](../.github/dev-workflow.toml) |
| Modelos de issue, PR, fechamento e mudança de escopo | [templates.md](../.claude/skills/dev-workflow/references/templates.md) |
| Verificação do PR, prévia de versão e evidências no merge | [flow.md](../.claude/skills/dev-workflow/references/flow.md#o-que-acontece-num-pr-sem-versão) |
| Epics, sub-issues, "blocked by" e fechamento automático | [flow.md](../.claude/skills/dev-workflow/references/flow.md#epics-sub-issues-dependências-e-fechamento) |
| Versão: sinais, 0.x, análise do diff, sobreposição, pré-release, hotfix, reversão | [versioning.md](../.claude/skills/dev-workflow/references/versioning.md) |
| Configuração | [configuration.md](../.claude/skills/dev-workflow/references/configuration.md) |
| Instalação, atualização do skill, hooks do Claude Code, passos do dono | [installation.md](../.claude/skills/dev-workflow/references/installation.md) |

Nos comandos abaixo, `S` é `.claude/skills/dev-workflow/scripts` e `python3` é `py -3` no Windows.

## O que este repositório acrescenta

### Issues e PRs

- Seção extra **"Segurança e contrato do AGENTS.md"** nas issues e nos PRs (`issues.sections` e `pr.required_sections` da configuração; os formulários em [.github/ISSUE_TEMPLATE](../.github/ISSUE_TEMPLATE) e o [modelo de PR](../.github/pull_request_template.md) já a têm).
- Label adicional `security` para qualquer impacto em tenant, RLS, autorização, autenticação, auditoria, segredos, CSRF ou CAS.
- O plano de testes cita integração em PostgreSQL com a role runtime e os negativos de segurança quando pertinentes, e a documentação a atualizar inclui a evidência em [testing](testing.md). Bugs incluem um teste de regressão.

### Checks obrigatórios da `main` (#249)

A proteção da `main` é um ruleset do repositório (Settings → Rules → Rulesets), não um arquivo: exigir pull request, exigir os checks `CI` e `Verificação do PR` e bloquear force-push e exclusão. `CI` é o job agregador de `.github/workflows/ci.yml`: ele só passa quando lint, assets, locks, testes, cobertura, typecheck, `check --deploy` e build passaram, então o nome continua estável se a matriz de testes mudar. Os alertas e as atualizações de segurança do Dependabot e o relato privado de vulnerabilidades também são configuração do repositório; o canal de relato está em [SECURITY.md](../SECURITY.md). Para conferir o que está aplicado, com acesso de leitura:

```sh
gh api repos/alaorneto/skeleton/rules/branches/main --jq '.[].type'   # pull_request, required_status_checks, non_fast_forward, deletion
gh api repos/alaorneto/skeleton/vulnerability-alerts -i | head -1      # HTTP/2.0 204
gh api repos/alaorneto/skeleton --jq '.security_and_analysis.dependabot_security_updates.status'
gh api repos/alaorneto/skeleton/private-vulnerability-reporting --jq .enabled
```

"Verificação do PR" roda o validador e a configuração da branch base, não os do PR (#331): um PR que altera o skill é conferido pela versão anterior, e a nova vale depois do merge. Os testes [test_workflow_files.py](../tests/unit/test_workflow_files.py) e [test_workflow_pins.py](../tests/unit/test_workflow_pins.py) conferem isso, que os workflows instalados são iguais aos modelos do skill e que toda action está fixada por SHA.

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

Ao criar uma capacidade com piso, escreva nele a versão em que ela será publicada. Uma capacidade nova é uma `feature`, então é o próximo minor: `python3 S/release.py next --level minor`. A automação confere o inverso: se o código declara um piso maior que a versão que sairia, o nível sobe até alcançá-lo, e isso fica nas notas; se nem assim alcança, a release para.

Apps de terceiros (`applications/`, `examples/notes`) têm versão própria, no manifesto delas, cuidada por `package build`. Não entram no versionamento do projeto.

### Superfícies públicas e 0.x

As superfícies públicas declaradas em `[[versioning.public_surfaces]]` são a API de serviços e o kit de testes, as bases de modelo, contexto e autorização, o manifesto e os pacotes de apps, a API HTTP e as migrations do núcleo. Enquanto o core está em `0.y.z`, quebra de compatibilidade sobe o minor (0.5.x → 0.6.0), que é o que a faixa `>=0.5.0,<0.6.0` das apps first-party espera; `1.0.0` só sai com `semver:major` no PR de release, com o motivo.

Ressalva para pré-releases: a faixa `>=0.6.0,<0.7.0` da agenda não aceita o core `0.6.0rc1`. Antes da primeira pré-release, ajuste a entrada da faixa em `[[versioning.files]]`.

## O skill neste repositório

A cópia em `.claude/skills/dev-workflow/` vem do repositório próprio do skill; `python3 S/bootstrap.py version` diz qual versão está instalada. Para atualizar, numa issue `chore`, rode o `update` **a partir da versão nova** e revise o diff ([installation.md](../.claude/skills/dev-workflow/references/installation.md#atualizar-o-skill)):

```sh
python3 <clone-do-skill>/scripts/bootstrap.py update --dry-run
python3 <clone-do-skill>/scripts/bootstrap.py update
python3 S/bootstrap.py doctor
```

Correção no skill feita aqui (urgente) volta para o repositório do skill num PR de lá; a cópia não diverge por muito tempo, porque o próximo `update` a sobrescreve.

| Peça própria deste repositório | Arquivo |
|---|---|
| Configuração | [.github/dev-workflow.toml](../.github/dev-workflow.toml) |
| Hooks do Claude Code | [.claude/settings.json](../.claude/settings.json): modo de planejamento, pedidos que falam do fluxo, plano aceito, `git commit` e `gh pr create` |
| Hook `commit-msg` | [.pre-commit-config.yaml](../.pre-commit-config.yaml), que chama `github_issues.py commit-msg` do skill |
| Locks do Dependabot | `.github/workflows/dependabot-locks.yml` e `tools/workflow/dependabot_locks.py`: regeneram com uv os locks dos PRs de pip do Dependabot e commitam no branch do PR, com `Refs #140`. Ver [testes](testing.md#locks-dos-prs-do-dependabot) |

## Preparação de um checkout

```sh
uv tool install pre-commit
pre-commit install            # instala os hooks pre-commit e commit-msg
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor
```

Prefira `uv tool install` a `uvx pre-commit install`: o hook gravado em `.git/hooks` aponta para o Python que o instalou, e o ambiente efêmero do `uvx` pode ser removido do cache. Os hooks do Claude Code exigem `node` no `PATH`. A suíte do skill não depende do Django nem do banco: `python3 -m unittest discover -s .claude/skills/dev-workflow/tests`.

## Passos únicos do responsável

1. **Tag de base**, depois que este fluxo estiver na `main`: `python3 S/release.py baseline --push` cria `v0.5.0` na ponta da `main`. Ela declara que tudo até ali é a 0.5.0; a primeira release automática cobre o que vier depois.
2. **Ruleset da `main`** (#249, acima). O modo de release configurado é `direct`, em que o workflow faz push do commit de release na `main`; com o ruleset exigindo PR, esse push é recusado. Escolha um: o segredo `RELEASE_TOKEN` com um token de quem está na lista de bypass do ruleset, ou `release.mode = "pull-request"` com "Allow GitHub Actions to create and approve pull requests" marcado (o PR de release aberto pelo token do Actions não dispara os checks obrigatórios; com `RELEASE_TOKEN` eles rodam).
3. **Segredos opcionais**: `ANTHROPIC_API_KEY` ou `CLAUDE_CODE_OAUTH_TOKEN` ligam a análise de versão no CI; sem eles o agente a faz localmente ao abrir o PR `release`, e a release sai mesmo sem análise.

## Exceções

- Merges, reverts e fixups gerados pelo git, e o commit de release, não precisam de referência própria.
- PRs do Dependabot dispensam issue; sem `release`, entram como mudança acumulada. Uma atualização de dependência feita à mão segue o fluxo com `chore`.
- Commits anteriores à adoção deste fluxo não são reescritos, e o que veio antes da tag de base não entra em changelog.
