---
type: llm
focus: trace
weight: 1
---

There is exactly one new commit, on the issue branch and not on main. Its subject is Conventional Commits starting with `fix` (for example `fix(reports): use a semicolon in the pt-BR CSV`), its body says why (spreadsheets in pt-BR read `;` as the separator and the comma as the decimal mark), and it ends with `Refs #34`. It commits reports/csv_export.py and not issue-34.json
