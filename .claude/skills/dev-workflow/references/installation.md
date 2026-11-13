# Instalação num projeto

`python3` é `py -3` no Windows. Requisitos: Python 3.11+, `git`, `gh` autenticado; `node` só para os hooks opcionais do Claude Code.

## 1. Um comando

A origem é o repositório do skill (um clone em qualquer lugar fora do projeto, por exemplo `~/src/dev-workflow`). Na raiz do repositório do projeto:

```bash
git clone https://github.com/<dono>/dev-workflow ~/src/dev-workflow
python3 ~/src/dev-workflow/scripts/bootstrap.py init
```

O que ele faz:

| Passo | Resultado |
|---|---|
| Copia o skill para `.claude/skills/dev-workflow/` se estiver fora do repositório, sem `.git/` e `.github/` do repositório do skill | Os workflows do CI chamam os scripts de lá; por isso o skill é versionado junto com o projeto. `bootstrap.py version` diz qual versão está instalada |
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
    "UserPromptSubmit": [
      {"hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" prompt", "timeout": 10}]}
    ],
    "PostToolUse": [
      {"matcher": "EnterPlanMode", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" plan-mode", "timeout": 10}]},
      {"matcher": "ExitPlanMode", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" plan-accepted", "timeout": 10}]}
    ],
    "PreToolUse": [
      {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/assets/claude/dev-workflow-hook.mjs\" pre-shell", "timeout": 10}]}
    ]
  }
}
```

| Gatilho | O que o agente recebe |
|---|---|
| Pedido no modo de planejamento, ou o agente entra nele | Carregar o skill antes de planejar; o plano termina com "Itens do GitHub" |
| Pedido que fala de issue, PR, commit, branch, merge, versão, release, tag, changelog ou plano | Carregar o skill antes de agir (perguntas sem mudança de arquivo não precisam) |
| Plano aceito | Criar as issues e o epic antes de qualquer edição |
| `git commit --no-verify`, commit sem o hook `commit-msg` instalado, PR sem `Closes`/`Refs #N` | Comando recusado, com a instrução |

São lembretes e guardas de texto; as barreiras são o hook `commit-msg` e o check do PR.

## 6. Normas do projeto

As instruções do projeto (`AGENTS.md`, `CLAUDE.md`) **delegam** ao skill em vez de repetir o procedimento: duas cópias divergem, e quem segue o texto copiado não recebe as ferramentas nem as correções do skill. Cole o trecho abaixo e acrescente depois dele só o que é do projeto (seções extras, arquivos de versão, pisos, checks obrigatórios):

```markdown
## Fluxo de desenvolvimento

O fluxo deste repositório é o skill `dev-workflow` (`.claude/skills/dev-workflow/`, configurado em
`.github/dev-workflow.toml`). Carregue o skill e siga-o (no Claude Code, pela ferramenta Skill; em
outro agente ou à mão, lendo `.claude/skills/dev-workflow/SKILL.md`) sempre que:

- entrar no modo de planejamento ou escrever um plano: ele termina com a seção "Itens do GitHub";
- um plano for aceito: as issues e o epic são criados antes de qualquer edição;
- for começar, sem plano, qualquer mudança em arquivo versionado (código, testes, documentação, CI,
  dependências);
- for criar ou atualizar uma issue ou um epic, criar branch, commitar ou abrir PR;
- for decidir se um PR publica versão e com que nível, ou mexer em release, changelog ou tag;
- for fechar uma issue.

Perguntas e investigações sem mudança de arquivo não precisam do skill. Valem sempre, mesmo sem ele:
nenhuma mudança versionada sem issue; nunca `--no-verify`; ninguém edita a versão nem o changelog à mão.
```

Os hooks do Claude Code (seção anterior) disparam o mesmo lembrete no modo de planejamento e nos pedidos que falam do fluxo; o texto acima é o que vale para qualquer agente e para pessoas.

## Atualizar o skill

A versão nova vem do repositório do skill, e o comando roda **a partir dela**:

```bash
git -C ~/src/dev-workflow pull
python3 ~/src/dev-workflow/scripts/bootstrap.py update --dry-run
python3 ~/src/dev-workflow/scripts/bootstrap.py update
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor
```

| O que | Comportamento |
|---|---|
| Cópia em `.claude/skills/dev-workflow/` | Espelhada: arquivos novos entram, os que saíram da versão nova são removidos |
| Workflows, formulários de issue e modelo de PR | Os iguais ao modelo da versão anterior recebem o modelo novo; os personalizados ficam como estão, com aviso (`--force` troca). Opcional ausente (`semver-analysis.yml`, autoteste) continua ausente |
| `.github/dev-workflow.toml` | Nunca é trocado. Chave nova tem padrão; chave removida aparece como erro no `doctor` |
| Versão mais antiga que a instalada | Recusada sem `--force` |

Rodar o `update` a partir da cópia instalada é recusado: ela não é uma versão nova. Commite o resultado numa branch de issue (`chore`), como qualquer mudança; os workflows novos valem depois do merge.

## Conferir que funciona

```bash
python3 -m unittest discover -s .claude/skills/dev-workflow/tests
```

A suíte cria repositórios git temporários fora do projeto e simula o GitHub; não toca o repositório nem a rede.
