#!/usr/bin/env bash
# Repositório com a versão v1.4.0 publicada e duas mudanças pendentes, nenhuma pedindo release:
# um `fix` e um `feat`, que juntos pedem 1.5.0. Sem remoto e sem gh: o cálculo usa só o git.
set -eu
here="$(dirname "$0")"
cp -R "$here/files/." .
git init -q -b main
git config user.name "Dev"
git config user.email "dev@example.com"
git config commit.gpgsign false
git config tag.gpgsign false
git config core.autocrlf false
git add -A
git commit -q -m "chore: start the project"
git tag -a v1.4.0 -m "v1.4.0"
cp -R "$here/history/1-fix/." .
git commit -q -am "fix(api): skip deleted records in fetch" -m "Refs #80"
cp -R "$here/history/2-feat/." .
git commit -q -am "feat(api): add Client.count" -m "Refs #81"
