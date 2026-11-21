---
type: regex
target:
  source: file
  path: semver-analysis.json
pattern: '"schema"\s*:\s*1\b'
match: contains
weight: 1
---

semver-analysis.json declares `"schema": 1`, the first thing `validate_analysis` checks
