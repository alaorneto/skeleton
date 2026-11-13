"""Limits, reserved names and fixed formats of package artifacts.

The MAX_* limits are read as attributes of this module at call time, so a test (or an operator
hook) that changes one changes every consumer."""

import re

MAX_ARCHIVE_BYTES = 512 * 1024 * 1024


MAX_FILE_BYTES = 256 * 1024 * 1024


MAX_TOTAL_BYTES = 1024 * 1024 * 1024


MAX_MEMBERS = 4096


MAX_WHEELHOUSE_MEMBERS = 65536


# Members of one wheel (#255): third-party wheels are larger than an app artifact.
MAX_WHEEL_MEMBERS = 32768


MAX_METADATA_BYTES = 2 * 1024 * 1024


MAX_EXPANSION_RATIO = 200


# First-party apps distributed inside the core wheel (#66): kind, package and label namespace.
BUNDLED_KIND = "bundled_app"


BUNDLED_PACKAGE = "skeleton.contrib"


BUNDLED_LABEL_PREFIX = "skeleton_"


RESERVED_NAMES = frozenset(
    {"skeleton", "config", "applications", "examples", "django", "deployment_agent", "site", "os", "sys"}
)


RESERVED_LAUNCHERS = {
    "gunicorn": "gunicorn",
    "uvicorn": "uvicorn",
    "django-admin": "django",
    "celery": "celery",
    "skeleton-core": "django-skeleton",
}


INTERPRETER_LAUNCHER = re.compile(r"(?:pythonw?|pypy|pip|easy_install)(?:\d+(?:\.\d+)*)?(?:[-_].*)?\Z")


IDENTIFIER = re.compile(r"[a-z][a-z0-9_]*\Z")


STABLE_ID = re.compile(r"[a-z][a-z0-9._-]{1,127}\Z")


SHA256 = re.compile(r"[a-f0-9]{64}\Z")


SERVICE_CLASS = re.compile(r"[A-Z][A-Za-z0-9_]{0,99}\Z")


MAX_SERVICE_CLASSES = 32


# runtime.service_api (docs/service-api.md) is unknown to older cores, so declaring it raises the floor.
SERVICE_API_CORE_MINIMUM = "0.4.0"


SERVICE_API_DOCUMENT = "service-api.md"


SERVICE_API_HEADINGS = ("## Serviços", "## DTOs", "## Histórico de versões")


# v1 entries lack dependency ranges; the verified artifact manifest stays authoritative.
CATALOG_VERSIONS = (1, 2)


BUILD_EXCLUDED_DIRECTORIES = frozenset(
    {"__pycache__", ".git", ".pytest_cache", ".mypy_cache", ".ruff_cache", "build", "dist", ".venv"}
)


DOCUMENT_HEADINGS = {
    "spec.md": ("## Contract", "## Acceptance"),
    "adr.md": ("## Context", "## Decision", "## Consequences"),
}


SUGGESTIBLE_PROBLEM_CODES = ("missing_dependency", "incompatible_dependency_version")
