"""Jobs of the system app: the informative update check of installations without agent (#502).

Opt-in (``SKELETON_UPDATE_REPOSITORY``): one GET to the fixed GitHub API host, through
``skeleton.common.outbound_http`` only, with a generic User-Agent and no installation identifier.
Only ``tag_name`` is read and validated; the release URL shown is built here from the validated
repository and tag. Nothing is verified by signature and nothing is installed: the result is an
informative notice kept in the cache, never application data.
"""

import logging
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

from django.conf import settings

UPDATE_CHECK_JOB = "system.update_check"
GITHUB_NOTICE_KEY = "skeleton:update-notice:github"
TAG = re.compile(r"v?(\d+(?:\.\d+){1,2}(?:[a-z0-9.+-]*)?)\Z")
logger = logging.getLogger("skeleton.system.updates")


def _interval_hours():
    return max(1, int(getattr(settings, "SKELETON_UPDATE_CHECK_HOURS", 24) or 1))


def _installed_version():
    from skeleton.versioning import core_version

    return core_version()["version"]


def _headers():
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "django-skeleton",
    }
    path = getattr(settings, "SKELETON_UPDATE_GITHUB_TOKEN_FILE", None)
    if path:
        try:
            token = Path(path).read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            token = ""
        if not token or any(character.isspace() for character in token):
            logger.warning("update_token_unreadable", extra={"event": "update_token_unreadable"})
            return None
        headers["Authorization"] = f"Bearer {token}"
    return headers


def update_check(**payload):
    """Look up the latest release of the configured repository; keep the previous result on failure."""
    import json

    from django.core.cache import cache
    from packaging.version import InvalidVersion, Version

    from skeleton.common import outbound_http
    from skeleton.system.releases import GITHUB_REPOSITORY

    repository = getattr(settings, "SKELETON_UPDATE_REPOSITORY", "") or ""
    if getattr(settings, "SKELETON_AGENT_SOCKET", None) or not repository:
        return
    if not GITHUB_REPOSITORY.fullmatch(repository):
        logger.warning("update_repository_invalid", extra={"event": "update_repository_invalid"})
        return
    headers = _headers()
    if headers is None:
        return
    url = f"https://api.github.com/repos/{repository}/releases/latest"  # The host is fixed here.
    try:
        response = outbound_http.request("GET", url, headers=headers, max_response_bytes=1024 * 1024)
    except Exception:  # noqa: BLE001 - network failure: the next window tries again
        logger.warning("update_check_failed", extra={"event": "update_check_failed"})
        return
    try:
        if response.status != 200 or response.truncated:
            raise ValueError(response.status)
        body = json.loads(response.body)
        tag = body.get("tag_name") if isinstance(body, dict) else None
        match = TAG.fullmatch(tag) if isinstance(tag, str) else None
        if match is None:
            raise ValueError("tag")
        latest = Version(match.group(1))
        installed = _installed_version()
        newer = installed is None or latest > Version(installed)
    except (ValueError, TypeError, InvalidVersion):
        logger.warning(
            "update_check_failed",
            extra={"event": "update_check_failed", "status": getattr(response, "status", None)},
        )
        return
    notice = {
        "latest": str(latest),
        "newer": newer,
        "url": f"https://github.com/{repository}/releases/tag/{tag}",  # Built here; html_url is ignored.
        "checked_at": datetime.now(UTC).isoformat(),
    }
    try:
        cache.set(GITHUB_NOTICE_KEY, notice, int(timedelta(hours=3 * _interval_hours()).total_seconds()))
    except Exception:  # noqa: BLE001 - a cache outage only delays the notice
        logger.warning("update_check_failed", extra={"event": "update_check_failed"})


def register_system_jobs():
    from skeleton.jobs.periodic import register_periodic
    from skeleton.jobs.registry import JobType, register_job_type

    register_job_type(
        JobType(
            UPDATE_CHECK_JOB, update_check, {}, run_as="system", max_attempts=1, scope="global", owner_app="sk_system"
        )
    )
    repository = getattr(settings, "SKELETON_UPDATE_REPOSITORY", "") or ""
    if repository and not getattr(settings, "SKELETON_AGENT_SOCKET", None):
        register_periodic(
            UPDATE_CHECK_JOB,
            UPDATE_CHECK_JOB,
            every=timedelta(hours=_interval_hours()),
            per_organization=False,
        )
