---
type: regex
target:
  source: file
  path: github-commands.sh
pattern: 'sync-epic\s+#?\d+\s+--dry-run'
match: contains
weight: 1
---

github-commands.sh checks the native links with `sync-epic <E> --dry-run`
