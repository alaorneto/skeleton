---
type: regex
target: trace
pattern: 'Refs:? #34'
match: contains
weight: 1
---

The commit message carries the trailer `Refs #34` (the sandbox has no commit-msg hook to add it), whether passed with -m or written to a file for -F
