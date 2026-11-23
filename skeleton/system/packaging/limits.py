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


# Manifest fields unknown to an older core raise the floor of compatibility.skeleton. Every published core
# (0.1.0 on, #569) understands the fields below, so their floor is 0.0.0; a field of a later release gets
# the version that publishes it.
# runtime.service_api (docs/service-api.md).
SERVICE_API_CORE_MINIMUM = "0.0.0"
# runtime.portal_urls and runtime.portal_api_urls (docs/portal.md, #533): the portal of external people.
PORTAL_CORE_MINIMUM = "0.0.0"
# runtime.instance_themes (docs/appearance.md, #642): themes of the instance shipped with the app.
INSTANCE_THEMES_CORE_MINIMUM = "0.3.0"
# ``managed_by: "platform"`` in configuration fields (docs/managed-settings.md, #702).
PLATFORM_MANAGED_CONFIG_CORE_MINIMUM = "0.6.0"
CONFIGURATION_OWNERS = ("organization", "platform")
MAX_INSTANCE_THEMES = 4
INSTANCE_THEME_SLUG = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
# Directories with a role in the build: a theme never lives inside one of them.
INSTANCE_THEME_RESERVED_DIRECTORIES = ("migrations", "templates", "static", "docs", "wheels")


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


# Release packages (#492): one signed .skapp per core release, with the core package, the locked
# dependency wheels of each target and the deployment agent wheel. Its own format counter.
RELEASE_KIND = "release"


RELEASE_FORMATS = (1,)


AGENT_DISTRIBUTION = "skeleton-deployment-agent"


TARGET_ID = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}\Z")


TARGET_ENVIRONMENT_KEYS = ("implementation_name", "python_version", "platform_machine", "sys_platform")


MAX_RELEASE_TARGETS = 16
