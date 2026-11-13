from django.conf import settings
from django.core.checks import Error, Tags, Warning, register


def billing_problems():
    """Configuration that billing needs once it is turned on (#394)."""
    if not getattr(settings, "SKELETON_BILLING_ENABLED", False):
        return []
    problems = []
    if not getattr(settings, "BILLING_ENCRYPTION_KEYS", None):
        problems.append(
            "SKELETON_BILLING_ENABLED is on without BILLING_ENCRYPTION_KEYS: payment provider secrets "
            "would have no keyring. Generate a Fernet key and set BILLING_ENCRYPTION_KEYS."
        )
    return problems


@register()
def billing_checks(app_configs=None, **kwargs):
    return [Error(problem, id="skeleton.E037") for problem in billing_problems()]


@register(Tags.security, deploy=True)
def billing_public_url_check(app_configs=None, **kwargs):
    """Provider return pages and webhooks need absolute URLs of the site (#400)."""
    from urllib.parse import urlsplit

    if not getattr(settings, "SKELETON_BILLING_ENABLED", False):
        return []
    parts = urlsplit(str(getattr(settings, "SKELETON_PUBLIC_URL", "") or ""))
    if parts.scheme in ("http", "https") and parts.netloc and parts.path in ("", "/"):
        return []
    return [
        Warning(
            "SKELETON_BILLING_ENABLED is on without a valid SKELETON_PUBLIC_URL: payment providers "
            "cannot reach the webhooks nor send people back after a hosted checkout.",
            id="skeleton.W037",
        )
    ]
