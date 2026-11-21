---
type: llm
focus: trace
weight: 1
---

In github-commands.sh each child is created with `gh issue create` and exactly one type label: `feature` for the CSV export and for the e-mail delivery, `bug` for the monthly total; the epic gets the label `epic`. Every create passes the body with --body-file (no inline --body) and assigns with --assignee @me
