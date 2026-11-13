from django.conf import settings
from django.core.checks import Error, register


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
