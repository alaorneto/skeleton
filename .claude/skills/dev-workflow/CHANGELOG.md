# Changelog

As versões depois da tag de base são escritas pelo workflow de release (`scripts/release.py run`): não edite as seções de versão à mão.

## [1.2.1](https://github.com/alaorneto/dev-workflow/compare/v1.2.0...v1.2.1) — 2026-11-21

### Correções

- Rodar a suíte do skill também na cópia instalada num projeto ([#60](https://github.com/alaorneto/dev-workflow/issues/60)) — PR [#61](https://github.com/alaorneto/dev-workflow/pull/61)

## [1.2.0](https://github.com/alaorneto/dev-workflow/compare/v1.1.0...v1.2.0) — 2026-11-21

### Funcionalidades

- Oferecer os modelos em inglês e aceitar nomes de seção equivalentes ([#41](https://github.com/alaorneto/dev-workflow/issues/41)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)

### Melhorias

- Dar tempo limite e nova tentativa às chamadas ao GitHub ([#26](https://github.com/alaorneto/dev-workflow/issues/26)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Recusar chave desconhecida e tipo errado na configuração ([#27](https://github.com/alaorneto/dev-workflow/issues/27)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Desconsiderar mudança revertida antes da release ([#28](https://github.com/alaorneto/dev-workflow/issues/28)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Dar ao bootstrap caminho configurável, manifesto e desinstalação ([#29](https://github.com/alaorneto/dev-workflow/issues/29)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Dar --help à CLI de issues e precisão a start e commit-msg ([#30](https://github.com/alaorneto/dev-workflow/issues/30)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Emitir em inglês as mensagens de issues, PR e epics ([#37](https://github.com/alaorneto/dev-workflow/issues/37)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)
- Emitir em inglês as mensagens de configuração, SemVer e plano ([#38](https://github.com/alaorneto/dev-workflow/issues/38)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)
- Emitir em inglês as mensagens de release ([#39](https://github.com/alaorneto/dev-workflow/issues/39)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)
- Instalar e atualizar o fluxo em dois idiomas ([#40](https://github.com/alaorneto/dev-workflow/issues/40)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)

### Correções

- Ler referências e seções como o GitHub as mostra ([#23](https://github.com/alaorneto/dev-workflow/issues/23)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Mostrar a causa dos erros em vez de traceback ([#24](https://github.com/alaorneto/dev-workflow/issues/24)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Aceitar a análise de versão só para os commits que ela viu ([#25](https://github.com/alaorneto/dev-workflow/issues/25)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Reduzir falsos positivos e negativos do hook do Claude Code ([#31](https://github.com/alaorneto/dev-workflow/issues/31)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Não dizer que abriu o PR de release quando o GitHub está desligado ([#54](https://github.com/alaorneto/dev-workflow/issues/54)) — PR [#59](https://github.com/alaorneto/dev-workflow/pull/59)
- Respeitar o modo offline em sync-labels ([#55](https://github.com/alaorneto/dev-workflow/issues/55)) — PR [#59](https://github.com/alaorneto/dev-workflow/pull/59)
- Dizer que start precisa do GitHub em vez de dizer que a issue não existe ([#56](https://github.com/alaorneto/dev-workflow/issues/56)) — PR [#59](https://github.com/alaorneto/dev-workflow/pull/59)

### Documentação

- Escrever SECURITY.md e CONTRIBUTING.md ([#36](https://github.com/alaorneto/dev-workflow/issues/36)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Documentar o idioma e a troca de idioma ([#42](https://github.com/alaorneto/dev-workflow/issues/42)) — PR [#52](https://github.com/alaorneto/dev-workflow/pull/52)
- Ensinar no README a usar os canais alpha, beta e rc ([#57](https://github.com/alaorneto/dev-workflow/issues/57)) — PR [#59](https://github.com/alaorneto/dev-workflow/pull/59)

### Manutenção

- Endurecer os workflows: tempo limite, permissões, fila de merge e análise isolada ([#32](https://github.com/alaorneto/dev-workflow/issues/32)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Rodar lint, tipos e Python 3.11 a 3.13 no CI e atualizar as actions ([#33](https://github.com/alaorneto/dev-workflow/issues/33)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Cobrir as lacunas da suíte de testes ([#34](https://github.com/alaorneto/dev-workflow/issues/34)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Criar casos de avaliação para as fases sem cobertura ([#35](https://github.com/alaorneto/dev-workflow/issues/35)) — PR [#50](https://github.com/alaorneto/dev-workflow/pull/50)
- Rodar a suíte de testes em até 3 minutos ([#53](https://github.com/alaorneto/dev-workflow/issues/53)) — PR [#59](https://github.com/alaorneto/dev-workflow/pull/59)
- **deps:** Bump anthropics/claude-code-action from 1.0.275 to 1.0.281 in the actions group (PR [#51](https://github.com/alaorneto/dev-workflow/pull/51))

## [1.1.0](https://github.com/alaorneto/dev-workflow/compare/v1.0.4...v1.1.0) — 2026-11-21

### ⚠ Mudanças incompatíveis

- **release:** pre-release channels requested by a PR label (alpha, beta, rc) (PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)) — Acrescenta pré-releases pedidas por label (`release:alpha`, `release:beta`, `release:rc`), a chave `language` e o canal nas saídas e nos marcadores; `run --prerelease` deixa de aceitar canal fora de `versioning.prerelease.channels` (padrão alpha, beta, rc), o que quebra o disparo manual de quem usava outro nome. Dúvida entre minor e major: a quebra só atinge esse disparo e se resolve declarando a lista na configuração.

### Funcionalidades

- Escolher o idioma na configuração: chave language, catálogo e padrões ([#12](https://github.com/alaorneto/dev-workflow/issues/12)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)
- Tornar os canais de pré-release uma lista ordenada e configurável ([#14](https://github.com/alaorneto/dev-workflow/issues/14)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)
- Publicar pré-release pedida por label no PR ([#15](https://github.com/alaorneto/dev-workflow/issues/15)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)

### Melhorias

- Escrever notas e changelog de pré-release sem repetição ([#16](https://github.com/alaorneto/dev-workflow/issues/16)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)
- Expor o canal nas saídas do workflow e nos marcadores de arquivo ([#17](https://github.com/alaorneto/dev-workflow/issues/17)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)

### Correções

- Impedir que a pré-release regrida de canal ou publique abaixo do ciclo em andamento ([#13](https://github.com/alaorneto/dev-workflow/issues/13)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)
- Corrigir as falhas apontadas na revisão dos canais de pré-release ([#48](https://github.com/alaorneto/dev-workflow/issues/48)) — PR [#49](https://github.com/alaorneto/dev-workflow/pull/49)

### Manutenção

- Avaliar a escolha do canal e o encerramento do ciclo ([#18](https://github.com/alaorneto/dev-workflow/issues/18)) — PR [#47](https://github.com/alaorneto/dev-workflow/pull/47)

## [1.0.4](https://github.com/alaorneto/dev-workflow/compare/v1.0.3...v1.0.4) — 2026-11-20

### Correções

- Publicar só o commit de release criado pela automação ([#19](https://github.com/alaorneto/dev-workflow/issues/19)) — PR [#43](https://github.com/alaorneto/dev-workflow/pull/43)
- Corrigir o modo pull-request: não publicar de release/next nem perder mudanças ([#20](https://github.com/alaorneto/dev-workflow/issues/20)) — PR [#44](https://github.com/alaorneto/dev-workflow/pull/44)
- Aceitar PR aberto antes da última release no check de arquivos de versão ([#21](https://github.com/alaorneto/dev-workflow/issues/21)) — PR [#45](https://github.com/alaorneto/dev-workflow/pull/45)
- Impedir que o * dos globs cruze diretórios ([#22](https://github.com/alaorneto/dev-workflow/issues/22)) — PR [#46](https://github.com/alaorneto/dev-workflow/pull/46)

### Documentação

- Reescrever o README em inglês com as regras de negócio do fluxo ([#7](https://github.com/alaorneto/dev-workflow/issues/7)) — PR [#8](https://github.com/alaorneto/dev-workflow/pull/8)

## [1.0.3](https://github.com/alaorneto/dev-workflow/compare/v1.0.2...v1.0.3) — 2026-11-20

### Correções

- Aceitar o PR de release na conferência dos arquivos de versão ([#5](https://github.com/alaorneto/dev-workflow/issues/5)) — PR [#6](https://github.com/alaorneto/dev-workflow/pull/6)

## [1.0.2](https://github.com/alaorneto/dev-workflow/compare/v1.0.1...v1.0.2) — 2026-11-20

### Manutenção

- Formatar os testes de limite das notas em 120 colunas ([#3](https://github.com/alaorneto/dev-workflow/issues/3)) — PR [#4](https://github.com/alaorneto/dev-workflow/pull/4)

## [1.0.1](https://github.com/alaorneto/dev-workflow/compare/v1.0.0...v1.0.1) — 2026-11-20

### Correções

- Limitar as notas de release ao tamanho que o GitHub aceita ([#1](https://github.com/alaorneto/dev-workflow/issues/1)) — PR [#2](https://github.com/alaorneto/dev-workflow/pull/2)

## 1.0.0

Primeira versão publicada como repositório próprio, extraída do projeto Skeleton.

- Fluxo issue-first: plano aceito vira issues e epic com sub-issues e "blocked by" nativos; branch `<tipo>/<N>-<slug>`; hook `commit-msg`; PR com `Closes #N`.
- Verificação do PR com o código da branch base: issue classificada, seções do modelo, título em Conventional Commits, sobreposição de versão, arquivos de versão coerentes; prévia da versão comentada no PR.
- Versionamento semântico automático: só o label `release` publica; nível pelo maior sinal das mudanças acumuladas desde a última tag; política de 0.x; sobreposição com motivo; análise por agente que só eleva.
- Release idempotente depois do merge, nos modos `direct` e `pull-request`: arquivos de versão, `CHANGELOG.md`, tag anotada, GitHub Release, comentários e milestone; pré-release, hotfix em `release/*` e reversão.
- Evidências no merge e fechamento automático de epics; leitura truncada de sub-issues recusada.
- `bootstrap.py` com `init`, `update`, `doctor`, `install-hook` e `version`.
