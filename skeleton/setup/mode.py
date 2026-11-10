"""Which settings module a process starts with: the setup or the normal one."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

SETUP_SETTINGS = "config.settings.setup"
COMPLETED = "completed"


def project_root() -> Path:
    return Path(os.environ.get("SKELETON_PROJECT_ROOT") or Path.cwd())


def setup_required(base_dir: Path | None = None, environment: Mapping[str, str] | None = None) -> bool:
    """True while a key required by the setup has no value in the environment or the store.

    ``SKELETON_SETUP=off`` disables the switch (CI, tools, instances configured elsewhere).
    """
    from skeleton.instance.resolve import missing_for_setup

    environment = os.environ if environment is None else environment
    if environment.get("SKELETON_SETUP", "").lower() in {"off", "0", "false"}:
        return False
    return bool(missing_for_setup(base_dir or project_root(), environment))


def select_settings_module(default: str, *, base_dir: Path | None = None) -> str:
    """Setup settings while the instance is unconfigured; ``default`` afterwards.

    Another explicit ``DJANGO_SETTINGS_MODULE`` always wins, so tests and tools keep their
    profile; the entrypoint's own default (production for WSGI, development for runserver)
    still falls into the setup when the instance has no configuration yet.
    """
    explicit = os.environ.get("DJANGO_SETTINGS_MODULE")
    if explicit and explicit != default:
        return explicit
    return SETUP_SETTINGS if setup_required(base_dir) else default


def setup_completed(store) -> bool:
    return store.setup_state().get("status") == COMPLETED
