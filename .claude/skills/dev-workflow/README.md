# dev-workflow

Skill de agente (Claude Code e compatíveis) e ferramentas para um fluxo de desenvolvimento rastreável no GitHub, do plano à release, em projetos de qualquer tecnologia:

- **Issue antes de qualquer edição.** Plano aceito vira issues (e um epic com sub-issues e "blocked by" nativos); pedido direto identifica ou cria a issue.
- **Branch, commits e PR vinculados.** Branch `<tipo>/<N>-<slug>`, commits com `Refs #N` (hook `commit-msg`), PR com `Closes #N` conferido por um check que roda o código da branch base.
- **Versão semântica calculada e publicada sozinha.** Só o label `release` faz o merge publicar uma versão. O nível é o maior entre os sinais de tudo que se acumulou desde a última tag: tipo das issues, Conventional Commits, `breaking-change`, superfícies públicas e a análise do diff pelo agente. O workflow escreve a versão nos arquivos declarados, o `CHANGELOG.md`, a tag e a GitHub Release.
- **Fechamento com evidências.** No merge, cada issue recebe os commits, os testes e a documentação declarados no PR.

Tudo que é específico de um projeto (tipos, seções obrigatórias, arquivos de versão, superfícies públicas, modo de release) fica em `.github/dev-workflow.toml` do projeto. Os scripts usam só a biblioteca padrão do Python 3.11+, `git` e `gh`.

## Conteúdo

| Caminho | O que é |
|---|---|
| `SKILL.md` | O procedimento que o agente segue, fase por fase |
| `references/` | Fluxo e decisões, versionamento, configuração, instalação, modelos de texto |
| `scripts/` | `github_issues.py` (issues, epics, commit-msg, check do PR, evidências), `release.py` (plano, prévia, release), `bootstrap.py` (instalar, atualizar, conferir) |
| `assets/` | Modelos de workflows, formulários de issue, modelo de PR, hook `commit-msg`, configuração inicial, hook do Claude Code |
| `tests/` | Suíte `unittest`; cria repositórios git temporários e simula o GitHub |
| `evals/` | Casos de avaliação do skill (formato de `claude plugin eval`) |
| `VERSION` | Versão deste skill, independente da versão dos projetos que o usam |

## Instalar num projeto

Requisitos: Python 3.11+, `git`, `gh` autenticado; `node` só para o hook opcional do Claude Code.

```bash
git clone https://github.com/<dono>/dev-workflow ~/src/dev-workflow
cd <projeto>
python3 ~/src/dev-workflow/scripts/bootstrap.py init
```

`init` copia o skill para `.claude/skills/dev-workflow/` do projeto (os workflows do CI chamam os scripts de lá, então o skill é versionado junto com o projeto), gera a configuração com os arquivos de versão que reconhecer, os workflows, os modelos de issue e de PR, instala o hook `commit-msg` e cria os labels. Nada que já existe é sobrescrito sem `--force`. O passo a passo, os passos do dono do repositório e o trecho para as instruções do projeto (`AGENTS.md`, `CLAUDE.md`) estão em [references/installation.md](references/installation.md).

No Windows, troque `python3` por `py -3`.

## Atualizar a cópia de um projeto

```bash
cd ~/src/dev-workflow && git pull
cd <projeto>
python3 ~/src/dev-workflow/scripts/bootstrap.py update --dry-run   # mostra o que mudaria
python3 ~/src/dev-workflow/scripts/bootstrap.py update
python3 .claude/skills/dev-workflow/scripts/bootstrap.py doctor
```

`update` troca a cópia em `.claude/skills/dev-workflow/` (remove o que saiu da versão nova), regenera os workflows e modelos que o projeto não personalizou e avisa sobre os personalizados. A configuração do projeto nunca é trocada. Recusa voltar para uma versão mais antiga sem `--force`. Commite o resultado numa branch de issue, como qualquer mudança.

## Versão deste skill

A versão está em `VERSION` e segue SemVer: quebra de compatibilidade é o que obriga um projeto a mexer na própria configuração, nos workflows personalizados ou na forma de chamar os scripts. Este repositório usa o próprio fluxo, com o skill na raiz (`.github/dev-workflow.toml` declara `VERSION` como arquivo de versão): issues, PRs com `Closes #N`, o label `release` para publicar e o `CHANGELOG.md` escrito pelo workflow.

## Desenvolver

```bash
python3 -m unittest discover -s tests          # a suíte inteira (alguns minutos: testes de ponta a ponta com git)
python3 scripts/bootstrap.py doctor            # este repositório conferido pelo próprio skill
```

O workflow "Autoteste do skill" roda a suíte em Linux, Windows e macOS a cada PR. Os casos em `evals/` medem o comportamento do agente com o skill; rode-os com `claude plugin eval` ou com o skill `skill-creator`.

## Primeira publicação deste repositório

O pacote já traz o fluxo adotado (`.github/`: configuração, workflows, formulários e modelo de PR, gerados por `bootstrap.py init` com o skill na raiz). Para criar o repositório:

```bash
cd dev-workflow
git init -b main
git add -A
git commit -m "chore: start the dev-workflow skill repository"
gh repo create <dono>/dev-workflow --private --source . --push
python3 scripts/github_issues.py sync-labels   # labels do fluxo
python3 scripts/bootstrap.py install-hook      # hook commit-msg: daqui em diante, todo commit cita uma issue
python3 scripts/release.py baseline --push     # tag de base v<VERSION>; as próximas versões saem do workflow
python3 scripts/bootstrap.py doctor
```

Se o repositório tiver outro nome ou dono, ajuste o link em `.github/ISSUE_TEMPLATE/config.yml`. Opcional: "Verificação do PR" e "Autoteste do skill dev-workflow" como checks obrigatórios da `main` (veja [references/installation.md](references/installation.md#4-passos-de-quem-é-dono-do-repositório-uma-vez)).

## Licença

Ainda não definida. Escolha uma licença antes de tornar o repositório público.
