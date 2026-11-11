"""First-party apps distributed inside the core wheel (#66).

Each subpackage is an ordinary app with a ``kind: "bundled_app"`` manifest, a ``skeleton_*``
label and the core's exact version. Nothing here is installed by default: an operator opts in
with ``SKELETON_BUNDLED_APPS``, and the platform still makes the app available to organizations
and each organization still activates it. The core never imports this package.
"""
