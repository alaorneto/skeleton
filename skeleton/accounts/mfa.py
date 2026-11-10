from django.conf import settings

from skeleton.common.crypto import multifernet


def cipher():
    """The MFA keyring; same MultiFernet tokens as before the common module (no data migration)."""
    return multifernet("MFA_ENCRYPTION_KEYS", purpose="MFA")


def mfa_required(user):
    if not getattr(settings, "SKELETON_REQUIRE_ADMIN_MFA", False):
        return False
    from allauth.mfa.models import Authenticator

    return not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
