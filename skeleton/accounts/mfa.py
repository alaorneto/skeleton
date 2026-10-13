from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured


def cipher():
    keys = getattr(settings, 'MFA_ENCRYPTION_KEYS', ())
    if not keys:
        raise ImproperlyConfigured('MFA_ENCRYPTION_KEYS must contain a Fernet key; plaintext MFA is forbidden.')
    try:
        return MultiFernet([Fernet(key.encode() if isinstance(key, str) else key) for key in keys])
    except (ValueError, TypeError) as exc:
        raise ImproperlyConfigured('Invalid MFA Fernet key.') from exc


def mfa_required(user):
    if not getattr(settings, 'SKELETON_REQUIRE_ADMIN_MFA', False):
        return False
    from allauth.mfa.models import Authenticator
    return not Authenticator.objects.filter(user=user, type=Authenticator.Type.TOTP).exists()
