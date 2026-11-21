---
tags: [dev-workflow]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

terminei a issue #21 (feature: exportar o relatório mensal em CSV). foi um commit só, `feat(reports): export the monthly report as CSV`, na branch feat/21-exportar-csv. rodei `make test` e passaram 163 testes, com os testes novos em tests/test_export.py, e documentei o comando em docs/reports.md. o pessoal do suporte quer experimentar antes de a gente fechar a versão, então quero soltar uma versão de teste pra eles agora; a versão de verdade só depois que eles validarem. a última versão publicada é a v2.3.1, não tem mais nada acumulado nem pré-release em aberto, e o projeto usa os canais padrão do skill. prepara o PR pra mim: grava o corpo em pr-body.md e o comando gh completo em pr-command.txt (não executa, eu rodo depois). no fim me diz qual versão sai quando eu mesclar.
