---
tags: [dev-workflow]
max_turns: 25
timeout_seconds: 900
allowed_tools: [Read, Glob, Grep, Skill, Write, Edit, Bash]
---

A gente está com um ciclo de pré-releases aberto desde a v1.2.0: o suporte testou a v2.0.0-rc.1 e deu ok. Abri o PR #53, que é só documentação, e marquei ele com `release`. Antes de mesclar quero entender o que sai exatamente: qual número, o que entra nas notas e no changelog (só o que é novo desde a rc.1 ou tudo?), e o que acontece com as pré-releases que já saíram. O plano está em semver-plan.json (saída de `release.py plan --pr 53 --format json`) e o `release.py status` da main em release-status.txt, porque você não tem o repositório aqui. Explica pelo skill dev-workflow, em poucas linhas, e grava a resposta em release-explained.md.

Input files: semver-plan.json, release-status.txt
