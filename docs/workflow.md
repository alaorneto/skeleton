# Fluxo de trabalho: plano, issue, commit, versão e fechamento

Todo trabalho versionado neste repositório parte de uma issue do GitHub e termina com seu fechamento após aprovação, mantendo commits e evidências vinculados a ela. Toda versão é calculada e escrita automaticamente com base no histórico acumulado desde a última tag.

O procedimento operacional está definido no skill `dev-workflow`, em [.claude/skills/dev-workflow/](../.claude/skills/dev-workflow/SKILL.md). Os momentos em que ele é obrigatório constam no [AGENTS.md](../AGENTS.md#rastreabilidade-plano-issue-commit-versão-e-fechamento): planejamento, aprovação de plano, início de qualquer alteração, gestão de issues, criação de branches, commits, pull requests, versionamento, release e fechamento. No Claude Code, carregue o skill pela ferramenta Skill (os hooks do projeto lembram de fazê-lo nesses pontos); outros agentes ou desenvolvedores devem consultar diretamente o `SKILL.md` e suas referências.

Este guia documenta apenas o que é específico deste repositório; os procedimentos gerais estão no skill:

| Tema | Onde consultar |
|---|---|
| Ciclo completo, o que ocorre em PRs com e sem versão e decisões de desenho | [flow.md](../.claude/skills/dev-workflow/references/flow.md) |
| Tipos de issue e seus efeitos (prefixo, versão e changelog) | [SKILL.md, fase 1](../.claude/skills/dev-workflow/SKILL.md#1-classificação-da-issue) e `[types]` de [.github/dev-workflow.toml](../.github/dev-workflow.toml) |
| Modelos de issue, PR, fechamento e mudança de escopo | [templates.md](../.claude/skills/dev-workflow/references/templates.md) |
| Verificação do PR, prévia de versão e evidências no merge | [flow.md](../.claude/skills/dev-workflow/references/flow.md#o-que-acontece-num-pr-sem-versão) |
| Epics, sub-issues, dependências ("blocked by") e fechamento automático | [flow.md](../.claude/skills/dev-workflow/references/flow.md#epics-sub-issues-dependências-e-fechamento) |
| Versionamento: sinais, 0.x, análise de diff, sobreposição, pré-release, hotfix e reversão | [versioning.md](../.claude/skills/dev-workflow/references/versioning.md) |
| Configuração | [configuration.md](../.claude/skills/dev-workflow/references/configuration.md) |
| Instalação, atualização do skill, hooks do Claude Code e passos do mantenedor | [installation.md](../.claude/skills/dev-workflow/references/installation.md) |

Nos comandos abaixo, `S` representa `.claude/skills/dev-workflow/scripts`, e `python3` corresponde a `py -3` no Windows.

## O que este repositório acrescenta

### Issues e PRs

- **Seção obrigatória extra:** issues e PRs incluem a seção **"Segurança e contrato do AGENTS.md"** (`issues.sections` e `pr.required_sections` na configuração; os formulários em [.github/ISSUE_TEMPLATE](../.github/ISSUE_TEMPLATE) e o [modelo de PR](../.github/pull_request_template.md) já a trazem).
- **Label `security`:** aplicado a qualquer alteração com impacto em tenant, RLS, autorização, autenticação, auditoria, segredos, CSRF ou CAS.
- **Evidências e testes:** o plano de testes cita integração em PostgreSQL com a role runtime e cenários negativos de segurança quando pertinentes, e a atualização de documentação inclui a evidência em [testing](testing.md). Correções de bug exigem teste de regressão.

### Checks obrigatórios da `main` (#249)

A proteção da branch `main` é configurada como um ruleset do repositório (Settings → Rules → Rulesets), não como arquivo versionado: exige pull request, exige os checks `CI` e `Verificação do PR` e bloqueia force-push e exclusão da branch. `CI` é o job agregador definido em `.github/workflows/ci.yml` — ele só passa quando lint, assets, locks, testes, cobertura, typecheck, `check --deploy` e build são aprovados, mantendo um nome estável mesmo que a matriz de testes mude.

Alertas e atualizações de segurança do Dependabot, assim como o relato privado de vulnerabilidades, também são configurados no repositório; o canal de relato está descrito em [SECURITY.md](../SECURITY.md). Com acesso de leitura, é possível verificar a configuração aplicada:

```sh
gh api repos/alaorneto/skeleton/rules/branches/main --jq '.[].type'   # pull_request, required_status_checks, non_fast_forward, deletion
gh api repos/alaorneto/skeleton/vulnerability-alerts -i | head -1      # HTTP/2.0 204
gh api repos/alaorneto/skeleton --jq '.security_and_analysis.dependabot_security_updates.status'
gh api repos/alaorneto/skeleton/private-vulnerability-reporting --jq .enabled
```

O check "Verificação do PR" executa o validador e a configuração da branch base, não os do próprio PR: assim, um PR que atualiza o skill é validado pela versão anterior, e a nova entra em vigor após o merge. Os testes [test_workflow_files.py](../tests/unit/test_workflow_files.py) e [test_workflow_pins.py](../tests/unit/test_workflow_pins.py) garantem essa regra, verificam que os workflows instalados coincidem com os modelos do skill e asseguram que toda GitHub Action esteja fixada por SHA.

## Versão

### De onde vem o número

A versão do projeto é a versão do core (`django-skeleton`), determinada a partir da **última tag `vX.Y.Z` alcançável**. Antes de existir qualquer tag, vale `versioning.initial_version` (`0.0.0`, já que nada foi publicado antes da primeira release `0.1.0`). Os arquivos abaixo repetem esse número e são atualizados exclusivamente pelo workflow de release, nunca manualmente:

| Arquivo | O que muda |
|---|---|
| `pyproject.toml` | `version` do core |
| `deployment_agent/pyproject.toml` | `version` do agente e o pin `django-skeleton==X.Y.Z`, mantendo o agente na mesma versão do core |
| `skeleton/contrib/calendar/manifest.json`, `skeleton/contrib/ledger/manifest.json` | `version` (apps first-party precisam acompanhar exatamente a versão do core; o startup recusa divergências) e a faixa `compatibility.skeleton`, presa ao minor corrente |
| `skeleton/contrib/calendar/docs/service-api.md`, `skeleton/contrib/ledger/docs/service-api.md` | Faixa recomendada para quem consome a agenda e o livro contábil |
| `docs/deployment.md`, `docs/extending.md`, `docs/service-api.md` | Exemplos de dependência de apps first-party (`"org.skeleton.calendar": ">=X.Y.0,<X.Y+1.0"`), presos ao minor corrente |
| `CHANGELOG.md` | Nova seção por versão, agrupada por tipo e com links para issues e PRs |

Toda nova app first-party adicionada em `skeleton/contrib` deve incluir suas entradas em `[[versioning.files]]` de [.github/dev-workflow.toml](../.github/dev-workflow.toml) no mesmo PR.

### Versão do projeto e pisos de core

Convém distinguir dois conceitos:

- **Versão do core:** o número publicado a cada release, atualizado automaticamente.
- **Piso de funcionalidade:** indica "disponível desde a versão X", usado nas constantes `*_CORE_MINIMUM` e `*_UPGRADE_CORE`, no atributo `since="X"` dos métodos da API de serviços e em `compatibility.skeleton` de apps de terceiros (`>=0.1.0,<1.0.0`). Pisos **não acompanham o bump**: `>=0.1.0` continua válido na `0.7.0`. Tudo o que já existia antes da primeira release nasce com `since="0.1.0"`, e os pisos de campos de manifesto ficam em `0.0.0`, pois todo núcleo publicado os reconhece.

Ao introduzir uma nova capacidade com piso, informe nele a versão em que ela será publicada. Como novas capacidades são classificadas como `feature`, o valor corresponde ao próximo minor (`python3 S/release.py next --level minor`). A automação valida a coerência no sentido inverso: se o código declarar um piso superior à versão calculada, o nível de release sobe até alcançá-lo (registrando a elevação nas notas); se ainda assim não alcançar, a release é bloqueada.

Apps de terceiros (`applications/` e `examples/notes`) mantêm versionamento próprio em seus manifestos, gerenciado por `package build`, e não entram no versionamento do core.

### Superfícies públicas e 0.x

As superfícies públicas listadas em `[[versioning.public_surfaces]]` abrangem a API de serviços e o kit de testes, as bases de modelo, contexto e autorização, o manifesto e o formato de pacotes de apps, a API HTTP e as migrations do núcleo. Enquanto o core estiver na série `0.y.z`, quebras de compatibilidade elevam o minor (`0.1.x` → `0.2.0`), comportamento esperado pela faixa `>=0.1.0,<0.2.0` das apps first-party. A versão `1.0.0` só é publicada quando o PR de release recebe `semver:major` com justificativa explícita.

**Ressalva sobre pré-releases:** a faixa `>=0.2.0,<0.3.0` da agenda não aceita o core `0.2.0rc1`. Antes de publicar a primeira pré-release, ajuste a entrada da faixa em `[[versioning.files]]`.

### Artefatos de cada release (#496)

Após publicar uma versão, o workflow "Versão e release" aciona `release-artifacts.yml` (`release.dispatch_workflow = "release-artifacts.yml"`, para o qual `release.yml` já conta com `actions: write`). O pipeline opera em duas etapas:

- **Validação sem segredos (`validate`).** Antes de abrir o ambiente `release` ou fazer checkout, o job `validate` (apenas com `contents: read`) valida o formato `vX.Y.Z`, confirma a correspondência `v$VERSION == $TAG` e verifica a existência de `refs/tags/$TAG` via `git ls-remote --exit-code --tags`.
- **Construção e publicação (`publish`).** Somente após a validação o job `publish` faz checkout de `refs/tags/<tag>`, acessa o segredo `RELEASE_SIGNING_KEY` do ambiente `release` e anexa à GitHub Release os artefatos assinados ([deployment.md](deployment.md#publicação-automática-nos-releases-do-github)): o pacote do core, o pacote de release, o wheel do núcleo (`django_skeleton-<versão>-py3-none-any.whl`, usado em versão fixa pelo CI de apps em repositórios próprios, com o SHA-256 acrescentado ao corpo da release), o wheel do agente, o índice `skeleton-index.json` e `SHA256SUMS`.

No dia 1 de cada mês, o mesmo workflow renova a validade do índice da release mais recente. Em caso de falha, basta executá-lo novamente (Actions → "Artefatos de release" → Run workflow, informando versão e tag): o build é determinístico e o upload usa `--clobber`, substituindo os assets sem efeitos colaterais. Para desativar a publicação automática de artefatos, esvazie `release.dispatch_workflow`. O consumo desses assets pelas instalações está detalhado no [guia de atualização](updates.md).

## O skill neste repositório

A cópia em `.claude/skills/dev-workflow/` provém do repositório próprio do skill; `python3 S/bootstrap.py version` exibe a versão instalada. Para atualizá-la, abra uma issue `chore`, execute `update` **a partir do clone na nova versão** e revise o diff ([installation.md](../.claude/skills/dev-workflow/references/installation.md#atualizar-o-skill)):

```sh
python3 <clone-do-skill>/scripts/bootstrap.py update --dry-run
python3 <clone-do-skill>/scripts/bootstrap.py update
python3 S/bootstrap.py doctor
```

Qualquer correção urgente feita localmente na cópia deve ser levada de volta ao repositório do skill via PR, pois o próximo `update` sobrescreve os arquivos locais.

| Peça própria deste repositório | Arquivo |
|---|---|
| Configuração | [.github/dev-workflow.toml](../.github/dev-workflow.toml) |
| Hooks do Claude Code | [.claude/settings.json](../.claude/settings.json): modo de planejamento, pedidos relacionados ao fluxo, plano aceito, `git commit` e `gh pr create` |
| Hook `commit-msg` | [.pre-commit-config.yaml](../.pre-commit-config.yaml), que chama `github_issues.py commit-msg` do skill |
| Locks do Dependabot | `.github/workflows/dependabot-locks.yml` e `tools/workflow/dependabot_locks.py`: regeneram com `uv` os locks dos PRs de pip do Dependabot e commitam na branch do PR com `Refs #140`. Veja [testes](testing.md#locks-dos-prs-do-dependabot) |

## Preparação de um checkout

```sh
uv tool install pre-commit
pre-commit install            # instala os hooks pre-commit e commit-msg
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor
```

Prefira `uv tool install` a `uvx pre-commit install`: o hook gravado em `.git/hooks` aponta para o interpretador Python que o instalou, e ambientes efêmeros do `uvx` podem ser limpos do cache. Os hooks do Claude Code requerem `node` no `PATH`. A suíte de testes do skill não depende do Django nem de banco de dados e pode ser executada com `python3 -m unittest discover -s .claude/skills/dev-workflow/tests`.

## Passos únicos do responsável

1. **Tag de base.** A primeira release (`v0.1.0`) cobre todo o histórico inicial e foi calculada a partir de `initial_version = "0.0.0"`, sem tag anterior, passando a servir de base para as versões seguintes (`initial_version` só vale enquanto não existe tag). Números anteriores a ela (como "core/agente 0.4.0") representam marcos internos de desenvolvimento nunca publicados — permanecem apenas no registro histórico de [evidência de testes](testing-evidence.md), enquanto a documentação de uso referencia exclusivamente versões publicadas.
2. **Ruleset da `main` (veja acima).** As versões são publicadas via PR (`release.mode = "pull-request"`), compatível com o ruleset da branch protegida: após o merge de um PR com label `release`, o workflow "Versão e release" atualiza a versão, o `CHANGELOG.md` e os arquivos de versão na branch `release/next` e abre o PR `chore(release): vX.Y.Z`. A tag, a GitHub Release e os comentários nas issues são criados quando esse PR é mesclado. Para habilitar esse fluxo, configure uma única vez:
   - marque "Allow GitHub Actions to create and approve pull requests" em Settings → Actions → General (`bootstrap.py doctor` aponta essa pendência enquanto estiver desligada);
   - crie o segredo `RELEASE_TOKEN` (fine-grained PAT ou GitHub App com permissão de escrita em `contents` e `pull-requests`), pois PRs abertos pelo `GITHUB_TOKEN` não disparam os checks obrigatórios (`CI` e `Verificação do PR`); sem esse segredo, é preciso fechar e reabrir o PR de release antes do merge.

   O PR de release altera apenas arquivos de versão e o changelog e é isento da verificação de issue. Ele pode ser integrado por merge ou squash (o formato `chore(release): vX.Y.Z (#N)` é reconhecido automaticamente). Não edite o PR à mão: a cada push na `main`, o workflow recalcula a versão e recria o mesmo PR.
3. **Segredos opcionais.** `ANTHROPIC_API_KEY` ou `CLAUDE_CODE_OAUTH_TOKEN` ativam a análise automática de versão no CI; sem eles, o agente executa a análise localmente ao abrir o PR `release`, e a release continua funcionando normalmente mesmo sem análise.

## Exceções

- Merges, reverts e fixups gerados pelo git, bem como o commit de release, não precisam referenciar uma issue própria.
- PRs do Dependabot dispensam issue; sem o label `release`, entram como mudança acumulada. Atualizações manuais de dependências seguem o fluxo normal como `chore`.
- Commits anteriores à adoção deste fluxo não são reescritos, e o histórico anterior à tag de base não entra no changelog.
