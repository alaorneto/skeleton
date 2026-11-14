"""Which settings module a process starts with: the setup or the normal one."""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

SETUP_SETTINGS = "config.settings.setup"
SETUP_TARGET_ENV = "SKELETON_SETUP_TARGET"
DEVELOPMENT_SETTINGS = "config.settings.development"
COMPLETED = "completed"


def project_root() -> Path:
    return Path(os.environ.get("SKELETON_PROJECT_ROOT") or Path.cwd())


def setup_required(base_dir: Path | None = None, environment: Mapping[str, str] | None = None) -> bool:
    """True while a key required by the setup is missing, or while a started setup is not completed.

    The first steps already store every required key, so a restart before the completion has to
    come back to the setup, not to settings that still lack cache, email and the rest (#453).
    ``SKELETON_SETUP=off`` disables the switch (CI, tools, instances configured elsewhere).
    """
    from skeleton.instance.resolve import missing_for_setup

    environment = os.environ if environment is None else environment
    if environment.get("SKELETON_SETUP", "").lower() in {"off", "0", "false"}:
        return False
    base_dir = base_dir or project_root()
    return bool(missing_for_setup(base_dir, environment)) or setup_in_progress(base_dir, environment)


def setup_in_progress(base_dir: Path, environment: Mapping[str, str]) -> bool:
    """A setup that recorded a step and has not completed; a token alone does not count.

    An instance that started empty (and so issued a token) and was then configured by variables
    or ``manage.py instance`` never recorded a step, and keeps leaving setup mode. Reading the
    state never creates the instance store.
    """
    from skeleton.instance.resolve import instance_paths
    from skeleton.instance.store import InstanceStore

    database, _ = instance_paths(base_dir, environment)
    state = InstanceStore(database).setup_state()
    return state.get("status") != COMPLETED and any(key.startswith("step:") for key in state)


def select_settings_module(default: str, *, base_dir: Path | None = None) -> str:
    """Setup settings while the instance is unconfigured; ``default`` afterwards.

    Another explicit ``DJANGO_SETTINGS_MODULE`` always wins, so tests and tools keep their
    profile; the entrypoint's own default (production for WSGI, development for runserver)
    still falls into the setup when the instance has no configuration yet.
    """
    explicit = os.environ.get("DJANGO_SETTINGS_MODULE")
    if explicit and explicit != default:
        return explicit
    if not setup_required(base_dir):
        return default
    # The profile the instance starts with once the setup completes: the setup applies its rules
    # (production refuses a local cache and non-SMTP email) instead of completing into a crash (#457).
    os.environ[SETUP_TARGET_ENV] = default
    return SETUP_SETTINGS


def setup_completed(store) -> bool:
    return store.setup_state().get("status") == COMPLETED
