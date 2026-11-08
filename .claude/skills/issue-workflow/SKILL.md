---
name: issue-workflow
description: Rastreabilidade issue-first deste repositório no GitHub. Use SEMPRE (1) logo depois que um plano for aceito (ExitPlanMode), para transformar o plano em issues classificadas e detalhadas; (2) antes da primeira edição de qualquer desenvolvimento (código, testes, docs, CI, dependências), para vincular o trabalho a uma issue existente ou criar uma; (3) ao criar branch, commitar ou abrir PR; (4) quando o usuário aprovar a entrega, para fechar a issue com commits e evidências. Triggers: plano aceito, implementar, corrigir, refatorar, documentar, commit, pull request, fechar issue, "item do GitHub", issue, plan accepted.
---

# Fluxo issue-first

Nenhuma mudança versionada existe sem issue. A norma está em `AGENTS.md` (seção "Rastreabilidade") e o racional em `docs/workflow.md`; este skill é o procedimento. Escreva issues, comentários e PRs em português; commits seguem o histórico (Conventional Commits em inglês).

Modelos de corpo: [references/issue-templates.md](references/issue-templates.md). Leia-o antes de redigir qualquer issue, comentário de fechamento ou PR.

## 0. Pré-requisitos (uma vez por sessão)

1. `gh auth status` e `gh repo view --json nameWithOwner -q .nameWithOwner`.
2. Labels: se `gh label list` não mostrar `feature`, `refactor` e `epic`, rode `py -3 tools/workflow/github_issues.py sync-labels` (Windows) ou `python3 tools/workflow/github_issues.py sync-labels`.
3. Hook `commit-msg`: se `git rev-parse --git-path hooks/commit-msg` não existir, rode `uv tool install pre-commit` e `pre-commit install` (a config instala `pre-commit` e `commit-msg`). O hook do Claude bloqueia `git commit` enquanto ele faltar.

## 1. Classificação

Cada issue recebe **exatamente um** label de tipo. `security` é adicional; `epic` é só para a issue de acompanhamento.

| Label | Quando | Commit / branch |
|---|---|---|
| `bug` | Comportamento diverge do documentado/esperado, regressão, falha de segurança existente | `fix` |
| `feature` | Capacidade nova para usuário, operador ou projeto derivado | `feat` |
| `enhancement` | Melhoria de algo que já existe (UX, desempenho, ergonomia, mensagens) | `feat` ou `perf` |
| `documentation` | Só documentação, sem mudar comportamento | `docs` |
| `refactor` | Reestruturação sem mudança observável | `refactor` |
| `test` | Só testes, fixtures ou evidência | `test` |
| `ci` | Pipeline, workflows e checks automáticos | `ci` |
| `chore` | Dependências, ferramentas, configuração de desenvolvimento | `chore` / `build` |

Adicione `security` quando o item tocar tenant, RLS, autorização, autenticação/MFA, auditoria, segredos, CSRF/CAS ou superfície HTTP. Documentação, testes e ajustes que acompanham uma feature/bug ficam **dentro** dela (são parte da definição de pronto), não em issues separadas.

## 2. Plano aceito → issues

Executa sem pedir nova confirmação: a aceitação do plano é a autorização. Faça antes de qualquer edição.

1. Releia o plano aprovado. Ele deve ter a seção "Itens do GitHub" (exigida pelo `AGENTS.md`); se não tiver, derive os itens do plano e diga isso ao usuário.
2. Decomponha em entregas **independentes, testáveis e revisáveis** (tipicamente um PR cada). Não crie issue por arquivo nem por passo mecânico; não misture tipos numa issue.
3. Deduplicar: `gh issue list --state open --search "<palavras-chave>"`. Se já existe issue equivalente, atualize-a (`gh issue edit N --body-file`) ou comente, em vez de duplicar.
4. Redija cada corpo pelo modelo do tipo, com **todas** as seções: contexto, objetivo, escopo e fora de escopo, abordagem técnica (arquivos/módulos), critérios de aceite verificáveis, plano de testes (camadas, negativos de segurança, comandos), documentação a atualizar, segurança/contrato do `AGENTS.md`, riscos/dependências e definição de pronto. Seção sem conteúdo leva "Não se aplica — <motivo>"; nunca a remova.
5. Grave o corpo em arquivo UTF-8 no scratchpad e crie com `--body-file` (nunca corpo longo inline, por causa do quoting do PowerShell/Bash):
   ```bash
   gh issue create --title "<resumo no imperativo>" --label feature,security --assignee @me --body-file <scratchpad>/issue-1.md
   ```
   Título curto, em português, sem prefixo de tipo (o label já classifica).
6. Com 2 ou mais issues: depois de criar as filhas, crie a issue `epic` "Plano: <nome>" com o resumo do plano e a lista ordenada `- [ ] #N título` (com dependências). Em cada filha, a seção "Riscos e dependências" diz `Parte de #E` (edite o corpo depois de criar o epic).
7. Informe ao usuário uma tabela `# | título | labels | link`. Só então comece a implementação pela primeira issue (fase 3).

## 3. Desenvolvimento sem plano (pedido direto)

Antes da primeira edição de arquivo versionado:

1. Identifique a issue: número citado pelo usuário, número na branch (`<tipo>/<N>-<slug>`) ou busca (`gh issue list --search`). Confira se o escopo dela cobre o pedido.
2. Se não houver, crie uma (passos 2.4–2.5), mesmo para mudança pequena: o corpo pode ser curto, mas mantém todas as seções. Não pergunte; informe o link.
3. Perguntas, leituras e investigações sem mudança de arquivo não exigem issue. Se a investigação virar correção, crie a issue antes da correção, com o diagnóstico em "Contexto".
4. Mudança de escopo no meio do trabalho: atualize o corpo da issue ou crie outra para o excedente. Não amplie a issue em silêncio.

## 4. Branch, commits e progresso

- Branch: `<prefixo>/<N>-<slug>` a partir de `origin/main` atualizada (`git fetch origin` e `git switch -c feat/42-slug origin/main`). Se a árvore estiver suja ou o usuário estiver em outra branch de trabalho, pergunte antes de trocar.
- Commit só quando o usuário pedir. Mensagem: `<tipo>(<escopo>): <assunto>` em inglês, corpo explicando o porquê e trailer `Refs #N` (uma linha por issue) antes do `Co-Authored-By`. Não use `Closes` em commits: o fechamento acontece pela fase 6.
- O hook `commit-msg` acrescenta `Refs: #N` a partir do nome da branch e recusa commits sem referência. Nunca use `--no-verify`.
- Marque um critério de aceite (`gh issue edit N --body-file`) apenas depois de verificá-lo de fato. Registre decisões relevantes e bloqueios com `gh issue comment N --body-file`.

## 5. Pull request

- `gh pr create --base main --title "<tipo>(<escopo>): <assunto>" --body-file <arquivo>` com o modelo de PR das referências (espelha `.github/pull_request_template.md`).
- `Closes #N` para cada issue entregue por inteiro, `Refs #N` para entrega parcial. O hook do Claude recusa `gh pr create` sem referência, e o workflow "Vínculo com issue" recusa PR sem issue válida e classificada.
- Testes listados no PR: somente os executados de fato, com comando e resultado. Serviço indisponível é bloqueio, não aprovação.

## 6. Aprovação e fechamento

Aprovação é (a) merge do PR na `main`, que fecha as issues com `Closes`, ou (b) aprovação explícita do usuário no chat quando não há PR. Nunca feche por iniciativa própria nem só porque os testes passaram.

No caso (b), ou quando a issue não foi fechada automaticamente:

1. Todos os critérios de aceite marcados, ou dispensados explicitamente pelo usuário (registre a dispensa).
2. Commits publicados no remoto, para que os links resolvam: `git log --oneline --grep "#N" origin/<branch>`.
3. Comentário de fechamento pelo modelo (commits com SHA, PR, testes executados com comando e resultado, documentação atualizada, follow-ups com links): `gh issue comment N --body-file <arquivo>`.
4. `gh issue close N --reason completed`.
5. Se a issue for filha de um epic, marque-a na lista do epic; feche o epic quando a última filha fechar, com comentário-resumo.

Itens abandonados: `gh issue close N --reason "not planned"` somente por decisão do usuário, com o motivo em comentário.
