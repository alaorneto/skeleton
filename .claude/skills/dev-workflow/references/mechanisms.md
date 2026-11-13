# Mecanismos

Cada barreira do fluxo e onde ela vive. `SKILL` é o diretório do skill; os workflows o chamam no próprio repositório, sem cópia dos scripts.

| Camada | Onde | O que garante |
|---|---|---|
| Configuração | `.github/dev-workflow.toml` | Tipos, labels, seções, branch padrão, regras de PR e de versão ([configuration.md](configuration.md)) |
| Git | hook `commit-msg` (`assets/hooks/commit-msg` instalado por `bootstrap.py init`, ou o trecho do pre-commit abaixo) | Recusa commit sem referência a issue; na branch `<tipo>/<N>-<slug>`, acrescenta `Refs: #N`. Merges, reverts e fixups gerados pelo git passam |
| Claude Code | `SKILL/scripts/claude_hook.mjs` | Ao aceitar um plano, instrui o agente a criar as issues antes de editar. Bloqueia `git commit` sem o hook `commit-msg` ou com `--no-verify`, e `gh pr create` sem `Closes`/`Refs #N` |
| PR | `assets/workflows/pr-check.yml`, instalado em `.github/workflows/` → `github_issues.py check-pr` | PR referencia issue existente com exatamente um label de tipo |
| Epics | `github_issues.py sync-epic` | Sub-issues e "blocked by" nativos a partir da seção de itens |
| Caixas e fechamento do epic | `assets/workflows/epic-checklist.yml`, instalado em `.github/workflows/` → `sync-checklist`, `close-epic` | Marca o item da filha fechada; fecha o epic quando a última filha fecha |

## Hook do Claude Code

Em `.claude/settings.json`:

```json
{
  "hooks": {
    "PostToolUse": [
      {"matcher": "ExitPlanMode", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/scripts/claude_hook.mjs\" plan-accepted", "timeout": 10}]}
    ],
    "PreToolUse": [
      {"matcher": "Bash|PowerShell", "hooks": [{"type": "command", "command": "node \"$CLAUDE_PROJECT_DIR/.claude/skills/dev-workflow/scripts/claude_hook.mjs\" pre-shell", "timeout": 10}]}
    ]
  }
}
```

O hook só bloqueia na branch cujo HEAD já versiona `.github/dev-workflow.toml` (ou o hook `issue-reference` do pre-commit): branches anteriores à adoção seguem livres. Falha interna nunca bloqueia; as barreiras finais são o `commit-msg` e o CI.

## Pre-commit

Quem usa pre-commit acrescenta o hook local em vez do hook git e roda `pre-commit install --hook-type commit-msg`:

```yaml
  - repo: local
    hooks:
      - id: issue-reference
        name: referência a issue do GitHub
        entry: python .claude/skills/dev-workflow/scripts/github_issues.py commit-msg
        language: python
        stages: [commit-msg]
```

## Segurança dos workflows

Dados do evento (título, corpo, autor, labels, números) entram por variável de ambiente, nunca interpolados no shell. O check de PR faz checkout da branch base e só lê; nenhum código do PR roda com token de escrita.
