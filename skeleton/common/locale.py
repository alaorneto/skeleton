"""Language and time zone of each response and message (#170).

Precedence: the person's profile, then the defaults of the current organization
(``OrganizationSettings``), then the instance (``SKELETON_DEFAULT_LANGUAGE`` and
``SKELETON_DEFAULT_TIMEZONE``). Before login only the browser's ``Accept-Language`` chooses,
among ``LANGUAGES``. A blank profile or organization value means "use the next level". The
organization always comes from the context the tenant middleware opened, never from a header
or parameter.

``LocaleMiddleware`` sits after ``TenantContextMiddleware`` so it reads the organization under
its RLS, and it renders lazy responses inside the overrides so templates see the same values.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.utils import timezone, translation
from django.utils.cache import patch_vary_headers


@dataclass(frozen=True, slots=True)
class Preferences:
    language: str
    timezone: str


def supported_language(code: Any) -> str | None:
    return code if isinstance(code, str) and code in dict(settings.LANGUAGES) else None


def valid_timezone(name: Any) -> str | None:
    if not isinstance(name, str) or not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def instance_preferences() -> Preferences:
    language = supported_language(getattr(settings, "SKELETON_DEFAULT_LANGUAGE", "")) or settings.LANGUAGE_CODE
    zone = valid_timezone(getattr(settings, "SKELETON_DEFAULT_TIMEZONE", "")) or "UTC"
    return Preferences(language, zone)


def organization_preferences() -> tuple[str | None, str | None]:
    """Defaults of the context organization (RLS applies); ``(None, None)`` outside one."""
    from skeleton.tenancy.context import current_context

    if current_context().organization_id is None:
        return None, None
    from skeleton.tenancy.organization_settings import current_settings

    record = current_settings()
    if record is None:
        return None, None
    return supported_language(record.default_language), valid_timezone(record.default_timezone)


def preferences_for(user: Any) -> Preferences:
    """Effective language and time zone of ``user`` in the current context."""
    profile = None if user is None or getattr(user, "is_anonymous", False) else getattr(user, "profile", None)
    language = supported_language(getattr(profile, "language", None))
    zone = valid_timezone(getattr(profile, "timezone", None))
    if language is None or zone is None:
        org_language, org_zone = organization_preferences()
        language = language or org_language
        zone = zone or org_zone
    fallback = instance_preferences()
    return Preferences(language or fallback.language, zone or fallback.timezone)


def browser_language(request: Any) -> str | None:
    """The first ``Accept-Language`` entry among ``LANGUAGES``, or None (no cookie, no default)."""
    from django.utils.translation.trans_real import parse_accept_lang_header

    for code, _quality in parse_accept_lang_header(request.META.get("HTTP_ACCEPT_LANGUAGE", "")):
        if code == "*":
            continue
        try:
            return supported_language(translation.get_supported_language_variant(code))
        except LookupError:
            continue
    return None


def preferences_for_request(request: Any) -> Preferences:
    user = getattr(request, "user", None)
    if getattr(user, "is_authenticated", False):
        return preferences_for(user)
    fallback = instance_preferences()
    return Preferences(browser_language(request) or fallback.language, fallback.timezone)


class LocaleMiddleware:
    """Activate the effective language and time zone for the view and its lazy render."""

    def __init__(self, get_response: Any) -> None:
        self.get_response = get_response

    def __call__(self, request: Any) -> Any:
        preferences = preferences_for_request(request)
        request.LANGUAGE_CODE = preferences.language
        with translation.override(preferences.language), timezone.override(preferences.timezone):
            response = self.get_response(request)
            if hasattr(response, "render") and not response.is_rendered:
                response.render()
        response.headers.setdefault("Content-Language", preferences.language)
        if not getattr(getattr(request, "user", None), "is_authenticated", False):
            patch_vary_headers(response, ("Accept-Language",))
        return response
