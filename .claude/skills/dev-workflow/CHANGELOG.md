# Changelog

As versões depois da tag de base são escritas pelo workflow de release (`scripts/release.py run`): não edite as seções de versão à mão.

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
