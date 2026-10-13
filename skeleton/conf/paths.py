"""Resolve the derived project's root independently of the installed core wheel."""
import importlib.util
import os
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured


def project_root():
    explicit = os.environ.get("SKELETON_PROJECT_ROOT")
    if explicit:
        return Path(explicit).expanduser().resolve()
    settings_module = os.environ.get("DJANGO_SETTINGS_MODULE", "config.settings.development")
    package = settings_module.split(".")[0]
    if package == "skeleton":
        raise ImproperlyConfigured("Reusable settings require SKELETON_PROJECT_ROOT or a project settings entrypoint.")
    spec = importlib.util.find_spec(package)
    if spec and spec.origin:
        return Path(spec.origin).resolve().parent.parent
    raise ImproperlyConfigured("Cannot locate the project root; set SKELETON_PROJECT_ROOT.")
