---
type: llm
focus: trace
weight: 1
---

pr-command.txt contains a gh pr create command that puts a pre-release channel label on the PR: `--label release:beta` (or `-l release:beta`); `release:alpha` or `release:rc` do not pass, because the user asked for a version for the support team to try a finished feature
