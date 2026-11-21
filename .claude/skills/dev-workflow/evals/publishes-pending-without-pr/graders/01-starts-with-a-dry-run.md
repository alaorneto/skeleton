---
type: tool_used
tool: Bash
input_match: 'release\.py["'']?\s+run\b[^"]*--dry-run'
min: 1
weight: 1
---

The agent runs `release.py run --force --dry-run` (the plan without writing anything), as the skill says every off-path version operation starts
