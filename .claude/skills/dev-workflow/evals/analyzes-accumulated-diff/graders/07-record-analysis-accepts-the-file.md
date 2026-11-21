---
type: regex
target: trace
pattern: 'proposto: \*\*major\*\*'
match: contains
weight: 1
---

`record-analysis --dry-run` accepted the file and computed level major: only a valid analysis prints the comment, whose header line ends with "nível proposto: **major**" (an invalid one prints "Análise inválida" and exits 1). The script's source has `**{data['level']}**` there, so reading it does not match
