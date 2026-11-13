# Versionamento semântico e release

`scripts/release.py` decide a versão e publica a release depois do merge, sem edição manual. Ligue em `.github/dev-workflow.toml` com `versioning.enabled = true` e declare os arquivos.

## Fonte única

A versão corrente é a **última tag de versão alcançável** a partir do HEAD (`<tag_prefix>X.Y.Z`, por padrão `v`). Os arquivos de `versioning.files` são projeções: a ferramenta os reescreve a cada release, e `release.py check` acusa um arquivo que diverge da tag. Cada entrada usa `search` (texto literal com `{version}`) ou `regex` (com o grupo nomeado `(?P<version>...)`), mais `replace`:

```toml
[versioning]
enabled = true
files = [
  { path = "pyproject.toml", regex = '^version = "(?P<version>[^"]+)"', replace = 'version = "{version}"' },
  { path = "docs/install.md", search = "versão {version}.", replace = "versão {version}." },
]
floors = [{ path = "src/compat.py", regex = 'MINIMUM = "(?P<version>[^"]+)"', replace = 'MINIMUM = "{version}"' }]
public_surfaces = ["src/api/**", "docs/api.md"]
```

`floors` são pisos de versão no código (uma constante "exige o core X.Y.Z"): não acompanham o bump, e `check` só confere que nenhum é maior que a versão publicada.

## Acumulação

Só o label `release` (`versioning.release_label`) marca um PR para versionar. Um PR sem ele é mesclado normalmente e **fica acumulado**: o próximo PR com o label versiona tudo desde a última tag, e os dois entram na mesma versão e no mesmo changelog. Os PRs vêm do histórico (`(#N)` dos squashes e `Merge pull request #N`) e são conferidos na API; as issues vêm do `Closes`/`Refs` de cada PR.

## Nível

O nível é o **maior** entre os sinais:

| Sinal | Nível |
|---|---|
| Label de tipo das issues incluídas | `types.<label>.level` |
| Conventional Commits | o nível do tipo de commit; `!` ou rodapé `BREAKING CHANGE:` é major |
| Label `breaking-change` num PR incluído | major |
| Arquivo de `public_surfaces` alterado | minor |
| Piso de `floors` acima da versão corrente | o nível que o alcança |
| Análise por agente registrada no PR | o nível registrado (só eleva) |

O mínimo é patch. Um label `semver:patch|minor|major` no PR que versiona **sobrepõe** o cálculo; ele exige `Motivo: ...` no corpo do PR, que aparece nas notas. Mais de um `semver:*` é erro.

Em 0.x, uma quebra de compatibilidade sobe o minor; 1.0.0 só sai com `semver:major` (decisão explícita).

A análise por agente é um comentário com `<!-- dev-workflow:semver-analysis level=... -->` (`release.py record-analysis`). Ela só conta se o autor tem permissão de escrita no repositório ou está em `versioning.analysis_authors`; o comentário de qualquer outra pessoa é ignorado. O workflow opcional `semver-analysis.yml` grava essa análise com a action do Claude Code.

## Release

`release.py run` (workflow `release.yml`, no push da branch padrão ou de `maint/**`):

1. Sem o label `release` no PR mesclado, não faz nada.
2. Calcula a versão; tag já existente é erro.
3. Modo `direct`: escreve os arquivos, atualiza `CHANGELOG.md` (agrupado pelo `heading` de cada tipo, com links), cria o commit `chore(release): vX.Y.Z`, a tag anotada e a GitHub Release com as notas, e comenta e põe o milestone `vX.Y.Z` em cada PR e issue incluídos.
4. Modo `pr`: abre o PR `release/vX.Y.Z` com o commit de release; o merge dele dispara de novo e só faltam tag, release e marcação.

O estado vem das tags, do commit de release e da GitHub Release: reexecutar depois de concluir não cria commit, tag nem release; reexecutar depois de uma falha parcial conclui o que faltou.

- **Pré-release:** `run --prerelease rc` (entrada do `workflow_dispatch`) gera `X.Y.Z-rc.N` e marca a GitHub Release como pré-release; a release final do mesmo alvo vem depois, sem o identificador.
- **Manutenção:** numa branch `maint/X.Y`, a última tag alcançável é a da linha de manutenção, e a release sai nela.
- **Reversão:** apague a GitHub Release e a tag (`gh release delete vX.Y.Z --cleanup-tag`) e reverta o commit de release; o intervalo volta sozinho para a tag anterior.

## Passos únicos do responsável

- Tag de base: `release.py baseline X.Y.Z` cria a tag na versão que os arquivos já têm (recusa se divergirem) e é publicada com `git push origin vX.Y.Z`.
- Permissões do Actions: "Read and write permissions" para o `GITHUB_TOKEN`.
- Proteção da branch: se a branch padrão exige PR, o push do commit de release é recusado. Use o modo `pr` ou um token com permissão de bypass num segredo, no lugar do `GITHUB_TOKEN` do workflow.
