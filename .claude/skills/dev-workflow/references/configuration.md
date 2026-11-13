# Configuração (`.github/dev-workflow.toml`)

Tudo que é específico do projeto fica neste arquivo; `bootstrap.py init` o cria a partir de `assets/dev-workflow.toml`. Os scripts recusam chave desconhecida, tipo errado e valor fora do domínio com uma mensagem que começa pela chave (`types.bug.color: esperado hexadecimal de seis dígitos.`); nada vira default em silêncio.

| Chave | Tipo | Padrão | Uso |
|---|---|---|---|
| `project.default_branch` | texto | `main` | Base das branches de `start`, alvo dos PRs e branch das releases |
| `project.language` | texto | `pt` | Idioma de issues, PRs e comentários |
| `types.<label>.color`, `.description` | texto | — | Label de tipo; cada issue tem exatamente um. Pelo menos um tipo é obrigatório |
| `types.<label>.commit` | lista de textos | `[]` | Tipos de Conventional Commits do label; o primeiro dá o prefixo da branch de `start`. Um tipo de commit pertence a um label só |
| `types.<label>.level` | `patch`, `minor`, `major` | `patch` | Nível SemVer que uma mudança desse tipo implica numa release |
| `labels.<label>.color`, `.description` | texto | — | Labels adicionais (`security`, `epic`, `release`, `semver:*`); não podem repetir um tipo |
| `issue.epic_label` | texto | `epic` | Label da issue de acompanhamento de um plano |
| `issue.items_section` | texto | `Itens` | Título da seção do epic com a lista `- [ ] #N título — depende de #M` |
| `issue.depends_marker` | texto | `depende de` | Expressão que, num item, introduz as dependências |
| `issue.sections` | lista de textos | `[]` | Seções que toda issue tem; o check de PR avisa quando faltam |
| `pull_request.sections` | lista de textos | `[]` | Seções do corpo do PR que o check exige preenchidas |
| `pull_request.exempt_authors` | lista de textos | `["dependabot[bot]"]` | Autores cujos PRs o check não exige vínculo |
| `pull_request.conventional_title` | booleano | `true` | Título do PR em Conventional Commits |
| `pull_request.pending_criteria` | `warning`, `error` | `warning` | Critério de aceite não marcado na issue fechada pelo PR |
| `versioning.*` | tabela | desligado | Versionamento semântico e release |

Caminhos em `versioning.files` e `versioning.floors` são relativos à raiz do repositório e não podem sair dela (`..`).
