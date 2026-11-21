---
type: regex
target:
  source: file
  path: reports/monthly.py
pattern: 'cancelled'
match: contains
weight: 1
---

monthly_total now looks at the "cancelled" status
