---
name: dev-workflow
description: Fluxo de desenvolvimento issue-first e portátil no GitHub — plano, issue, branch, commit, PR, versão e release. Use SEMPRE (1) logo depois que um plano for aceito (ExitPlanMode), para transformar o plano em issues classificadas e detalhadas; (2) antes da primeira edição de qualquer desenvolvimento (código, testes, docs, CI, dependências), para vincular o trabalho a uma issue existente ou criar uma; (3) ao criar branch, commitar ou abrir PR; (4) quando o usuário aprovar a entrega, para fechar a issue com commits e evidências; (5) ao instalar o fluxo num repositório novo. Triggers: plano aceito, implementar, corrigir, refatorar, documentar, commit, pull request, fechar issue, issue, epic, versão, release, changelog, plan accepted.
---

# Fluxo de desenvolvimento (dev-workflow)

Nenhuma mudança versionada existe sem issue. O que é específico do projeto (labels de tipo, seções obrigatórias, branch padrão, regras de PR e de versão) está em `.github/dev-workflow.toml`; este skill é o procedimento e as ferramentas. Escreva issues, comentários e PRs no idioma da configuração (`project.language`); commits seguem Conventional Commits.

`SKILL` abaixo é o diretório deste skill (neste repositório, `.claude/skills/dev-workflow`). Os scripts usam só a biblioteca padrão do Python 3.11+, `git` e `gh`; no Windows chame `py -3`, fora dele `python3`.

Referências: [modelos de issue, PR e fechamento](references/issue-templates.md), [configuração](references/configuration.md), [mecanismos](references/mechanisms.md). Leia os modelos antes de redigir qualquer issue, comentário de fechamento ou PR.

## 0. Pré-requisitos (uma vez por sessão)

1. `gh auth status` e `gh repo view --json nameWithOwner -q .nameWithOwner`.
2. `py -3 SKILL/scripts/bootstrap.py doctor`. Item `falta` se resolve com `bootstrap.py init` (cria configuração, modelos, workflows, hook e labels sem sobrescrever o que existe); item `pendente` é passo do responsável ou exige `gh`.
3. Sem o hook `commit-msg` instalado, o hook do Claude bloqueia `git commit`. Nunca use `--no-verify`.

## 1. Classificação

Cada issue recebe **exatamente um** label de tipo da configuração (`[types.*]`); cada tipo lista os tipos de commit correspondentes (`commit`). Labels adicionais (`[labels.*]`, como `security`) são extras; o label de epic (`issue.epic_label`) é só para a issue de acompanhamento.

Documentação, testes e ajustes que acompanham uma entrega ficam **dentro** dela (são parte da definição de pronto), não em issues separadas.

## 2. Plano aceito → issues

Executa sem pedir nova confirmação: a aceitação do plano é a autorização. Faça antes de qualquer edição.

1. Releia o plano aprovado. Ele deve ter a seção "Itens do GitHub"; se não tiver, derive os itens do plano e diga isso ao usuário.
2. Decomponha em entregas **independentes, testáveis e revisáveis** (tipicamente um PR cada). Não crie issue por arquivo nem por passo mecânico; não misture tipos numa issue.
3. Deduplicar: `gh issue list --state open --search "<palavras-chave>"`. Se já existe issue equivalente, atualize-a (`gh issue edit N --body-file`) ou comente.
4. Redija cada corpo pelo modelo do tipo, com **todas** as seções (`issue.sections` lista as que o check de PR confere). Seção sem conteúdo leva "Não se aplica — <motivo>"; nunca a remova.
5. Grave o corpo em arquivo UTF-8 e crie com `--body-file` (nunca corpo longo inline):
   ```bash
   gh issue create --title "<resumo no imperativo>" --label <tipo>[,<extra>] --assignee @me --body-file <arquivo>
   ```
6. Com 2 ou mais issues, crie a issue de epic "Plano: <nome>" pelo modelo, com a lista ordenada `- [ ] #N título — depende de #M | independente` na seção de itens (`issue.items_section`). Crie os vínculos nativos com `py -3 SKILL/scripts/github_issues.py sync-epic <E>`: as filhas viram sub-issues na ordem da lista e cada "depende de" vira um "blocked by". O comando é idempotente, nunca troca um parent existente e termina com erro em qualquer divergência; resolva cada uma. Confira com `sync-epic <E> --dry-run` ("0 vínculos a criar", sem divergências).
7. Informe ao usuário uma tabela `# | título | labels | link`. Só então comece pela primeira issue.

## 3. Desenvolvimento sem plano

Antes da primeira edição de arquivo versionado:

1. Identifique a issue: número citado, número na branch (`<tipo>/<N>-<slug>`) ou busca. Confira se o escopo cobre o pedido.
2. Se não houver, crie uma (passos 2.4–2.5), mesmo para mudança pequena. Não pergunte; informe o link.
3. Perguntas e investigações sem mudança de arquivo não exigem issue. Se a investigação virar correção, crie a issue antes, com o diagnóstico em "Contexto".
4. Mudança de escopo no meio do trabalho: atualize o corpo da issue ou crie outra para o excedente, e registre com o modelo de mudança de escopo. Não amplie a issue em silêncio.

## 4. Branch, commits e progresso

- Branch: `py -3 SKILL/scripts/github_issues.py start N` cria `<tipo>/<N>-<slug>` a partir de `origin/<branch padrão>` (o tipo é o primeiro tipo de commit do label da issue). Ele recusa árvore suja; se o usuário estiver em outra branch de trabalho, pergunte antes.
- Commit só quando o usuário pedir. Mensagem: `<tipo>(<escopo>): <assunto>`, corpo explicando o porquê e trailer `Refs #N` (uma linha por issue). Não use `Closes` em commits.
- O hook `commit-msg` acrescenta `Refs: #N` a partir do nome da branch e recusa commits sem referência.
- Marque um critério de aceite (`gh issue edit N --body-file`) apenas depois de verificá-lo. Registre decisões e bloqueios com `gh issue comment N --body-file`.

## 5. Pull request

- `gh pr create --base <branch padrão> --title "<tipo>(<escopo>): <assunto>" --body-file <arquivo>` com o modelo de PR (espelha `.github/pull_request_template.md`).
- `Closes #N` para cada issue entregue por inteiro, `Refs #N` para entrega parcial. O hook do Claude recusa `gh pr create` sem referência, e o workflow de verificação do PR recusa PR sem issue válida e classificada.
- Testes listados no PR: somente os executados de fato, com comando e resultado. Serviço indisponível é bloqueio, não aprovação.

## 6. Aprovação e fechamento

Aprovação é (a) merge do PR na branch padrão, que fecha as issues com `Closes`, ou (b) aprovação explícita do usuário no chat quando não há PR. Nunca feche por iniciativa própria nem só porque os testes passaram.

No caso (b), ou quando a issue não foi fechada automaticamente:

1. Todos os critérios de aceite marcados, ou dispensados explicitamente pelo usuário (registre a dispensa).
2. Commits publicados no remoto: `git log --oneline --grep "#N" origin/<branch>`.
3. Comentário de fechamento pelo modelo (commits, PR, testes executados, documentação, follow-ups): `gh issue comment N --body-file <arquivo>`.
4. `gh issue close N --reason completed`.
5. Sub-issue de epic: o workflow `epic-checklist` marca `[x]` no item (e desmarca se reaberta) e, quando a última sub-issue fecha, fecha o epic com comentário-resumo e sobe para o epic de cima. Não marque nem feche à mão; se a caixa não mudar, rode `github_issues.py sync-checklist <E>`. Confira depois os "Critérios de conclusão" do epic.

Itens abandonados: `gh issue close N --reason "not planned"` somente por decisão do usuário, com o motivo em comentário.

## 7. Instalar num repositório novo

1. Copie este diretório para `.claude/skills/dev-workflow/` do repositório.
2. `py -3 SKILL/scripts/bootstrap.py init` (`--no-labels` sem `gh`; `--hook pre-commit` para receber o trecho do pre-commit em vez do hook git). Ajuste `.github/dev-workflow.toml` (references/configuration.md) e rode `bootstrap.py doctor`.
3. Para o hook do Claude Code, acrescente ao `.claude/settings.json` os comandos de `references/mechanisms.md`.
4. Testes do próprio skill: `py -3 -m unittest discover -s SKILL/tests -t SKILL`.
