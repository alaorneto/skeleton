"""First-party apps distributed inside the core wheel (#66).

Each subpackage has a ``kind: "bundled_app"`` or ``kind: "bundled_library"`` manifest, a
``skeleton_*`` label and the core's exact version. A library (#792: the calendar and the ledger)
has no availability nor activation of its own: it is effective in an organization while an
effective app depends on it. An app is made available by the platform and activated by each
organization. The core never imports this package.
"""
