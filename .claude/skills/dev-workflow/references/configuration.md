# Configuração: `.github/dev-workflow.toml`

Os scripts procuram, nesta ordem: a variável `DEV_WORKFLOW_CONFIG`, `.github/dev-workflow.toml`, `dev-workflow.toml`, `.dev-workflow.toml`. Sem arquivo, valem os padrões (suficientes para o hook `commit-msg`). As tabelas do arquivo se sobrepõem aos padrões chave a chave; `types`, `labels.extra`, `issues.sections`, `commits.levels` e todas as listas substituem o padrão por inteiro. Valor inválido é recusado com a chave na mensagem. Modelo comentado: `assets/dev-workflow.toml`.

## `schema`

Sempre `1`.

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
| `release` | `"release"`: o único label que faz o merge publicar versão |
| `breaking` | `"breaking-change"` |
| `override_prefix` | `"semver:"` (gera `semver:major`, `semver:minor`, `semver:patch`) |
| `[labels.extra.<nome>]` | Labels adicionais que `sync-labels` cria (`color`, `description`) |

Se trocar o label de release, ajuste a condição `if:` de `semver-analysis.yml` (o `bootstrap.py init --force` regenera).

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

Marcadores: `{current}` e `{new}` (SemVer completo) e, para cada um, `_pep440` (`1.2.0rc1`), `_core` (`1.2.0`), `_major`, `_minor`, `_patch`, `_next_minor` (`1.3.0`), `_next_major` (`2.0.0`); mais `{date}`.

`release.py check` reprova quando reescrever para a versão corrente mudaria algum arquivo: é a detecção de edição manual.

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
| `globs` | Padrões de caminho; `**` cruza diretórios |
| `on_add`, `on_modify`, `on_remove` | `none`, `patch`, `minor`, `major` ou `review`. Padrão: `review`, `review`, `major`. `review` não muda o nível; marca a mudança como "exige análise" |

### `[[versioning.floors]]`

Versões que o código declara como "disponível desde". A release nunca sai com número menor que a maior delas.

| Chave | Significado |
|---|---|
| `globs` | Arquivos versionados a ler |
| `regex` | Expressão com um grupo de captura que contém a versão |

## `[changelog]`

| Chave | Padrão | Significado |
|---|---|---|
| `path` | `"CHANGELOG.md"` | Vazio desliga o arquivo (as notas da GitHub Release continuam) |
| `title`, `intro` | — | Cabeçalho escrito quando o arquivo é criado |
| `sections` | incompatíveis, funcionalidades, melhorias, correções, documentação, manutenção | Lista ordenada de `{ id, title }`. Os ids `breaking` e `maintenance` são obrigatórios |
| `hide` | `[]` | Ids de seções que ficam fora do arquivo; as notas da release mostram todas |
| `prereleases` | `false` | `true` escreve também as pré-releases no arquivo |

## `[release]`

| Chave | Padrão | Significado |
|---|---|---|
| `mode` | `"direct"` | `"direct"` commita na branch; `"pull-request"` abre um PR de release |
| `commit_message` | `"chore(release): {tag}"` | Assunto do commit de release. É por ele que a automação reconhece uma release preparada; não mude com release em andamento |
| `pr_branch` | `"release/next"` | Branch do PR de release (modo `pull-request`) |
| `mark` | `["comment", "milestone"]` | Como marcar PRs e issues incluídos |
| `dispatch_workflow` | `""` | Arquivo de workflow a disparar depois da release, com os inputs `version` e `tag` |

## Variáveis de ambiente

| Variável | Uso |
|---|---|
| `DEV_WORKFLOW_CONFIG` | Caminho alternativo da configuração |
| `DEV_WORKFLOW_OFFLINE=1` | Não consulta o GitHub: o plano sai só do histórico do git |
| `DEV_WORKFLOW_GH` | Lista JSON com o comando que substitui `gh` (usada pelos testes) |
| `RELEASE_FORCE`, `RELEASE_LEVEL`, `RELEASE_REASON`, `RELEASE_PRERELEASE`, `RELEASE_DRY_RUN`, `RELEASE_BRANCH` | Equivalentes das opções de `release.py run`, preenchidas pelo workflow |
