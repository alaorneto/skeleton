# Changelog

As versões depois da tag de base são escritas pelo workflow de release (`scripts/release.py run`): não edite as seções de versão à mão.

## 1.0.0

Primeira versão publicada como repositório próprio, extraída do projeto Skeleton.

- Fluxo issue-first: plano aceito vira issues e epic com sub-issues e "blocked by" nativos; branch `<tipo>/<N>-<slug>`; hook `commit-msg`; PR com `Closes #N`.
- Verificação do PR com o código da branch base: issue classificada, seções do modelo, título em Conventional Commits, sobreposição de versão, arquivos de versão coerentes; prévia da versão comentada no PR.
- Versionamento semântico automático: só o label `release` publica; nível pelo maior sinal das mudanças acumuladas desde a última tag; política de 0.x; sobreposição com motivo; análise por agente que só eleva.
- Release idempotente depois do merge, nos modos `direct` e `pull-request`: arquivos de versão, `CHANGELOG.md`, tag anotada, GitHub Release, comentários e milestone; pré-release, hotfix em `release/*` e reversão.
- Evidências no merge e fechamento automático de epics; leitura truncada de sub-issues recusada.
- `bootstrap.py` com `init`, `update`, `doctor`, `install-hook` e `version`.
