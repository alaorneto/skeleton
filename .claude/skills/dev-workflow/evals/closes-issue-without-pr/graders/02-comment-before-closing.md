---
type: regex
target:
  source: file
  path: close-commands.txt
pattern: 'gh issue comment #?61\b[^\n]*--body-file[\s\S]*gh issue close #?61\b'
match: contains
weight: 1
---

The closing comment is posted with --body-file before the issue is closed
