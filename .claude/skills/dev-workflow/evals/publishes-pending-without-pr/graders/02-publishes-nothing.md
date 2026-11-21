---
type: tool_used
tool: Bash
input_match: 'git tag\s+-a|git tag\s+v|git push|gh workflow run|gh release create|release\.py["'']?\s+(?:bump|baseline)'
min: 0
max: 0
weight: 1
---

The agent publishes nothing: no tag, push, workflow dispatch, GitHub Release, bump or baseline
