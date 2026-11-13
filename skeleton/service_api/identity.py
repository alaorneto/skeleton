"""IdentityService: who is acting and the minimal identity of fellow members (#102)."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.translation import gettext

from .base import ServiceAPI, service_method
from .dto import as_uuid
from .errors import InvalidInput, NotFound


def _user(actor: AbstractBaseUser | None) -> Any:
    """The actor as the core policies type it (the concrete User model)."""
    return actor


def _kind(user: Any) -> IdentityKind:
    return IdentityKind.SERVICE if getattr(user, "is_service_account", False) else IdentityKind.PERSON


class IdentityKind(StrEnum):
    PERSON = "person"
    SERVICE = "service"  # Service accounts (#44).


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
    first_name: str = ""
    last_name: str = ""
    user_revision: int = 0
    profile_sysid: UUID | None = None
    profile_display_name: str = ""
    # What applies in the current context: profile, then organization, then instance (#170).
    effective_language: str = ""
    effective_timezone: str = ""


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
    from skeleton.accounts.models import User
    from skeleton.common.locale import preferences_for

    user = User.objects.get(pk=user.pk)
    profile = _profile(user)
    effective = preferences_for(user)
    return UserProfileInfo(
        sysid=user.pk,
        email=user.email,
        display_name=_display_name(user, profile),
        language=profile.language,
        timezone=profile.timezone,
        kind=_kind(user),
        revision=profile.revision,
        first_name=user.first_name,
        last_name=user.last_name,
        user_revision=user.revision,
        profile_sysid=profile.pk,
        profile_display_name=profile.display_name,
        effective_language=effective.language,
        effective_timezone=effective.timezone,
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

        active_membership(_user(actor))
        identifier = as_uuid(user_id, field="user_id")
        membership = (
            Membership.objects.filter(
                user_id=identifier, is_active=True, user__is_active=True, user__deleted_at__isnull=True
            )
            .select_related("user", "user__profile")
            .first()
        )
        if membership is None:
            raise NotFound()
        person = membership.user
        return PersonSummary(
            sysid=person.pk,
            display_name=_display_name(person, getattr(person, "profile", None)),
            email=person.email if has_capability(_user(actor), MEMBERS_VIEW) else None,
            kind=_kind(person),
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
            raise InvalidInput({"non_field_errors": [gettext("Informe language ou timezone.")]})
        if type(expected_revision) is not int:
            raise InvalidInput({"expected_revision": [gettext("Use a revisão inteira do perfil.")]})
        update_profile(actor, data=data, profile_revision=expected_revision)
        return _me(actor)

    @service_method(read_only=False, since="0.5.0", tenant=False)
    def update_my_profile(
        self,
        *,
        actor: AbstractBaseUser,
        expected_user_revision: int,
        expected_revision: int,
        changes: tuple[tuple[str, str], ...],
    ) -> UserProfileInfo:
        """Change the actor's own name and profile fields with CAS on both identity and profile.

        Autorização: só o próprio ator. Campos aceitos: ``first_name``, ``last_name``,
        ``display_name``, ``timezone`` e ``language``; nunca email, senha ou flags.
        Erros: InvalidInput para campo desconhecido ou valor inválido; Conflict("stale_revision")
        se qualquer uma das revisões não for a atual.
        Efeitos: atualiza identidade e perfil na transação do chamador (auditados).
        """
        from skeleton.accounts.services import update_profile

        data = dict(changes)
        if len(data) != len(changes):
            raise InvalidInput({"non_field_errors": [gettext("Campo repetido.")]})
        if type(expected_user_revision) is not int or type(expected_revision) is not int:
            raise InvalidInput({"expected_revision": [gettext("Use revisões inteiras.")]})
        update_profile(actor, data=data, user_revision=expected_user_revision, profile_revision=expected_revision)
        return _me(actor)
