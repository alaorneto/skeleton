---
type: regex
target:
  source: file
  path: close-commands.txt
pattern: 'gh issue close #?61\b[^\n]*--reason[ =]["'']?completed'
match: contains
weight: 1
---

close-commands.txt closes the issue with `gh issue close 61 --reason completed`
