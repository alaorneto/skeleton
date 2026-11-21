---
type: regex
target:
  source: file
  path: release-plan.md
pattern: '\b1\.5\.0\b'
match: contains
weight: 1
---

release-plan.md names 1.5.0, the version `run --force --dry-run` computes
