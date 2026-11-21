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
| Copia o skill para `.claude/skills/dev-workflow/` (ou para `--skill-dir`) se estiver fora do repositório, sem `.git/` e `.github/` do repositório do skill | Os workflows do CI chamam os scripts de lá; por isso o skill é versionado junto com o projeto. `bootstrap.py version` diz qual versão está instalada |
| Detecta a branch padrão e os arquivos de versão (`pyproject.toml`, `package.json`, `Cargo.toml`, `VERSION`, `pubspec.yaml`, `mix.exs`, `build.gradle`, `Directory.Build.props`…) | `.github/dev-workflow.toml` com `initial_version` e `[[versioning.files]]` preenchidos |
| Gera os workflows a partir dos modelos | `.github/workflows/pr-check.yml`, `pr-merged.yml`, `release.yml`, `epic-checklist.yml`, `semver-analysis.yml` |
| Copia os modelos | `.github/ISSUE_TEMPLATE/*.yml`, `.github/pull_request_template.md` |
| Instala o hook `commit-msg` | `.git/hooks/commit-msg`; se o projeto usa pre-commit, mostra o trecho a acrescentar |
| Grava o manifesto | `.github/dev-workflow-manifest.json`: versão, caminho do skill e hash de cada arquivo gerado. É por ele que `update` e `uninstall` reconhecem o que não foi editado |
| Cria os labels | Tipos, `epic`, `release`, `release:alpha|beta|rc` (um por canal de pré-release), `breaking-change`, `semver:major|minor|patch` |

Opções: `--skill-dir` (onde copiar o skill, relativo à raiz, por exemplo `.agents/skills/dev-workflow`; os workflows e o hook apontam para lá, e o `update` mantém), `--language` (`pt-BR` ou `en`: grava a chave `language` e gera a configuração, os workflows e os modelos nesse idioma; veja [Idioma](#idioma)), `--default-branch`, `--initial-version`, `--no-labels`, `--selftest` (instala também o workflow que roda a suíte do skill), `--dry-run`, `--force` (troca arquivos que já existem; sem ele, nada existente é sobrescrito).

## 2. Ajustar a configuração

Abra `.github/dev-workflow.toml` ([referência](configuration.md)) e confira:

- `[[versioning.files]]`: todo lugar em que o número aparece e deve acompanhar a versão. Teste: `python3 S/release.py check` e `python3 S/release.py bump --to 9.9.9 --dry-run` (lista os arquivos que mudariam).
- `[[versioning.public_surfaces]]`: os caminhos cujo diff interessa a quem usa o projeto (API, CLI, esquema, formato de arquivo).
- `[types]`: os tipos e o nível que cada um pede.
- `release.mode`: `direct`, ou `pull-request` se a branch padrão exige PR.

Depois: `python3 S/bootstrap.py doctor`. Ele confere a configuração, o Python (3.11+, inclusive o que o hook `commit-msg` vai achar no PATH), o conteúdo do hook (desatualizado: `bootstrap.py install-hook` renova), os hooks do Claude Code se configurados (caminho e `node`), o manifesto e os workflows: além de comparar com o modelo, aponta o que contradiz a configuração (scripts chamados em outro caminho, branch padrão fora do `release.yml`, labels de versão que a análise por agente não reconhece) e o arquivo gerado em outro idioma que o da chave `language`.

## 3. Commitar e mesclar

Os workflows só passam a valer quando estão na branch padrão. Commite tudo que o `init` gerou (inclusive a cópia do skill e `.github/dev-workflow-manifest.json`) e mescle.

Cada pessoa instala o hook no próprio checkout, uma vez: `python3 S/bootstrap.py install-hook` (ou `pre-commit install`).

## 4. Passos de quem é dono do repositório (uma vez)

1. **Tag de base.** Diz "tudo até aqui é a versão X": `python3 S/release.py baseline --push`, na branch padrão já com o fluxo mesclado. Sem ela, a primeira release consideraria o histórico inteiro.
2. **Permissões do Actions.** Os workflows declaram o mínimo de que precisam, e todo job tem `timeout-minutes` (um job pendurado não segura a fila nem a concorrência); em "Settings → Actions → General → Workflow permissions" o padrão "Read" basta. No modo `pull-request`, marque "Allow GitHub Actions to create and approve pull requests".
   - `release.yml` só pede `actions: write` quando `release.dispatch_workflow` está configurado (é o que dispara o workflow de publicação). Configurou ou tirou depois do `init`? Rode o `update` a partir do clone do skill para regenerar; o `doctor` aponta o `release.yml` sem a permissão.
   - Na análise por agente (`semver-analysis.yml`), o agente só lê o git e grava `semver-analysis.json`, com limite de turnos. O passo que grava no PR, o único com escrita, roda uma cópia dos scripts e da configuração feita antes do agente, fora do workspace e sem permissão de escrita: nada que o agente possa ter escrito é executado, e do workspace só o JSON é lido, como dado.
3. **Proteção de branch (recomendado).** Marque o job de `pr-check.yml` como check obrigatório da branch padrão: "Verificação do PR" em `pt-BR`, "PR check" em `en` (o fim do `doctor` diz qual; quando o token pode ler a proteção, ele também confere). Com fila de merge (merge queue), o mesmo check vale: `pr-check.yml` responde ao evento `merge_group` e reporta sucesso sem refazer a verificação, que o PR já passou para entrar na fila. Atenção ao modo de release:
   - `direct` precisa que o workflow consiga fazer push na branch. Com proteção que exige PR, ou troque para `pull-request`, ou crie o segredo `RELEASE_TOKEN` com um token (fine-grained PAT ou GitHub App) de quem pode ignorar a proteção.
     Se o push for recusado (proteção de branch, regra do repositório ou permissão do token: o git responde `GH006`, `protected branch`, `403`...), a release para na primeira tentativa, sem publicar nada, com a mensagem "o push do commit de release para `<branch>` foi recusado pelo GitHub", a saída do git e as duas saídas acima. Só um push recusado porque a branch andou faz a release recalcular e tentar de novo.
   - `pull-request`: o PR de release é aberto pelo token do Actions, que não dispara os checks. Com checks obrigatórios, use `RELEASE_TOKEN` (aí os checks rodam) ou feche e reabra o PR.
     O PR de release é refeito a cada push na branch de destino, mas pode ser mesclado antes de o workflow terminar de refazê-lo. Mesclado por **merge commit**, nada se perde: a tag fica no commit de release, e o PR que entrou nesse intervalo sai na versão seguinte. Mesclado por **squash ou rebase**, o git não distingue o que entrou depois, e esse PR fica na versão sem linha no changelog; nesse caso marque "Require branches to be up to date before merging" na proteção da branch.
     A branch do PR de release (`release.pr_branch`, por padrão `release/next`) nunca publica, mesmo casando com `release/*`: um push nela feito com `RELEASE_TOKEN` dispara o workflow, que termina sem fazer nada. Uma branch de manutenção não pode ter esse nome.
4. **Segredos opcionais.**
   - `RELEASE_TOKEN`: escrever em branch protegida e fazer a tag disparar outros workflows.
   - `ANTHROPIC_API_KEY` ou `CLAUDE_CODE_OAUTH_TOKEN`: liga a análise de versão por agente no CI. Sem eles o workflow pula os passos.
5. **Publicação de artefatos (opcional).** Aponte `release.dispatch_workflow` para um workflow com `workflow_dispatch` e os inputs `version` e `tag`. Ele dispara também nas pré-releases; para tratá-las de outro jeito (dist-tag do npm, por exemplo), veja [configuração](configuration.md#publicar-artefatos-final-ou-pré-release).

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
| `git commit --no-verify`/`-n`, `git -c core.hooksPath=… commit`, commit sem o hook `commit-msg` instalado num repositório que adotou o fluxo | Comando recusado, com a instrução |
| `gh pr create`/`gh pr new` com `--fill` sem corpo, ou com corpo sem `Closes`/`Refs #N` | Comando recusado, com a instrução |

O comando é separado em argumentos como o shell faria: aspas e escapes são respeitados, e cada trecho de `;`, `&&`, `||` e `|` é conferido sozinho. Por isso `git commit -m "remove -n flag"` passa e `git commit -m "a;b" --no-verify` é recusado. O corpo do PR vem de `-b`/`--body`, de `-F`/`--body-file arquivo` (ou `--body-file=arquivo`) ou, com `--body-file -`, do heredoc, do `< arquivo` ou do comando ligado por `|`. O repositório adotou o fluxo quando o `HEAD` versiona `.github/dev-workflow.toml`, `dev-workflow.toml` ou `.dev-workflow.toml`.

São lembretes e guardas de texto; as barreiras são o hook `commit-msg` e o check do PR. Um erro interno do hook nunca bloqueia o comando.

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
| Cópia do skill | No caminho registrado no manifesto (`.claude/skills/dev-workflow/` sem ele). Trocada inteira: arquivos novos entram, os que saíram da versão nova são removidos |
| Workflows, formulários de issue e modelo de PR | Gerados no idioma da chave `language`. Não editado recebe o modelo novo: o hash bate com o do manifesto, mesmo que a configuração (label, branch) tenha mudado o que o modelo renderiza. Os personalizados ficam como estão, com aviso (`--force` troca). Opcional ausente (`semver-analysis.yml`, autoteste) continua ausente |
| Arquivo que a versão nova deixou de gerar | Removido se não foi editado; personalizado fica, com aviso |
| Hook `commit-msg` deste checkout | Renovado se é o do skill; hook de outra origem não é tocado. Os outros checkouts renovam com `bootstrap.py install-hook` |
| `.github/dev-workflow-manifest.json` | Reescrito com a versão nova e os hashes do que foi gerado |
| `.github/dev-workflow.toml` | Nunca é trocado. Chave nova tem padrão; chave removida aparece como erro no `doctor` |
| Versão mais antiga que a instalada | Recusada sem `--force` |

É tudo ou nada: a cópia nova é montada ao lado antes de tocar no projeto, e uma falha no meio (disco, permissão) devolve a cópia anterior e os arquivos como estavam. Projeto instalado antes do manifesto não tem como saber o que foi gerado: o `update` compara com o modelo da versão instalada renderizado com a configuração de hoje (se ela mudou, o arquivo parece personalizado) e grava o manifesto, que vale a partir do próximo.

Rodar o `update` a partir da cópia instalada é recusado: ela não é uma versão nova. Commite o resultado numa branch de issue (`chore`), como qualquer mudança; os workflows novos valem depois do merge. Mudou só a configuração (label de release, branch padrão)? Rode o `update` a partir do clone da mesma versão: ele regenera o que não foi editado.

## Idioma

O idioma do fluxo é a chave `language` da configuração ([referência](configuration.md#language)): `pt-BR` quando ela falta, ou `en`. Num projeto novo, escolha no `init`:

```bash
python3 ~/src/dev-workflow/scripts/bootstrap.py init --language en
```

Ele grava `language = "en"` em `.github/dev-workflow.toml` e gera em inglês a configuração comentada, os formulários de issue, o modelo de PR e os nomes de exibição dos workflows; o manifesto registra o idioma. Sem `--language` e sem configuração, tudo sai como antes da chave existir, em português. Num projeto que já tem configuração, `--language` tem de ser o idioma dela: outro valor é recusado antes de qualquer arquivo ser tocado (para recomeçar a configuração, `--force`).

O `update` segue sempre a chave e nunca troca o idioma sozinho: com a chave igual, os arquivos são regenerados no mesmo idioma e o check obrigatório mantém o nome.

### Trocar o idioma de um projeto

1. Troque a chave em `.github/dev-workflow.toml` (por exemplo `language = "en"` → `language = "pt-BR"`). O `update` nunca mexe nesse arquivo: os comentários dele ficam no idioma em que foi gerado.
2. `python3 S/bootstrap.py doctor` mostra, como pendência, cada arquivo que ainda é o modelo do outro idioma e o check obrigatório que vai mudar de nome (`PR check` → `Verificação do PR`).
3. Rode o `update` a partir do clone do skill (`--dry-run` antes). Os workflows, formulários e o modelo de PR que não foram editados saem no idioma novo; ele é reconhecido pelo manifesto ou, num projeto sem manifesto, pelo nome do check. O personalizado fica, com aviso, como em qualquer `update`. A saída diz `idioma do fluxo: en → pt-BR` e avisa a troca do check obrigatório.
4. **Proteção de branch, à mão.** O nome do job de `pr-check.yml` é o nome do check obrigatório. No PR da atualização o check já roda com o nome novo, e o antigo não aparece mais: troque um pelo outro na proteção da branch padrão (Settings → Branches ou Rulesets) antes de mesclar esse PR.
5. `doctor` de novo: sem pendência de idioma. Labels e tags não mudam; os nomes dos labels não dependem do idioma.

Issues e PRs abertos antes da troca continuam passando no check: os nomes de seção padrão valem nos dois idiomas (`## Versão` e `## Version`). Veja os limites em [configuração](configuration.md#language).

## Desinstalar

```bash
python3 .claude/skills/dev-workflow/scripts/bootstrap.py uninstall        # lista o que sai e o que fica
python3 .claude/skills/dev-workflow/scripts/bootstrap.py uninstall --yes  # remove
```

Sai o que o fluxo gerou e não foi editado (pelo hash do manifesto ou, sem ele, pela comparação com o modelo): a cópia do skill, os workflows, os formulários de issue, o modelo de PR, a configuração se ainda é a que o `init` gerou, o manifesto e o hook `commit-msg` deste checkout. Fica, listado: o que foi personalizado, a configuração editada, o trecho do pre-commit e os hooks do Claude Code (tire à mão), os labels no GitHub, as tags de versão e o changelog, que são o histórico. No repositório do próprio skill, o skill fica. Commite a remoção numa branch de issue.

## Conferir que funciona

```bash
python3 .claude/skills/dev-workflow/tests/run.py
```

A suíte cria repositórios git temporários fora do projeto e simula o GitHub; não toca o repositório nem a rede.
