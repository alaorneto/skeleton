"""Container healthcheck: ``python -m skeleton.api.healthcheck`` (#148).

Requests ``/health/live/`` on the local port with a Host the instance accepts, resolved
without starting Django (a setup-mode Django would issue tokens): ``DJANGO_HEALTH_HOST``,
the stored ``health_host`` or the first ``allowed_hosts`` entry, environment first; in setup
mode, ``localhost``.
"""

from __future__ import annotations

import os
import sys
import urllib.request
from pathlib import Path


def health_host(base_dir: Path) -> str:
    from skeleton.instance.resolve import LayeredEnviron, instance_paths
    from skeleton.instance.store import InstanceStore
    from skeleton.setup.mode import setup_required

    if setup_required(base_dir):
        return "localhost"
    database, key = instance_paths(base_dir)
    layered = LayeredEnviron(os.environ, InstanceStore(database).settings(), key)
    if layered.get("DJANGO_HEALTH_HOST"):
        return layered["DJANGO_HEALTH_HOST"]
    return layered.get("DJANGO_ALLOWED_HOSTS", "localhost").split(",")[0].strip().lstrip(".")


def main() -> int:
    base_dir = Path(os.environ.get("SKELETON_PROJECT_ROOT") or Path.cwd())
    port = os.environ.get("SKELETON_HEALTHCHECK_PORT", "8000")
    request = urllib.request.Request(f"http://127.0.0.1:{port}/health/live/", headers={"Host": health_host(base_dir)})
    with urllib.request.urlopen(request, timeout=3) as response:  # noqa: S310 - fixed local URL
        return 0 if response.status == 200 else 1


if __name__ == "__main__":
    sys.exit(main())
