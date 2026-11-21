---
type: llm
focus: trace
weight: 1
---

release-plan.md says the version is 1.5.0 (minor) because of `feat(api): add Client.count`, lists both pending changes since v1.4.0 (that feat and `fix(api): skip deleted records in fetch`), and gives the command to publish later: `gh workflow run release.yml -f force=true` (preferred, it uses the CI identity and permissions) or `release.py run --force`. It does not tell the user to edit VERSION or the changelog
