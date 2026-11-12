---
name: dev-workflow
description: Fluxo de desenvolvimento rastreável no GitHub, do plano à release, para projetos de qualquer tecnologia - issue antes de qualquer edição, branch e commits vinculados, PR verificado, versão semântica calculada e publicada sozinha. Use SEMPRE que (1) um plano for aceito (ExitPlanMode), para criar as issues; (2) for começar a implementar, corrigir, refatorar, documentar ou mexer em CI ou dependências, para vincular o trabalho a uma issue; (3) criar branch, commitar ou abrir PR; (4) classificar um PR como gerando versão ou não (labels release / no-release), decidir patch, minor ou major, ou analisar o diff acumulado; (5) versionar, gerar changelog, publicar ou desfazer uma release, pré-release ou hotfix; (6) fechar uma issue com evidências; (7) instalar ou conferir este fluxo num projeto. Vale também quando o pedido só diz "sobe a versão", "qual é a próxima versão", "isso quebra compatibilidade?", "abre o PR", "fecha a issue", "o que está pendente de release", semver, changelog, tag, release notes, bump.
---

# Fluxo de desenvolvimento: da issue à release

Nenhuma mudança versionada existe sem issue, e nenhuma versão é escrita à mão. O que é específico do projeto (tipos, seções, arquivos de versão, superfícies públicas) está em `.github/dev-workflow.toml`; este skill é o procedimento e as ferramentas.

Os scripts ficam em `scripts/` deste skill. Nos comandos abaixo, `S` é esse diretório (em geral `.claude/skills/dev-workflow/scripts`) e `python3` é `py -3` no Windows. Eles usam só a biblioteca padrão do Python 3.11+, `git` e `gh`.

Leia conforme a fase:

| Quando | Leia |
|---|---|
| Vai redigir issue, PR, comentário de fechamento ou de mudança de escopo | [references/templates.md](references/templates.md) |
| Vai classificar um PR, analisar o diff para decidir o nível, publicar, desfazer ou fazer hotfix | [references/versioning.md](references/versioning.md) |
| Quer o passo a passo do que acontece num PR com e sem versão, ou o porquê de uma decisão | [references/flow.md](references/flow.md) |
| Vai instalar o fluxo num projeto ou mexer na configuração | [references/installation.md](references/installation.md), [references/configuration.md](references/configuration.md) |

Escreva issues, PRs e comentários no idioma do projeto; commits seguem o histórico (Conventional Commits).

## 0. Pré-requisitos (uma vez por sessão)

1. `gh auth status`.
2. `python3 S/bootstrap.py doctor`. Ele diz o que falta: configuração, labels, hook `commit-msg`, workflows, arquivos de versão coerentes, tag de base. Resolva os erros antes de seguir. Projeto sem `.github/dev-workflow.toml` ainda não adotou o fluxo: siga [references/installation.md](references/installation.md).

## 1. Classificação da issue

Cada issue recebe exatamente um label de tipo, definido em `[types]` da configuração. O tipo decide três coisas ao mesmo tempo: o prefixo de commit e de branch, o incremento de versão que a entrega pede e a seção do changelog. Por isso vale escolher com cuidado: uma `feature` rotulada como `chore` sai como patch e some do changelog.

Padrão de fábrica (o projeto pode ter outro; confira a configuração):

| Label | Quando | Commit / branch | Versão |
|---|---|---|---|
| `bug` | Comportamento diverge do esperado ou documentado; regressão | `fix` | patch |
| `feature` | Capacidade nova | `feat` | minor |
| `enhancement` | Melhoria de algo que já existe | `feat` ou `perf` | minor |
| `documentation` | Só documentação | `docs` | — |
| `refactor` | Reestruturação sem mudança observável | `refactor` | — |
| `test` | Só testes | `test` | — |
| `ci` | Pipeline e checks | `ci` | — |
| `chore` | Dependências, ferramentas, configuração | `chore` ou `build` | — |

Documentação e testes que acompanham uma feature ou um bug ficam dentro da issue dela, como parte da definição de pronto. `epic` é só para a issue que acompanha um plano.

## 2. Plano aceito → issues

A aceitação do plano é a autorização: execute sem pedir nova confirmação, antes de qualquer edição.

1. O plano termina com a seção "Itens do GitHub" (tipo, título, critérios de aceite, plano de testes, documentação). Se não tiver, derive os itens e diga isso ao usuário.
2. Decomponha em entregas independentes, testáveis e revisáveis, tipicamente um PR cada. Não crie issue por arquivo nem misture tipos numa issue.
3. Procure duplicatas: `gh issue list --state open --search "<palavras-chave>"`. Se existe equivalente, atualize-a em vez de duplicar.
4. Redija cada corpo pelo modelo do tipo, com todas as seções. Seção sem conteúdo leva "Não se aplica — motivo"; ela não é removida, porque a ausência não distingue "não se aplica" de "esqueci".
5. Grave o corpo num arquivo UTF-8 temporário e crie com `--body-file` (corpo longo inline quebra no quoting do shell):
   ```bash
   gh issue create --title "<resumo no imperativo>" --label feature --assignee @me --body-file <arquivo>
   ```
6. Com duas ou mais issues, crie também a issue `epic` "Plano: <nome>" com a lista `- [ ] #N título — depende de #M | independente` na seção "Itens", e gere os vínculos nativos:
   ```bash
   python3 S/github_issues.py sync-epic <E>            # cria sub-issues e "blocked by"; idempotente
   python3 S/github_issues.py sync-epic <E> --dry-run  # deve dizer "0 vínculos a criar"
   ```
   Em cada filha, a seção de riscos diz `Parte de #E`. Resolva cada divergência que o comando apontar.
7. Mostre ao usuário a tabela `# | título | labels | link` e só então implemente.

## 3. Pedido direto, sem plano

Antes da primeira edição de arquivo versionado, identifique a issue (número citado, número na branch, busca) e confira se o escopo dela cobre o pedido. Se não houver, crie uma, mesmo para mudança pequena; informe o link, não pergunte. Perguntas e investigações sem mudança de arquivo não precisam de issue; se a investigação virar correção, crie a issue antes, com o diagnóstico no contexto.

**Mudança de escopo.** Quando o trabalho passa a incluir algo que a issue não descreve, pare e escolha: se continua sendo a mesma entrega, atualize o corpo da issue e registre o comentário de mudança de escopo (modelo nas referências); se é outra entrega, abra outra issue e cite-a. Ampliar em silêncio quebra a revisão: quem aprovou a issue aprovou outro escopo. Se a mudança altera o tipo (uma correção que virou funcionalidade), troque o label, porque ele decide a versão.

## 4. Branch, commits e progresso

- Branch a partir da branch padrão atualizada: `python3 S/github_issues.py start <N>` cria `<prefixo>/<N>-<slug>` pelo tipo e título da issue (`--dry-run` só mostra o nome). Com a árvore suja ou outra branch de trabalho em uso, pergunte antes de trocar.
- Commit só quando o usuário pedir. Mensagem `<tipo>(<escopo>): <assunto>`, corpo com o porquê, trailer `Refs #N`. Quebra de compatibilidade leva `!` depois do tipo ou rodapé `BREAKING CHANGE: <o que muda e como migrar>`: é o marcador que a automação de versão lê.
- O hook `commit-msg` acrescenta `Refs: #N` a partir do nome da branch e recusa commit sem referência. Não use `--no-verify`: o hook é a barreira que mantém o vínculo.
- Marque um critério de aceite só depois de verificá-lo de fato. Registre decisões e bloqueios em comentário na issue.

## 5. Pull request

1. Corpo pelo modelo (`.github/pull_request_template.md`): `Closes #N` para entrega completa, `Refs #N` para parcial; só os testes que rodaram, com comando e resultado; a seção "Versão" diz o impacto para quem usa o projeto.
2. Título em Conventional Commits: ele vira o commit de um squash e entra no changelog.
3. **Classifique a versão** com exatamente um label, e diga ao usuário qual escolheu e por quê:
   - `release`: o merge publica uma versão com tudo que está acumulado desde a última tag.
   - `no-release`: as mudanças ficam acumuladas e entram na próxima versão publicada.

   Se o usuário não disse, pergunte uma vez; se ele não quiser decidir agora, use `no-release`: nada se perde, porque a próxima release olha retroativamente tudo que ficou pendente.
4. Veja o que o merge publicaria: `python3 S/release.py plan --pr <N>` (ou `status` para o que já está acumulado).
5. Se o PR está marcado `release`, faça a **análise do diff acumulado** e registre-a no PR, seguindo [references/versioning.md](references/versioning.md#análise-por-agente). A análise cobre todas as mudanças pendentes, não só este PR, e só eleva o nível; ela é o que pega a quebra de compatibilidade que ninguém marcou.
6. Marque `breaking-change` quando houver quebra. Para sobrepor o nível calculado, o dono usa `semver:major|minor|patch` e escreve `Motivo da sobreposição: ...` na seção "Versão".
7. `gh pr create --base <padrão> --title "..." --body-file <arquivo> --label release` (ou `no-release`).

O check "Verificação do PR" confere tudo isso e comenta a prévia da versão. Antes de abrir, você pode rodar o mesmo check: `python3 S/github_issues.py check-pr --pr <N>`.

## 6. Merge, versão e fechamento

Quem implementa não faz merge nem fecha a própria issue por conta própria; o merge é a aprovação.

Depois do merge, sem intervenção:

- Cada issue citada recebe o comentário de evidências (commits, testes e documentação declarados no PR, critérios de aceite pendentes). As citadas com `Closes` são fechadas pelo GitHub.
- Se o PR era `release`, o workflow "Versão e release" calcula o número, escreve a versão nos arquivos declarados, atualiza o `CHANGELOG.md`, cria a tag anotada e a GitHub Release, e comenta a versão em cada PR e issue incluídos.
- A caixa da issue no epic é marcada; fechada a última filha, o epic fecha.

Confira o resultado em vez de presumir: `gh run list --workflow release.yml --limit 1` e `python3 S/release.py status`. Se algo falhou no meio, rodar o workflow de novo conclui o que faltou.

Sem PR (aprovação explícita do usuário no chat): todos os critérios marcados ou dispensados por ele, commits publicados, comentário de fechamento pelo modelo, e só então `gh issue close N --reason completed`. Item abandonado: `--reason "not planned"`, por decisão do usuário, com o motivo em comentário.

## 7. Operações de versão fora do caminho comum

Publicar o que está acumulado sem esperar um PR `release`, pré-release, hotfix numa versão antiga e desfazer uma release errada estão em [references/versioning.md](references/versioning.md). Em todas elas, comece por `python3 S/release.py run --dry-run`, que mostra o plano sem escrever nada, e nunca edite à mão os arquivos de versão nem o changelog: o check de coerência reprova a divergência e a próxima release falha.
