"""Single-use, email-bound invitations with transactional membership provisioning."""
from dataclasses import dataclass
from datetime import timedelta
import hashlib
import secrets
from uuid import UUID

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.mail import EmailMultiAlternatives
from django.db import transaction
from django.urls import reverse
from django.utils import timezone


class InvalidInvitation(ValidationError):
    def __init__(self):
        super().__init__('Invalid invitation.', code='invalid_invitation')


@dataclass(frozen=True)
class InvitationProof:
    sysid: UUID
    organization_id: UUID
    email: str
    role_id: UUID
    token_digest: str


def _digest(token):
    if not isinstance(token, str) or len(token) > 128 or len(token) < 32:
        raise InvalidInvitation()
    return hashlib.sha256(token.encode()).hexdigest()


def validate_invitation(organization_id, token):
    return _validate_reference(organization_id, _digest(token))


def _validate_reference(organization_id, digest, invitation_id=None):
    from skeleton.tenancy.models import Invitation, Organization
    from skeleton.tenancy.context import system_context
    # Invitation discovery is a narrow infrastructure lookup, never a tenant endpoint.
    with system_context(reason='invitation:validated', organization_id=organization_id):
        organization = Organization.objects.filter(pk=organization_id, is_active=True).first()
        candidates = Invitation.objects.filter(organization_id=organization_id, token_digest=digest, accepted_at__isnull=True, expires_at__gt=timezone.now())
        if invitation_id is not None:
            candidates = candidates.filter(pk=invitation_id)
        invitation = candidates.first()
        if organization is None or invitation is None or invitation.role.deleted_at:
            raise InvalidInvitation()
        return InvitationProof(invitation.pk, organization.pk, invitation.email, invitation.role_id, digest)


def pending_invitation(request):
    reference = request.session.get('skeleton_invitation')
    if not isinstance(reference, dict) or set(reference) != {'organization', 'invitation', 'digest'}:
        return None
    try:
        return _validate_reference(UUID(reference['organization']), reference['digest'], UUID(reference['invitation']))
    except (InvalidInvitation, ValueError, TypeError):
        request.session.pop('skeleton_invitation', None)
        return None


def issue_invitation(*, user, email, role, request=None):
    from skeleton.access.services import require_organization_admin
    from skeleton.accounts.services import normalized_email
    from skeleton.tenancy.models import Invitation, Organization
    from skeleton.tenancy.context import current_context
    require_organization_admin(user)
    organization_id = current_context().organization_id
    if role.organization_id != organization_id or role.deleted_at:
        raise ValidationError({'role': 'Role must be active in this organization.'})
    organization = Organization.objects.get(pk=organization_id, is_active=True)
    token = secrets.token_urlsafe(32)
    with transaction.atomic():
        invitation = Invitation(organization=organization, email=normalized_email(email), role=role, token_digest=_digest(token), expires_at=timezone.now() + timedelta(days=7))
        invitation.save()
        path = reverse('invitation', kwargs={'organization_id': organization_id, 'token': token})
        if request is not None:
            url = request.build_absolute_uri(path)
        else:
            origin = getattr(settings, 'SKELETON_PUBLIC_ORIGIN', '')
            if not origin:
                raise ValidationError('Invitation email requires a request or SKELETON_PUBLIC_ORIGIN.')
            url = origin.rstrip('/') + path
        subject_name = " ".join(organization.name.splitlines())
        message = EmailMultiAlternatives(f'Convite: {subject_name}', f'Você recebeu um convite para {organization.name}.\n\n{url}\n\nO convite expira em sete dias.', settings.DEFAULT_FROM_EMAIL, [invitation.email])
        from skeleton.common.mail import schedule_mail
        schedule_mail(message, request=request, target_sysid=invitation.pk)
        return invitation


def accept_invitation(*, user, organization_id, token=None, proof=None, verified_signup=False):
    from allauth.account.models import EmailAddress
    from skeleton.access.services import provision_membership
    from skeleton.tenancy.context import system_context
    from skeleton.tenancy.models import Invitation, Organization
    from skeleton.access.models import Role
    proof = validate_invitation(organization_id, token) if proof is None else _validate_reference(organization_id, proof.token_digest, proof.sysid)
    if not user.is_active or user.deleted_at or user.email.strip().lower() != proof.email:
        raise InvalidInvitation()
    if not verified_signup and not EmailAddress.objects.filter(user=user, email__iexact=proof.email, verified=True).exists():
        raise InvalidInvitation()
    with system_context(reason='invitation:validated', organization_id=organization_id):
        with transaction.atomic():
            organization = Organization.objects.select_for_update().get(pk=organization_id, is_active=True)
            invitation = Invitation.objects.select_for_update().filter(pk=proof.sysid, token_digest=proof.token_digest, accepted_at__isnull=True, expires_at__gt=timezone.now()).first()
            if invitation is None:
                raise InvalidInvitation()
            role = Role.objects.get(pk=invitation.role_id)
            membership = provision_membership(organization=organization, user=user, role=role)
            updated = Invitation.objects.filter(pk=invitation.pk, revision=invitation.revision, accepted_at__isnull=True).update(accepted_at=timezone.now(), accepted_by=user.pk)
            if updated != 1:
                raise InvalidInvitation()
            return membership


@transaction.atomic
def resend_invitation(invitation, *, user, revision, request):
    from skeleton.access.services import require_organization_admin
    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy.context import current_context
    from skeleton.tenancy.models import Invitation
    require_organization_admin(user)
    if invitation.organization_id != current_context().organization_id:
        raise InvalidInvitation()
    locked = Invitation.objects.select_for_update().get(pk=invitation.pk)
    if locked.revision != revision:
        raise ConcurrentUpdate('Invitation revision is stale.')
    if locked.accepted_at is not None:
        raise InvalidInvitation()
    # The immutable old digest is revoked; only the new message contains the new token.
    locked.delete()
    return issue_invitation(user=user, email=locked.email, role=locked.role, request=request)
