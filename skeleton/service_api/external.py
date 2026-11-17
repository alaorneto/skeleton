"""External people of the organization's portal (#532): invitations, accesses and the person's own access.

An app invites the person for one of its records (``subject_ref``) after authorizing its own act,
or a member with ``external.manage`` does; ``register_external_admission`` (re-exported here)
declares the handler that links the accepted access to the app's record. Inside the portal,
``me`` is the only way to know who the external person is: the app's services marked
``allow_external`` start from it.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_uuid, paginate
from .errors import Conflict, Forbidden, InvalidInput, NotFound

if TYPE_CHECKING:
    from django.contrib.auth.base_user import AbstractBaseUser

__all__ = [
    "ExternalAccessInfo",
    "ExternalAccessService",
    "ExternalInvitationInfo",
    "register_external_admission",
]


def register_external_admission(app_label: str, handler: Callable[[UUID, UUID, UUID], None]) -> None:
    """Declare, from the app's ``ready()``, ``handler(access_id, user_id, subject_ref)`` run on acceptance.

    It links the new access to the app's record in the same transaction; raising undoes the acceptance.
    """
    from skeleton.tenancy.external import register_external_admission as register

    register(app_label, handler)


@dataclass(frozen=True, slots=True)
class ExternalAccessInfo:
    sysid: UUID
    user_id: UUID
    display_name: str
    app_label: str
    is_active: bool
    revision: int


@dataclass(frozen=True, slots=True)
class ExternalInvitationInfo:
    sysid: UUID
    email: str
    app_label: str
    subject_ref: UUID
    expires_at: datetime
    accepted: bool
    revision: int


def _display_name(user: Any) -> str:
    profile = getattr(user, "profile", None)
    name = getattr(profile, "display_name", "") or ""
    return name or user.get_full_name() or ""


def access_info(access: Any) -> ExternalAccessInfo:
    return ExternalAccessInfo(
        access.pk, access.user_id, _display_name(access.user), access.app_label, access.is_active, access.revision
    )


def invitation_info(invitation: Any) -> ExternalInvitationInfo:
    return ExternalInvitationInfo(
        invitation.pk,
        invitation.email,
        invitation.app_label,
        invitation.subject_ref,
        invitation.expires_at,
        invitation.accepted_at is not None,
        invitation.revision,
    )


def _by_app(app_label: Any) -> bool:
    """Whether the calling app is ``app_label`` (it authorized its own act); else ``external.manage`` decides."""
    from .applications import calling_app_label

    return isinstance(app_label, str) and calling_app_label() == app_label


def _manager(actor: Any) -> None:
    from skeleton.access.capabilities import EXTERNAL_MANAGE, has_capability

    if not has_capability(actor, EXTERNAL_MANAGE):
        raise Forbidden()


def _translate(function: Callable[[], Any]) -> Any:
    from django.core.exceptions import PermissionDenied, ValidationError

    from skeleton.common.exceptions import ConcurrentUpdate
    from skeleton.tenancy import external

    try:
        return function()
    except external.ExternalDisabled:
        raise Conflict("external_disabled") from None
    except ConcurrentUpdate:
        raise Conflict("stale_revision") from None
    except PermissionDenied:
        raise NotFound() from None
    except ValidationError as error:
        raise InvalidInput(
            error.message_dict if hasattr(error, "error_dict") else {"__all__": error.messages}
        ) from None


class ExternalAccessService(ServiceAPI):
    """Invitations and accesses of the organization's external people (the portal)."""

    @service_method(read_only=False, since="0.1.0")
    def invite(
        self,
        *,
        actor: AbstractBaseUser,
        email: str,
        app_label: str,
        subject_ref: UUID,
        idempotency_key: str,
    ) -> ExternalInvitationInfo:
        """Invite an external person to the portal for one record of an app; a live invitation for it is revoked.

        Autorização: ``external.manage``, ou a própria app ``app_label`` depois de autorizar o ato dela;
        a app precisa estar ativa.
        Erros: Forbidden; Conflict("external_disabled") com o portal desligado; Unavailable com a app
        desativada; InvalidInput para email, referência ou chave inválidos.
        Efeitos: grava o convite e agenda o email com o link (o token só existe nele) na transação; a
        mesma ``idempotency_key`` devolve o convite já emitido, sem novo email.
        """
        from django.core.exceptions import PermissionDenied

        from skeleton.tenancy import external

        from .errors import Unavailable

        by_app = _by_app(app_label)
        if not by_app:
            _manager(actor)
        if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 200:
            raise InvalidInput({"idempotency_key": ("Chave de 1 a 200 caracteres.",)})
        subject = as_uuid(subject_ref, field="subject_ref")
        if not isinstance(email, str) or "@" not in email or len(email) > 254:
            raise InvalidInput({"email": ("Informe um email.",)})
        try:
            from skeleton.system.applications import require_application_enabled

            require_application_enabled(app_label)
        except PermissionDenied:
            raise Unavailable() from None
        return _translate(
            lambda: invitation_info(
                external.issue(
                    actor=actor,
                    email=email,
                    app_label=app_label,
                    subject_ref=subject,
                    idempotency_key=idempotency_key,
                    by_app=True,  # Authorized above.
                )
            )
        )

    @service_method(read_only=False, since="0.1.0")
    def revoke_invitation(self, *, actor: AbstractBaseUser, invitation_id: UUID, expected_revision: int) -> None:
        """Revoke a pending invitation, with CAS; its link stops working.

        Autorização: ``external.manage``, ou a app do convite.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); InvalidInput para convite já aceito.
        Efeitos: exclusão lógica do convite.
        """
        from skeleton.tenancy import external

        invitation = _invitation(invitation_id)
        by_app = _by_app(invitation.app_label)
        if not by_app:
            _manager(actor)
        _translate(
            lambda: external.revoke_invitation(
                user=actor, invitation_id=invitation.pk, expected_revision=expected_revision, by_app=True
            )
        )

    @service_method(read_only=False, since="0.1.0")
    def resend_invitation(
        self, *, actor: AbstractBaseUser, invitation_id: UUID, expected_revision: int
    ) -> ExternalInvitationInfo:
        """Replace a pending invitation by a new one (new token and email), with CAS.

        Autorização: ``external.manage``, ou a app do convite.
        Erros: NotFound; Forbidden; Conflict("stale_revision"); Conflict("external_disabled"); InvalidInput.
        Efeitos: revoga o convite e agenda o email do novo na transação.
        """
        from skeleton.tenancy import external

        invitation = _invitation(invitation_id)
        by_app = _by_app(invitation.app_label)
        if not by_app:
            _manager(actor)
        return _translate(
            lambda: invitation_info(
                external.resend_invitation(
                    user=actor, invitation_id=invitation.pk, expected_revision=expected_revision, by_app=True
                )
            )
        )

    @service_method(read_only=True, since="0.1.0")
    def accesses(
        self,
        *,
        actor: AbstractBaseUser,
        app_label: str | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[ExternalAccessInfo]:
        """The organization's external accesses, active first.

        Autorização: ``external.manage``.
        Erros: Forbidden; InvalidInput para cursor ou página.
        Efeitos: nenhum.
        """
        from skeleton.tenancy.models import ExternalAccess

        _manager(actor)
        rows = ExternalAccess.objects.select_related("user", "user__profile").order_by(
            "-is_active", "created_at", "sysid"
        )
        if app_label is not None:
            rows = rows.filter(app_label=app_label)
        return paginate(rows, convert=access_info, page_size=page_size, cursor=cursor)

    @service_method(read_only=True, since="0.1.0")
    def invitations(self, *, actor: AbstractBaseUser) -> tuple[ExternalInvitationInfo, ...]:
        """The pending invitations of the organization (at most 200), newest first.

        Autorização: ``external.manage``.
        Erros: Forbidden.
        Efeitos: nenhum.
        """
        from skeleton.tenancy.models import ExternalInvitation

        _manager(actor)
        rows = ExternalInvitation.objects.filter(accepted_at__isnull=True).order_by("-created_at")[:200]
        return tuple(invitation_info(row) for row in rows)

    @service_method(read_only=True, since="0.1.0")
    def access_for(self, *, actor: AbstractBaseUser, app_label: str, subject_ref: UUID) -> ExternalAccessInfo | None:
        """The access linked to an app's record by an accepted invitation, if any.

        Autorização: ``external.manage``, ou a própria app ``app_label``.
        Erros: Forbidden; InvalidInput.
        Efeitos: nenhum.
        """
        from skeleton.tenancy.models import ExternalAccess, ExternalInvitation

        if not _by_app(app_label):
            _manager(actor)
        subject = as_uuid(subject_ref, field="subject_ref")
        accepted = (
            ExternalInvitation.objects.filter(app_label=app_label, subject_ref=subject, accepted_at__isnull=False)
            .order_by("-accepted_at")
            .values_list("access_id", flat=True)
            .first()
        )
        if accepted is None:
            return None
        access = ExternalAccess.objects.select_related("user", "user__profile").filter(pk=accepted).first()
        return access_info(access) if access else None

    @service_method(read_only=False, since="0.1.0")
    def revoke(self, *, actor: AbstractBaseUser, access_id: UUID, expected_revision: int) -> ExternalAccessInfo:
        """Turn an external access off, with CAS; the portal closes on the next request and nothing is deleted.

        Autorização: ``external.manage``, ou a app que admitiu a pessoa.
        Erros: NotFound; Forbidden; Conflict("stale_revision").
        Efeitos: desativa o acesso (auditado).
        """
        from skeleton.tenancy import external
        from skeleton.tenancy.models import ExternalAccess

        access = ExternalAccess.objects.filter(pk=as_uuid(access_id, field="access_id")).first()
        if access is None:
            raise NotFound()
        if not _by_app(access.app_label):
            _manager(actor)
        return _translate(
            lambda: access_info(
                external.revoke_access(
                    user=actor, access_id=access.pk, expected_revision=expected_revision, by_app=True
                )
            )
        )

    @service_method(read_only=True, since="0.1.0", allow_external=True)
    def me(self, *, actor: AbstractBaseUser) -> ExternalAccessInfo:
        """The external person's own access, inside the portal.

        Autorização: só em contexto externo (o portal); fora dele, Forbidden.
        Erros: Forbidden fora do portal; NotFound sem acesso ativo.
        Efeitos: nenhum.
        """
        from skeleton.tenancy.context import current_context
        from skeleton.tenancy.models import ExternalAccess

        if not current_context().external:
            raise Forbidden(code="external_only")
        access = (
            ExternalAccess.objects.select_related("user", "user__profile")
            .filter(user_id=actor.pk, is_active=True)
            .first()
        )
        if access is None:
            raise NotFound()
        return access_info(access)


def _invitation(invitation_id: Any) -> Any:
    from skeleton.tenancy.models import ExternalInvitation

    invitation = ExternalInvitation.objects.filter(pk=as_uuid(invitation_id, field="invitation_id")).first()
    if invitation is None:
        raise NotFound()
    return invitation
