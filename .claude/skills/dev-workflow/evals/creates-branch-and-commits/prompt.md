---
tags: [dev-workflow]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

já fiz a correção da issue #34 em reports/csv_export.py: está na árvore, sem commit, aqui na main. cria a branch da issue, leva essa mudança pra ela e commita. o gh não está autenticado neste ambiente; o que `gh issue view 34 --json number,title,labels,state` responderia está em issue-34.json. não faz push nem abre PR, isso eu faço depois.

Input files: issue-34.json, reports/csv_export.py
