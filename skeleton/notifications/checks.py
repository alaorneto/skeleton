from urllib.parse import urlsplit

from django.conf import settings
from django.core.checks import Error, Tags, register
from django.core.checks import Warning as CheckWarning


@register()
def notification_type_checks(app_configs=None, **kwargs):
    from .registry import notification_problems

    return [Error(problem, id="skeleton.E023") for problem in notification_problems()]


@register(Tags.security, deploy=True)
def public_url_checks(app_configs=None, **kwargs):
    """skeleton.W035: notification emails need an absolute origin for their links (#279)."""
    value = (getattr(settings, "SKELETON_PUBLIC_URL", "") or "").strip()
    parts = urlsplit(value)
    if value and parts.scheme in {"http", "https"} and parts.netloc and parts.path in {"", "/"}:
        return []
    return [
        CheckWarning(
            "SKELETON_PUBLIC_URL is empty or is not an origin such as https://app.example.com: "
            "notification emails would carry relative links.",
            hint="Set SKELETON_PUBLIC_URL to the public origin of the site (scheme and host, no path).",
            id="skeleton.W035",
        )
    ]
