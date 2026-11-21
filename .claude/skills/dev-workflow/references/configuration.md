# Configuração: `.github/dev-workflow.toml`

Os scripts procuram, nesta ordem: a variável `DEV_WORKFLOW_CONFIG`, `.github/dev-workflow.toml`, `dev-workflow.toml`, `.dev-workflow.toml`. Sem arquivo, valem os padrões (suficientes para o hook `commit-msg`). As tabelas do arquivo se sobrepõem aos padrões chave a chave; `types`, `labels.extra`, `issues.sections`, `commits.levels` e todas as listas substituem o padrão por inteiro. Valor inválido é recusado com a chave na mensagem. Modelo comentado: `assets/dev-workflow.toml` (em inglês, `assets/locales/en/dev-workflow.toml`).

**Validação.** A configuração é conferida na carga, antes de qualquer comando: chave desconhecida é erro e a mensagem sugere a chave mais parecida (`pr.severity.titel` → `title`); valor de tipo errado é erro com a chave (`exempt_authors = "dependabot[bot]"` precisa ser lista); `search` de `[[versioning.files]]` precisa de um marcador `{current…}` e `regex` precisa compilar. Sem `pr.title_pattern` explícito, um projeto que redefine `[types]` aceita no título do PR os prefixos de todos os tipos, mais `revert`: um tipo novo com prefixo `hotfix` vale sem mexer no padrão. Uma issue citada por um PR sem exatamente um label de tipo aparece como nota no plano de versão, porque o tipo dela não conta.

## `schema`

Sempre `1`.

## `language`

Idioma dos padrões e das mensagens: `"pt-BR"` (vale quando a chave falta) ou `"en"`. É uma chave da raiz do arquivo, antes da primeira tabela. Valor desconhecido é recusado.

O idioma troca os **padrões** abaixo. O que o projeto escreve no arquivo continua valendo por cima deles, chave a chave.

| Padrão | `pt-BR` | `en` |
|---|---|---|
| `issues.items_section` | `"Itens"` | `"Items"` |
| `issues.depends_marker` | `"depende de"` | `"depends on"` |
| `issues.acceptance_section` | `"Critérios de aceite"` | `"Acceptance criteria"` |
| `issues.sections` | Contexto, Objetivo, Escopo… | Context, Goal, Scope… |
| `pr.required_sections` | Issues, Resumo, Testes executados, Documentação, Versão | Issues, Summary, Tests run, Documentation, Version |
| `pr.version_section`, `pr.tests_section`, `pr.docs_section` | `"Versão"`, `"Testes executados"`, `"Documentação"` | `"Version"`, `"Tests run"`, `"Documentation"` |
| `pr.override_reason_marker` | `"Motivo da sobreposição"` | `"Override reason"` |
| `changelog.intro` e os títulos de `changelog.sections` | Funcionalidades, Correções… | Features, Fixes… |
| `description` de cada tipo e dos labels do fluxo | português | inglês |

Não mudam com o idioma: os nomes dos labels, os prefixos de commit, o assunto do commit de release, as marcas em comentário HTML e as opções de linha de comando.

O idioma também decide o que o `bootstrap.py` gera ([instalação](installation.md#idioma)):

- os formulários de issue, o modelo de PR e a configuração inicial vêm de `assets/locales/<idioma>/` (os de `pt-BR` continuam em `assets/`), com os rótulos iguais aos nomes de seção da tabela acima;
- os workflows têm um modelo só; mudam os nomes de exibição (do workflow, dos jobs e dos passos), as descrições das entradas manuais e as mensagens dos passos. O nome do job de `pr-check.yml` é o check obrigatório da proteção de branch: `Verificação do PR` em `pt-BR`, `PR check` em `en`.

**Nomes de seção equivalentes.** O check do PR e a leitura das issues aceitam o nome de uma seção padrão em qualquer idioma: um PR com `## Version` passa num projeto em `pt-BR`, e uma issue com `## Critérios de aceite` vale num projeto em `en`. A equivalência sai da posição de cada nome nos padrões dos idiomas (`issues.sections`, `pr.required_sections`, `issues.acceptance_section`, `pr.version_section`, `pr.tests_section`, `pr.docs_section`, `issues.items_section`, `issues.depends_marker`, `pr.override_reason_marker`), então vale para os nomes padrão; uma seção própria do projeto só casa com o próprio nome. Por isso trocar o idioma não quebra as issues e os PRs abertos antes da troca.

Limites desta versão:

- Com `en`, saem em inglês os padrões da tabela, o que o `bootstrap.py` gera e escreve e as mensagens que já estão no catálogo (`scripts/devflow/locales/`). As mensagens que ainda não estão nele saem em português.
- A equivalência também vale para o marcador do motivo de sobreposição (`Motivo da sobreposição` e `Override reason`), para a seção de itens de um epic (`Itens` e `Items`, lida por `sync-epic` e `sync-checklist`) e para o marcador de dependência (`depende de` e `depends on`): um epic aberto antes da troca continua sendo lido.
- Os comentários dos workflows gerados e o `SKILL.md` e os documentos de `references/` continuam em português.

## `[repository]`

| Chave | Padrão | Significado |
|---|---|---|
| `default_branch` | `"main"` | Base das branches de trabalho e do PR de release |
| `release_branches` | `["main", "release/*"]` | Padrões (glob) das branches em que `release.py run` publica. Se mudar, ajuste também o gatilho de `release.yml` |

## `[types.<label>]`

Um bloco por tipo de issue. O nome do bloco é o label.

| Chave | Significado |
|---|---|
| `color`, `description` | Usados por `sync-labels` |
| `prefixes` | Prefixos de commit e de branch aceitos; o primeiro é o usado por `github_issues.py start` |
| `level` | `none`, `patch`, `minor` ou `major`: o incremento que uma issue deste tipo pede |
| `section` | `id` de uma seção de `changelog.sections` |

## `[labels]`

| Chave | Padrão |
|---|---|
| `epic` | `"epic"` |
| `release` | `"release"`: o label que faz o merge publicar a versão final |
| `prerelease_prefix` | `"release:"` (gera um label por canal de `versioning.prerelease.channels`: `release:alpha`, `release:beta`, `release:rc`). O merge de um PR com um deles publica a pré-release do canal |
| `breaking` | `"breaking-change"` |
| `override_prefix` | `"semver:"` (gera `semver:major`, `semver:minor`, `semver:patch`) |
| `[labels.extra.<nome>]` | Labels adicionais que `sync-labels` cria (`color`, `description`) |

Um label de canal não pode ser igual a outro label do fluxo: a configuração é recusada. Um label com o prefixo e um sufixo que não é canal (`release:nightly`) não pede versão; o check do PR e o plano avisam.

Se trocar o label de release ou o prefixo dos canais, ajuste a condição `if:` de `semver-analysis.yml`: à mão, ou com `bootstrap.py update --force`, que refaz os workflows com os labels da configuração (e desfaz o que foi personalizado neles).

## `[commits.levels]`

Nível por tipo de Conventional Commits. Padrão: `feat = "minor"`, `fix = "patch"`, `perf = "patch"`. Tipos fora da tabela não pedem incremento.

## `[issues]`

| Chave | Padrão | Significado |
|---|---|---|
| `items_section` | `"Itens"` | Seção do epic com a lista de filhas |
| `depends_marker` | `"depende de"` | Texto que precede as dependências numa linha da lista |
| `acceptance_section` | `"Critérios de aceite"` | Seção cujas caixas o check e as evidências contam |
| `[issues.sections]` | — | Seções obrigatórias por tipo: `default = [...]` e, se quiser, uma lista por label (`bug = [...]`) |

## `[pr]`

| Chave | Padrão | Significado |
|---|---|---|
| `required_sections` | `["Issues", "Resumo", "Testes executados", "Documentação", "Versão"]` | Títulos `##` que o corpo do PR precisa ter, preenchidos |
| `version_section` | `"Versão"` | Onde fica o motivo da sobreposição |
| `tests_section`, `docs_section` | `"Testes executados"`, `"Documentação"` | Copiadas para o comentário de evidências |
| `override_reason_marker` | `"Motivo da sobreposição"` | Início da linha com o motivo |
| `title_pattern` | Conventional Commits | Expressão regular do título |
| `exempt_authors` | `["dependabot[bot]"]` | Autores isentos do check e das evidências |
| `[pr.severity]` | — | `error`, `warning` ou `off` para: `sections`, `title` (padrão `error`), `unchecked_criteria`, `issue_sections`, `type_mismatch` (padrão `warning`) |

## `[versioning]`

| Chave | Padrão | Significado |
|---|---|---|
| `enabled` | `true` | `false` desliga prévia, sobreposição e release; o fluxo de issues continua |
| `tag_prefix` | `"v"` | Tags de versão são `<prefixo><semver>` |
| `initial_version` | `"0.1.0"` | Versão corrente enquanto não existe tag |
| `zero_major` | `"breaking-is-minor"` | Em 0.x, quebra sobe o minor; `"strict"` a leva a 1.0.0 |
| `require_analysis` | `false` | `true`: mudança em superfície pública só sai com análise registrada ou sobreposição |
| `trusted_bots` | `["github-actions[bot]", "claude[bot]"]` | Bots cujos comentários de análise e de marcação valem |

### `[[versioning.files]]`

Um bloco por substituição; o mesmo arquivo pode ter vários.

| Chave | Significado |
|---|---|
| `path` | Caminho a partir da raiz do repositório |
| `search` | Texto literal a procurar, com marcadores. Ex.: `'version = "{current}"'` |
| `regex` | Alternativa a `search`: expressão regular (os marcadores entram escapados). Use quando o texto não contém a versão corrente inteira, como uma faixa de compatibilidade |
| `replace` | Texto que entra no lugar. Opcional com `search` (o padrão troca `{current…}` por `{new…}`); obrigatório com `regex`. É literal: sem referências a grupos |
| `count` | Ocorrências esperadas (padrão 1). Número diferente é erro, de propósito: um padrão que casa demais reescreve o que não devia |

Marcadores: `{current}` e `{new}` (SemVer completo) e, para cada um, `_pep440` (`1.2.0rc1`), `_core` (`1.2.0`), `_major`, `_minor`, `_patch`, `_next_minor` (`1.3.0`), `_next_major` (`2.0.0`), `_prerelease` (o sufixo de pré-release, `rc.1`) e `_channel` (o canal, `rc`); mais `{date}`. `_prerelease` e `_channel` ficam vazios numa versão final: servem a arquivos que guardam o sufixo separado do número.

`release.py check` reprova quando reescrever para a versão corrente mudaria algum arquivo: é a detecção de edição manual. No check do PR (`--tree`), a versão corrente é a de que o PR partiu (a maior tag alcançável da ponta dele), para que um PR aberto antes da última release não seja reprovado por estar atrás da base.

Exemplos:

```toml
[[versioning.files]]            # Python
path = "pyproject.toml"
search = 'version = "{current_pep440}"'

[[versioning.files]]            # Node (inclua o package-lock.json se ele carrega a versão do pacote)
path = "package.json"
search = '"version": "{current}"'

[[versioning.files]]            # Rust
path = "Cargo.toml"
search = 'version = "{current}"'

[[versioning.files]]            # .NET
path = "Directory.Build.props"
search = "<Version>{current}</Version>"

[[versioning.files]]            # .NET com o sufixo à parte: <VersionSuffix>beta.1</VersionSuffix>, vazio na final
path = "Directory.Build.props"
search = "<VersionSuffix>{current_prerelease}</VersionSuffix>"

[[versioning.files]]            # Go: constante no código; a versão do módulo é a própria tag
path = "internal/version/version.go"
search = 'const Version = "{current}"'

[[versioning.files]]            # faixa presa ao minor corrente
path = "plugin/manifest.json"
regex = '"host": ">=\d+\.\d+\.\d+,<\d+\.\d+\.\d+"'
replace = '"host": ">={new_major}.{new_minor}.0,<{new_next_minor}"'
```

### `[[versioning.public_surfaces]]`

| Chave | Significado |
|---|---|
| `name` | Nome mostrado nos relatórios |
| `globs` | Padrões de caminho, relativos à raiz do repositório e com `/`. `*`, `?` e `[...]` ficam dentro de um nível de diretório; `**/` vale zero ou mais diretórios e `dir/**` cobre tudo abaixo de `dir`. Exemplos: `scripts/*.py` não cobre `scripts/sub/x.py`; `**/*.md` cobre `README.md` e `docs/a/b.md`; `*.md` cobre só a raiz |
| `on_add`, `on_modify`, `on_remove` | `none`, `patch`, `minor`, `major` ou `review`. Padrão: `review`, `review`, `major`. `review` não muda o nível; marca a mudança como "exige análise" |

### `[[versioning.floors]]`

Versões que o código declara como "disponível desde". A release nunca sai com número menor que a maior delas.

| Chave | Significado |
|---|---|
| `globs` | Arquivos versionados a ler, com a mesma sintaxe das superfícies públicas |
| `regex` | Expressão com um grupo de captura que contém a versão |

### `[versioning.prerelease]`

| Chave | Padrão | Significado |
|---|---|---|
| `channels` | `["alpha", "beta", "rc"]` | Canais de pré-release aceitos, do menos ao mais maduro. Cada um tem um label (`labels.prerelease_prefix` + canal) e pode ser pedido por `release.py run --prerelease`; canal fora da lista é recusado |

A lista é conferida ao carregar a configuração:

- Nomes em minúsculas, começando por letra (letras, dígitos e hífen).
- A ordem é a de precedência do SemVer, que compara os canais em ordem alfabética (ASCII). `["beta", "alpha"]` é recusado, e `snapshot` não cabe antes de `rc`: todo gerenciador de pacote trataria `1.3.0-snapshot.1` como mais novo que `1.3.0-rc.1`.
- Se algum `[[versioning.files]]` usa um marcador `_pep440`, cada canal precisa ter forma PEP 440 (`alpha` ou `a` → `a`; `beta` ou `b` → `b`; `rc`, `c`, `pre` ou `preview` → `rc`), e dois canais não podem dar a mesma forma. Sem isso, `1.3.0-dev.1` iria para um `pyproject.toml`, onde não é uma versão válida.
- Lista vazia desliga as pré-releases.

## `[changelog]`

| Chave | Padrão | Significado |
|---|---|---|
| `path` | `"CHANGELOG.md"` | Vazio desliga o arquivo (as notas da GitHub Release continuam) |
| `title`, `intro` | — | Cabeçalho escrito quando o arquivo é criado |
| `sections` | incompatíveis, funcionalidades, melhorias, correções, documentação, manutenção | Lista ordenada de `{ id, title }`. Os ids `breaking` e `maintenance` são obrigatórios |
| `hide` | `[]` | Ids de seções que ficam fora do arquivo; as notas da release mostram todas |
| `prereleases` | `false` | `true` escreve também as pré-releases no arquivo. Cada versão escreve uma seção com a lista inteira desde a última versão final e tira as seções das pré-releases anteriores do ciclo; depois da versão final, o arquivo fica só com a seção dela. O link do título de uma pré-release compara com a tag anterior |

## `[release]`

| Chave | Padrão | Significado |
|---|---|---|
| `mode` | `"direct"` | `"direct"` commita na branch; `"pull-request"` abre um PR de release |
| `commit_message` | `"chore(release): {tag}"` | Assunto do commit de release. É por ele que a automação reconhece uma release preparada; não mude com release em andamento |
| `pr_branch` | `"release/next"` | Branch do PR de release (modo `pull-request`) |
| `mark` | `["comment", "milestone"]` | Como marcar PRs e issues incluídos |
| `dispatch_workflow` | `""` | Arquivo de workflow a disparar depois da release, com os inputs `version` e `tag`. Dispara também nas pré-releases |

### Publicar artefatos: final ou pré-release

O job `release` do workflow "Versão e release" expõe saídas para os jobs seguintes do mesmo workflow:

| Saída | Valor |
|---|---|
| `released` | `true` quando esta execução publicou uma versão |
| `version`, `tag` | `1.3.0-beta.1` e `v1.3.0-beta.1` (no modo `pull-request`, a versão do PR de release aberto, com `released=false`) |
| `prerelease` | `true` numa pré-release, `false` numa versão final |
| `channel` | `alpha`, `beta`, `rc`; vazio numa versão final |

O workflow de `dispatch_workflow` recebe só `version` e `tag` (o `workflow_dispatch` recusa entrada não declarada) e deriva o resto da versão. Para o npm, por exemplo, a pré-release vai para a dist-tag do canal, e só a versão final vira `latest`:

```yaml
on:
  workflow_dispatch:
    inputs:
      version: { required: true, type: string }
      tag: { required: true, type: string }
jobs:
  publish:
    runs-on: ubuntu-24.04
    steps:
      - uses: actions/checkout@v7
        with: { ref: "${{ inputs.tag }}" }
      - env:
          VERSION: ${{ inputs.version }}
        run: |
          case "$VERSION" in
            *-*) dist="${VERSION#*-}"; dist="${dist%%.*}" ;;   # 1.3.0-beta.1 → beta
            *)   dist="latest" ;;
          esac
          npm publish --tag "$dist"
```

## Variáveis de ambiente

| Variável | Uso |
|---|---|
| `DEV_WORKFLOW_CONFIG` | Caminho alternativo da configuração |
| `DEV_WORKFLOW_OFFLINE=1` | Não consulta o GitHub: o plano sai só do histórico do git |
| `DEV_WORKFLOW_GH` | Lista JSON com o comando que substitui `gh` (usada pelos testes) |
| `DEV_WORKFLOW_GH_MODULE` | Só para os testes: arquivo Python cujo `main(argv)` responde no lugar do `gh`, no mesmo processo; tem precedência sobre `DEV_WORKFLOW_GH` |
| `DEV_WORKFLOW_TIMEOUT` | Tempo limite, em segundos, de cada chamada a `git` e `gh` (padrão 120). Uma chamada que passa dele termina com erro que diz o comando e o tempo |
| `DEV_WORKFLOW_RETRIES` | Tentativas de uma chamada à API em falha passageira, contando a primeira (padrão 4). Repete leituras e escritas idempotentes (`GET`, `PATCH`, `PUT`, `DELETE`) em 5xx, 429 e limite secundário, esperando 2, 4, 8… segundos, até 30. Criação (`POST` de comentário, release, PR) não é repetida: a reexecução confere antes de criar |
| `GH_HOST` | Servidor do GitHub Enterprise: o remoto `origin` desse servidor também identifica o repositório (como `GITHUB_SERVER_URL` no Actions) |
| `RELEASE_FORCE`, `RELEASE_LEVEL`, `RELEASE_REASON`, `RELEASE_PRERELEASE`, `RELEASE_DRY_RUN`, `RELEASE_BRANCH` | Equivalentes das opções de `release.py run`, preenchidas pelo workflow |
