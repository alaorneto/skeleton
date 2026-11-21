#!/usr/bin/env bash
# Repositório com a main commitada e a correção da issue #34 na árvore, sem commit. issue-34.json fica
# fora do git (.git/info/exclude): é só o que `gh issue view` diria, porque o gh não está aqui.
set -eu
here="$(dirname "$0")"
cp -R "$here/files/." .
git init -q -b main
git config user.name "Dev"
git config user.email "dev@example.com"
git config commit.gpgsign false
git config core.autocrlf false
git add -A
git commit -q -m "feat(reports): export the monthly report as CSV"
cp -R "$here/change/." .
cp "$here/context/issue-34.json" .
echo "issue-34.json" >> .git/info/exclude
