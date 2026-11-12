"""Identity lifecycle operations; callers cannot mass-assign identity flags."""

from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.common.exceptions import ConcurrentUpdate


def normalized_email(email):
    from django.core.validators import validate_email

    from skeleton.accounts.emails import normalize_email

    email = normalize_email(email)
    validate_email(email)
    return email


def password_allowed(user):
    return bool(
        user.is_active
        and user.deleted_at is None
        and user.local_password_allowed
        and not getattr(user, "is_service_account", False)
    )


def create_identity(*, email, password=None, first_name="", last_name="", local_password_allowed=True, using="default"):
    """A new identity carries no platform flag nor membership, so creating one grants nothing."""
    from skeleton.accounts.models import User, UserProfile

    with transaction.atomic(using=using):
        user = User(
            email=normalized_email(email),
            first_name=first_name,
            last_name=last_name,
            local_password_allowed=local_password_allowed,
        )
        if password is None:
            user.set_unusable_password()
        else:
            from django.contrib.auth.password_validation import validate_password

            validate_password(password, user)
            user.set_password(password)
        user.save(using=using)
        UserProfile(user=user).save(using=using)
        return user


def ensure_profile(user, *, using="default"):
    from skeleton.accounts.models import UserProfile

    profile = UserProfile.all_objects.using(using).filter(user_id=user.pk).first()
    if profile is None:
        profile = UserProfile(user=user)
        profile.save(using=using)
    return profile


def update_profile(user, *, data, user_revision=None, profile_revision=None):
    from skeleton.accounts.models import User, UserProfile

    allowed = {"first_name", "last_name", "display_name", "timezone", "language"}
    if set(data) - allowed:
        raise ValidationError(gettext("Campos de perfil não permitidos."))
    if "timezone" in data and data["timezone"] != "":
        try:
            ZoneInfo(data["timezone"])
        except (ZoneInfoNotFoundError, TypeError, ValueError) as exc:
            raise ValidationError({"timezone": gettext("Fuso horário inválido.")}) from exc
    if "language" in data and (
        not isinstance(data["language"], str)
        or (data["language"] != "" and data["language"] not in dict(settings.LANGUAGES))
    ):
        raise ValidationError({"language": gettext("Idioma não suportado.")})
    with transaction.atomic():
        locked = User.objects.select_for_update().get(pk=user.pk)
        profile = UserProfile.objects.select_for_update().get(user=locked)
        if (user_revision is not None and locked.revision != user_revision) or (
            profile_revision is not None and profile.revision != profile_revision
        ):
            raise ConcurrentUpdate("Profile revision is stale.")
        changed_user, changed_profile = [], []
        for name, value in data.items():
            target, changed = (
                (locked, changed_user) if name in {"first_name", "last_name"} else (profile, changed_profile)
            )
            if getattr(target, name) != value:
                setattr(target, name, value)
                changed.append(name)
        if changed_user:
            locked.save(update_fields=changed_user)
        if changed_profile:
            profile.save(update_fields=changed_profile)
        return locked, profile


def revoke_sessions(user, *, using="default"):
    from skeleton.accounts.models import User

    with transaction.atomic(using=using):
        locked = User.all_objects.using(using).select_for_update().get(pk=user.pk)
        locked.auth_version += 1
        locked.save(using=using, update_fields=["auth_version"])
        user.auth_version = locked.auth_version
        return locked


def deactivate_user(user, *, actor, delete=False, expected_revision=None, using="default"):
    """Deactivate (or soft delete) an identity for a platform administrator (#226).

    ``expected_revision`` is the revision the confirmation page displayed; a different one
    raises ``ConcurrentUpdate``.
    """
    from skeleton.access.services import assert_user_deactivation
    from skeleton.accounts.models import User
    from skeleton.accounts.platform import assert_platform_admin_remains, lock_platform_admins, require_platform_actor

    require_platform_actor(actor)
    with transaction.atomic(using=using):
        # Platform administrators first, in sysid order, then the target: no lock inversion.
        admins = lock_platform_admins(using=using)
        locked = User.all_objects.using(using).select_for_update().get(pk=user.pk)
        if expected_revision is not None and locked.revision != expected_revision:
            raise ConcurrentUpdate("The identity changed since the form was displayed.")
        assert_platform_admin_remains(admins, locked, keeps_role=False)
        assert_user_deactivation(locked, using=using)
        if delete:
            locked.delete(using=using)
        else:
            locked.is_active = False
            locked.auth_version += 1
            locked.save(using=using, update_fields=["is_active", "auth_version"])
        return locked


PLATFORM_USER_FIELDS = ("first_name", "last_name", "is_active", "is_staff", "is_superuser")


def update_platform_user(user, *, actor, data, expected_revision, using="default"):
    """Platform-side identity edit with CAS; email changes stay in the verified allauth flow.

    Deactivating keeps the last-administrator guard, demoting or deactivating the last
    platform administrator raises ``LastPlatformAdministrator``, and any change to an access
    flag increments ``auth_version`` so existing sessions are revoked.
    """
    from skeleton.access.services import assert_user_deactivation
    from skeleton.accounts.models import User
    from skeleton.accounts.platform import (
        PLATFORM_FLAGS,
        assert_platform_admin_remains,
        is_platform_admin,
        lock_platform_admins,
        require_platform_actor,
    )

    require_platform_actor(actor)
    unknown = set(data) - set(PLATFORM_USER_FIELDS) - {"email"}
    if unknown:
        raise ValidationError(gettext("Campo não permitido."))
    with transaction.atomic(using=using):
        touches_role = bool(set(data) & {"is_active", *PLATFORM_FLAGS})
        admins = lock_platform_admins(using=using) if touches_role else []
        previous = User.all_objects.using(using).select_for_update().get(pk=user.pk)
        if expected_revision != previous.revision:
            raise ConcurrentUpdate("The platform form is stale.")
        if "email" in data and normalized_email(data["email"]) != previous.email:
            raise PermissionDenied(gettext("O email só muda pelo fluxo de verificação da conta."))
        if previous.is_active and data.get("is_active", True) is False:
            assert_user_deactivation(previous, using=using)
        changed = {key for key in PLATFORM_USER_FIELDS if key in data and getattr(previous, key) != data[key]}
        if not changed:
            return previous
        for key in changed:
            setattr(previous, key, data[key])
        if touches_role:
            assert_platform_admin_remains(admins, previous, keeps_role=is_platform_admin(previous))
        if changed & {"is_active", *PLATFORM_FLAGS}:
            previous.auth_version += 1
            changed.add("auth_version")
        previous.save(using=using, update_fields=changed)
        return previous


def restore_user(user, *, actor, using="default"):
    """Restore a deleted identity for a platform administrator; ``restore()`` applies the CAS."""
    from skeleton.accounts.platform import require_platform_actor

    require_platform_actor(actor)
    with transaction.atomic(using=using):
        user.restore(using=using)
        return user


def set_local_password(user, password, *, using="default"):
    from django.contrib.auth.password_validation import validate_password

    from skeleton.accounts.models import User

    with transaction.atomic(using=using):
        locked = User.all_objects.using(using).select_for_update().get(pk=user.pk)
        if not password_allowed(locked):
            raise PermissionDenied(gettext("O login com senha local não está disponível."))
        validate_password(password, locked)
        locked.set_password(password)
        locked.auth_version += 1
        locked.save(using=using, update_fields=["password", "auth_version"])
        user.password = locked.password
        user.auth_version = locked.auth_version
        user.revision = locked.revision
    return user


def bootstrap_identity(*, email, organization, name, password=None, using="default"):
    from allauth.account.models import EmailAddress

    from skeleton.accounts.models import User
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Organization

    email = normalized_email(email)
    with system_context(reason="bootstrap:identity", using=using):
        with transaction.atomic(using=using):
            user = User.all_objects.using(using).select_for_update().filter(email=email).first()
            if user is not None:
                from skeleton.accounts.platform import is_platform_admin

                if not is_platform_admin(user):
                    raise ValidationError("Existing bootstrap identity is inactive or not a platform administrator.")
            else:
                if not password:
                    raise ValidationError("A bootstrap password is required for a new identity.")
                user = create_identity(email=email, password=password, using=using)
                user.is_staff = user.is_superuser = True
                user.save(using=using, update_fields=["is_staff", "is_superuser"])
            EmailAddress.objects.using(using).get_or_create(
                user=user, email=email, defaults={"verified": True, "primary": True}
            )
            ensure_profile(user, using=using)
            org = Organization.all_objects.using(using).filter(slug=organization).first()
            if org and (org.deleted_at or not org.is_active):
                raise ValidationError("Existing bootstrap organization is inactive or deleted.")
    if org is None:
        org = create_organization(owner=user, slug=organization, name=name, using=using)
    else:
        from skeleton.access.models import Role
        from skeleton.access.services import provision_membership

        with system_context(reason="bootstrap:membership", organization_id=org.pk, using=using):
            role = Role.objects.using(using).get(code="organization_admin")
            provision_membership(organization=org, user=user, role=role)
    return user, org


def create_service_account(*, actor, name):
    """An organization creates a non-interactive member for its integrations (#44).

    Requires ``integrations.manage`` under ``organization_lock``. The identity is global (it is a
    ``User`` so owners, policies and audit keep working) but bound to this organization only: a
    synthetic ``@service.invalid`` email, an unusable password, ``local_password_allowed=False``
    and exactly one membership, with no role. Every login path refuses it.
    """
    from uuid import uuid4

    from skeleton.access.capabilities import INTEGRATIONS_MANAGE
    from skeleton.access.services import organization_lock
    from skeleton.accounts.models import SERVICE_EMAIL_DOMAIN, AccountType, User, UserProfile
    from skeleton.tenancy.models import Membership

    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 150:
        raise ValidationError({"name": gettext("Informe um nome de até 150 caracteres.")})
    with organization_lock(actor, capability=INTEGRATIONS_MANAGE) as organization:
        user = User(
            email=f"svc-{uuid4().hex}@{SERVICE_EMAIL_DOMAIN}",
            first_name=name.strip(),
            account_type=AccountType.SERVICE,
            service_organization=organization,
            local_password_allowed=False,
        )
        user.set_unusable_password()
        user.save()
        UserProfile(user=user).save()
        membership = Membership(organization=organization, user=user, is_active=True)
        membership.save()
        from skeleton.events.core_events import membership_changed

        membership_changed(membership, was_active=False)
        return user


def deactivate_service_account(*, actor, user):
    """Deactivate a service account of the current organization; sessions and tokens stop at once.

    Same capability and lock as creation. Idempotent and deactivation only, so it takes no
    revision. Revokes the membership's roles, groups and grants,
    deactivates the membership and the identity, and increments ``auth_version``. Another
    organization's account, or a human, is reported as unavailable.
    """
    from skeleton.access.capabilities import INTEGRATIONS_MANAGE
    from skeleton.access.services import organization_lock, revoke_membership_links
    from skeleton.accounts.models import AccountType, User
    from skeleton.tenancy.models import Membership

    with organization_lock(actor, capability=INTEGRATIONS_MANAGE) as organization:
        membership = Membership.objects.select_for_update().filter(user_id=user.pk).first()
        locked = (
            User.all_objects.select_for_update()
            .filter(pk=user.pk, account_type=AccountType.SERVICE, service_organization=organization)
            .first()
        )
        if locked is None or membership is None:
            raise ValidationError({"user": gettext("Conta de serviço indisponível.")})
        revoke_membership_links(membership)
        if membership.is_active:
            from skeleton.events.core_events import membership_changed

            membership.is_active = False
            membership.save(update_fields={"is_active"})
            membership_changed(membership, was_active=True)
        if locked.is_active:
            locked.is_active = False
            locked.auth_version += 1
            locked.save(update_fields=["is_active", "auth_version"])
        return locked


# Moved to their owning packages (#303); importable from here for one minor version.
from skeleton.access.administration import (  # noqa: E402, F401
    AdministratorSummary,
    OrganizationOverview,
    designate_organization_admin,
    organization_overview,
)
from skeleton.tenancy.services import create_organization, update_organization  # noqa: E402, F401
