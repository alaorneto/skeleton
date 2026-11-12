# Instalação num projeto

`python3` é `py -3` no Windows. Requisitos: Python 3.11+, `git`, `gh` autenticado; `node` só para os hooks opcionais do Claude Code.

## 1. Um comando

Com o skill em qualquer lugar (por exemplo `~/.claude/skills/dev-workflow`), na raiz do repositório do projeto:

```bash
python3 <skill>/scripts/bootstrap.py init
```

O que ele faz:

| Passo | Resultado |
|---|---|
| Copia o skill para `.claude/skills/dev-workflow/` se estiver fora do repositório | Os workflows do CI chamam os scripts de lá; por isso o skill é versionado junto com o projeto |
| Detecta a branch padrão e os arquivos de versão (`pyproject.toml`, `package.json`, `Cargo.toml`, `VERSION`, `pubspec.yaml`, `mix.exs`, `build.gradle`, `Directory.Build.props`…) | `.github/dev-workflow.toml` com `initial_version` e `[[versioning.files]]` preenchidos |
| Gera os workflows a partir dos modelos | `.github/workflows/pr-check.yml`, `pr-merged.yml`, `release.yml`, `epic-checklist.yml`, `semver-analysis.yml` |
| Copia os modelos | `.github/ISSUE_TEMPLATE/*.yml`, `.github/pull_request_template.md` |
| Instala o hook `commit-msg` | `.git/hooks/commit-msg`; se o projeto usa pre-commit, mostra o trecho a acrescentar |
| Cria os labels | Tipos, `epic`, `release`, `breaking-change`, `semver:major|minor|patch` |

Opções: `--default-branch`, `--initial-version`, `--no-labels`, `--selftest` (instala também o workflow que roda a suíte do skill), `--dry-run`, `--force` (troca arquivos que já existem; sem ele, nada existente é sobrescrito).

## 2. Ajustar a configuração

Abra `.github/dev-workflow.toml` ([referência](configuration.md)) e confira:

- `[[versioning.files]]`: todo lugar em que o número aparece e deve acompanhar a versão. Teste: `python3 S/release.py check` e `python3 S/release.py bump --to 9.9.9 --dry-run` (lista os arquivos que mudariam).
- `[[versioning.public_surfaces]]`: os caminhos cujo diff interessa a quem usa o projeto (API, CLI, esquema, formato de arquivo).
- `[types]`: os tipos e o nível que cada um pede.
- `release.mode`: `direct`, ou `pull-request` se a branch padrão exige PR.

Depois: `python3 S/bootstrap.py doctor`.

## 3. Commitar e mesclar

Os workflows só passam a valer quando estão na branch padrão. Commite tudo que o `init` gerou (inclusive `.claude/skills/dev-workflow/`) e mescle.

Cada pessoa instala o hook no próprio checkout, uma vez: `python3 S/bootstrap.py install-hook` (ou `pre-commit install`).

## 4. Passos de quem é dono do repositório (uma vez)

1. **Tag de base.** Diz "tudo até aqui é a versão X": `python3 S/release.py baseline --push`, na branch padrão já com o fluxo mesclado. Sem ela, a primeira release consideraria o histórico inteiro.
2. **Permissões do Actions.** Os workflows declaram as permissões de que precisam; em "Settings → Actions → General → Workflow permissions" o padrão "Read" basta. No modo `pull-request`, marque "Allow GitHub Actions to create and approve pull requests".
3. **Proteção de branch (recomendado).** Marque "Verificação do PR" como check obrigatório da branch padrão. Atenção ao modo de release:
   - `direct` precisa que o workflow consiga fazer push na branch. Com proteção que exige PR, ou troque para `pull-request`, ou crie o segredo `RELEASE_TOKEN` com um token (fine-grained PAT ou GitHub App) de quem pode ignorar a proteção.
   - `pull-request`: o PR de release é aberto pelo token do Actions, que não dispara os checks. Com checks obrigatórios, use `RELEASE_TOKEN` (aí os checks rodam) ou feche e reabra o PR.
4. **Segredos opcionais.**
   - `RELEASE_TOKEN`: escrever em branch protegida e fazer a tag disparar outros workflows.
   - `ANTHROPIC_API_KEY` ou `CLAUDE_CODE_OAUTH_TOKEN`: liga a análise de versão por agente no CI. Sem eles o workflow pula os passos.
5. **Publicação de artefatos (opcional).** Aponte `release.dispatch_workflow` para um workflow com `workflow_dispatch` e os inputs `version` e `tag`.

## 5. Hooks do Claude Code (opcional)

Acrescente em `.claude/settings.json` do projeto:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "ExitPlanMode", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" plan-accepted", "timeout": 10}]}
    ],
    "PreToolUse": [
      {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" pre-shell", "timeout": 10}]}
    ]
  }
}
```

Eles lembram o agente de criar as issues quando um plano é aceito e recusam `git commit --no-verify` e `gh pr create` sem referência a issue. São lembretes; as barreiras são o hook `commit-msg` e o check do PR.

## 6. Normas do projeto

Acrescente às instruções do projeto (`AGENTS.md`, `CLAUDE.md`) um parágrafo curto apontando para o skill: todo trabalho versionado nasce de uma issue; branch `<tipo>/<N>-<slug>`; commits com `Refs #N`; PR com `Closes #N` e o label `release` quando o merge deve publicar versão; ninguém edita à mão os arquivos de versão nem o changelog.

## Atualizar o skill

Substitua `.claude/skills/dev-workflow/` pela versão nova e rode `bootstrap.py doctor`: ele aponta os workflows que diferem dos modelos. `bootstrap.py init --force` os regenera (revise o diff antes de commitar, para não perder personalizações). A configuração nunca é trocada sem `--force`.

## Conferir que funciona

```bash
python3 -m unittest discover -s .claude/skills/dev-workflow/tests
```

A suíte cria repositórios git temporários fora do projeto e simula o GitHub; não toca o repositório nem a rede.
