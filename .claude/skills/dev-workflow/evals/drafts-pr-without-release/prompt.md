---
tags: [dev-workflow]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

terminei a correção da issue #12 (bug: o relatório mensal somava pedidos cancelados). foi um commit só, `fix(reports): ignore cancelled orders in the monthly total`, na branch fix/12-relatorio-cancelados. rodei `make test` e passaram 148 testes, e acrescentei um teste de regressão em tests/test_reports.py. não mexi em documentação porque o comportamento documentado já era esse. ainda tem mais duas correções pra entrar essa semana, então não quero soltar versão agora. prepara o PR pra mim: grava o corpo em pr-body.md e o comando gh completo em pr-command.txt (não executa, eu rodo depois).
