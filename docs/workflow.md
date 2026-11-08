# Fluxo de trabalho: plano, issue, commit e fechamento

Todo trabalho versionado neste repositório nasce de uma issue do GitHub e termina com ela fechada depois de aprovado, com os commits e as evidências ligados a ela. A regra normativa está no [AGENTS.md](../AGENTS.md#rastreabilidade-plano-issue-commit-e-fechamento). Este guia explica o ciclo e os mecanismos que o garantem, para pessoas e agentes.

## Ciclo

1. **Planejar.** No modo de planejamento do Claude Code, o plano termina com a seção `Itens do GitHub`: para cada item, o label de tipo, o título, os critérios de aceite, o plano de testes e a documentação a atualizar. Revisar o plano é também revisar essa decomposição.
2. **Aceitar → issues.** Quando o plano é aceito, o agente cria as issues antes de qualquer edição: uma por entrega independente e revisável, cada uma detalhada pelo modelo do tipo. Com duas ou mais, cria também uma issue `epic` com a lista ordenada das filhas. Sem modo de planejamento, a issue é identificada ou criada antes da primeira edição.
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
- **Plano de testes**: unitários, integração SQLite, PostgreSQL com a role runtime, negativos de segurança, smoke manual e os comandos. Bugs incluem um teste de regressão.
- **Documentação a atualizar**, incluindo a evidência em [testing](testing.md).
- **Segurança e contrato do AGENTS.md**.
- **Riscos e dependências**: epic, issues bloqueantes, migrations, rollback.
- **Definição de pronto**.

Uma seção sem conteúdo leva "Não se aplica — motivo". Ela não é removida.

## Mecanismos

| Camada | Arquivo | O que garante |
|---|---|---|
| Norma | `AGENTS.md` | Regras lidas por qualquer agente e pessoa |
| Procedimento | `.claude/skills/issue-workflow/` | Passo a passo do agente e modelos de issue, PR e fechamento |
| Hook do Claude Code | `.claude/settings.json`, `.claude/hooks/issue-workflow.mjs` | Ao aceitar um plano, instrui o agente a criar as issues antes de editar. Bloqueia `git commit` sem o hook `commit-msg` instalado ou com `--no-verify`, e `gh pr create` sem `Closes`/`Refs #N` |
| Git | `.pre-commit-config.yaml`, `tools/workflow/github_issues.py commit-msg` | Recusa commit sem referência a issue. Na branch `<tipo>/<N>-<slug>`, acrescenta `Refs: #N` sozinho. Merges, reverts e fixups gerados pelo git passam |
| CI | `.github/workflows/issue-link.yml` | O PR precisa citar uma issue existente deste repositório com exatamente um label de tipo. PRs do Dependabot ficam isentos |
| GitHub | `.github/ISSUE_TEMPLATE/`, `.github/pull_request_template.md` | Formulários com as seções obrigatórias. Issue em branco desabilitada |

O bloqueio de `git commit` por falta do hook `commit-msg` só vale nas branches cujo `HEAD` já versiona o hook `issue-reference` em `.pre-commit-config.yaml`. Branches anteriores à adoção não são bloqueadas. Os hooks do Claude Code são lembretes fortes, não uma barreira de segurança: eles analisam o texto do comando. As barreiras finais são o hook `commit-msg` e o check do PR. Para que o check impeça o merge, marque "PR vinculado a issue classificada" como check obrigatório na proteção da branch `main`.

## Preparação de um checkout

```sh
uv tool install pre-commit
pre-commit install            # instala os hooks pre-commit e commit-msg
python3 tools/workflow/github_issues.py sync-labels   # Windows: py -3 tools/workflow/github_issues.py sync-labels
```

`sync-labels` é idempotente e só precisa rodar de novo quando a lista de labels mudar. Prefira `uv tool install` a `uvx pre-commit install`: o hook gravado em `.git/hooks` aponta para o Python que o instalou, e o ambiente efêmero do `uvx` pode ser removido do cache. Os hooks do Claude Code exigem `node` no `PATH`.

## Exceções

- Merges, reverts e fixups gerados pelo git não precisam de referência própria.
- PRs do Dependabot dispensam issue. Uma atualização de dependência feita à mão segue o fluxo com `chore`.
- Perguntas, leituras e investigações sem mudança de arquivo não precisam de issue. Se a investigação levar a uma correção, a issue é criada antes da correção, com o diagnóstico no contexto.
- Commits anteriores à adoção deste fluxo não são reescritos.
