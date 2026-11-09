"""IdentityService: who is acting and the minimal identity of fellow members (#102)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser

from .base import ServiceAPI, service_method
from .dto import as_uuid
from .errors import InvalidInput, NotFound


class IdentityKind(StrEnum):
    PERSON = "person"
    SERVICE = "service"  # Service accounts (#44) will report this kind.


@dataclass(frozen=True, slots=True)
class UserProfileInfo:
    """The actor's own identity and preferences; ``revision`` is the profile revision for CAS."""

    sysid: UUID
    email: str
    display_name: str
    language: str
    timezone: str
    kind: IdentityKind
    revision: int


@dataclass(frozen=True, slots=True)
class PersonSummary:
    """Another member as an app may show them; ``email`` only for holders of ``members.view``."""

    sysid: UUID
    display_name: str
    email: str | None
    kind: IdentityKind


def _display_name(user: Any, profile: Any) -> str:
    name = (getattr(profile, "display_name", "") or "").strip()
    if name:
        return name
    full = f"{user.first_name} {user.last_name}".strip()
    return full or user.email.split("@", 1)[0]


def _profile(user: Any) -> Any:
    from skeleton.accounts.models import UserProfile

    return UserProfile.objects.get(user_id=user.pk)


def _me(user: Any) -> UserProfileInfo:
    profile = _profile(user)
    return UserProfileInfo(
        sysid=user.pk,
        email=user.email,
        display_name=_display_name(user, profile),
        language=profile.language,
        timezone=profile.timezone,
        kind=IdentityKind.PERSON,
        revision=profile.revision,
    )


class IdentityService(ServiceAPI):
    """Identity and profile of people; never passwords, MFA state or ``auth_version``."""

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def me(self, *, actor: AbstractBaseUser) -> UserProfileInfo:
        """Return the actor's own identity and preferences.

        Autorização: o próprio ator autenticado; funciona com ou sem organização no contexto.
        Erros: Forbidden se o ator divergir do contexto.
        Efeitos: nenhum.
        """
        return _me(actor)

    @service_method(read_only=True, since="0.5.0")
    def get_person(self, *, actor: AbstractBaseUser, user_id: UUID) -> PersonSummary:
        """Return a member of the current organization.

        Autorização: o ator precisa de vínculo ativo; o email só aparece com a capacidade
        ``members.view``.
        Erros: NotFound se a pessoa não tiver vínculo ativo na organização atual, mesmo que exista.
        Efeitos: nenhum.
        """
        from skeleton.access.capabilities import MEMBERS_VIEW, has_capability
        from skeleton.access.policies import active_membership
        from skeleton.tenancy.models import Membership

        active_membership(actor)
        identifier = as_uuid(user_id, field="user_id")
        membership = (
            Membership.objects.filter(
                user_id=identifier, is_active=True, user__is_active=True, user__deleted_at__isnull=True
            )
            .select_related("user")
            .first()
        )
        if membership is None:
            raise NotFound()
        person = membership.user
        return PersonSummary(
            sysid=person.pk,
            display_name=_display_name(person, _profile(person)),
            email=person.email if has_capability(actor, MEMBERS_VIEW) else None,
            kind=IdentityKind.PERSON,
        )

    @service_method(read_only=False, since="0.5.0", tenant=False)
    def update_my_preferences(
        self,
        *,
        actor: AbstractBaseUser,
        expected_revision: int,
        language: str | None = None,
        timezone: str | None = None,
    ) -> UserProfileInfo:
        """Change the actor's own language and/or time zone.

        Autorização: só o próprio ator; ninguém altera preferências de outra pessoa.
        Erros: InvalidInput para idioma ou fuso inválido ou nada a alterar; Conflict("stale_revision")
        se ``expected_revision`` não for a revisão atual do perfil.
        Efeitos: atualiza o perfil na transação do chamador (auditado).
        """
        from skeleton.accounts.services import update_profile

        data = {name: value for name, value in (("language", language), ("timezone", timezone)) if value is not None}
        if not data:
            raise InvalidInput({"non_field_errors": ["Informe language ou timezone."]})
        if type(expected_revision) is not int:
            raise InvalidInput({"expected_revision": ["Use a revisão inteira do perfil."]})
        update_profile(actor, data=data, profile_revision=expected_revision)
        return _me(actor)
