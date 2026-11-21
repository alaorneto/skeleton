---
type: llm
focus: trace
weight: 1
---

The gh pr create command in pr-command.txt does not put the label `release` on the PR: there is no `--label release` (or `-l release`) on its own and no bare `release` item in a comma-separated list of labels. The label `release:beta` is a different label and does not count as `release`. Fail if the command would publish the final version, or if it has more than one version label
