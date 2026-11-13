"""OrganizationService: the current organization, the actor's memberships and the member directory (#103)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from django.contrib.auth.base_user import AbstractBaseUser
from django.utils.translation import gettext

from .base import ServiceAPI, service_method
from .dto import DEFAULT_PAGE_SIZE, Page, as_uuid, paginate
from .errors import InvalidInput, NotFound
from .identity import PersonSummary, _display_name, _kind, _user


@dataclass(frozen=True, slots=True)
class OrganizationInfo:
    sysid: UUID
    name: str
    slug: str
    membership_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class OrganizationSettingsInfo:
    """Settings of the organization; blank values mean "use the instance default"."""

    display_name: str
    default_language: str
    default_timezone: str
    brand_color: str
    has_logo: bool
    revision: int


def _settings_info(record: Any) -> OrganizationSettingsInfo:
    from skeleton.tenancy.organization_settings import revision_of

    if record is None:
        return OrganizationSettingsInfo("", "", "", "", False, 0)
    return OrganizationSettingsInfo(
        display_name=record.display_name,
        default_language=record.default_language,
        default_timezone=record.default_timezone,
        brand_color=record.brand_color,
        has_logo=bool(record.logo_key),
        revision=revision_of(record),
    )


@dataclass(frozen=True, slots=True)
class GroupSummary:
    sysid: UUID
    code: str
    name: str


@dataclass(frozen=True, slots=True)
class MemberRef:
    """An active membership of the current organization, by identifiers only (#343)."""

    user_id: UUID
    membership_id: UUID


MAX_MEMBER_REFS = 500


def _active_members(*, include_service_accounts: bool = False) -> Any:
    from skeleton.tenancy.models import Membership

    # The directory lists people: service accounts (#44) are left out by default.
    members = Membership.objects.filter(is_active=True, user__is_active=True, user__deleted_at__isnull=True)
    return members if include_service_accounts else members.filter(user__account_type="human")


class OrganizationService(ServiceAPI):
    """Organization context and directory; managing memberships stays in the access services."""

    @service_method(read_only=True, since="0.5.0")
    def current(self, *, actor: AbstractBaseUser) -> OrganizationInfo:
        """Return the organization of the current context.

        Autorização: vínculo ativo do ator (validado pelo contexto).
        Erros: ContextRequired sem organização no contexto.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership
        from skeleton.tenancy.models import Organization

        organization = Organization.objects.get(pk=active_membership(_user(actor)).organization_id)
        return OrganizationInfo(organization.pk, organization.name, organization.slug)

    @service_method(read_only=True, since="0.5.0", tenant=False)
    def my_organizations(self, *, actor: AbstractBaseUser) -> tuple[OrganizationInfo, ...]:
        """Return the active organizations where the actor has an active membership.

        Autorização: o próprio ator, por autodescoberta dos vínculos; nunca os de outra pessoa.
        Erros: Forbidden se o ator divergir do contexto.
        Efeitos: nenhum.
        """
        from skeleton.tenancy.context import memberships_for_user

        rows = sorted(
            memberships_for_user(actor), key=lambda row: (row["organization_name"], str(row["organization_id"]))
        )
        return tuple(
            OrganizationInfo(row["organization_id"], row["organization_name"], row["organization_slug"], row["sysid"])
            for row in rows
        )

    @service_method(read_only=True, since="0.5.0")
    def members(
        self,
        *,
        actor: AbstractBaseUser,
        query: str | None = None,
        group_id: UUID | None = None,
        cursor: str | None = None,
        page_size: int = DEFAULT_PAGE_SIZE,
    ) -> Page[PersonSummary]:
        """Return a page of active members, optionally searched by name or limited to a group.

        Autorização: vínculo ativo do ator; o email só aparece com a capacidade ``members.view``.
        Erros: NotFound para grupo inexistente ou de outra organização; InvalidInput para busca,
        cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from django.db.models import Q

        from skeleton.access.capabilities import MEMBERS_VIEW, has_capability
        from skeleton.access.models import AccessGroup, GroupMember
        from skeleton.access.policies import active_membership

        active_membership(_user(actor))
        rows = _active_members().select_related("user", "user__profile")
        if query is not None:
            if not isinstance(query, str) or len(query.strip()) > 100:
                raise InvalidInput({"query": [gettext("Use até 100 caracteres.")]})
            term = query.strip()
            if term:
                rows = rows.filter(
                    Q(user__profile__display_name__icontains=term)
                    | Q(user__first_name__icontains=term)
                    | Q(user__last_name__icontains=term)
                )
        if group_id is not None:
            group = AccessGroup.objects.filter(pk=as_uuid(group_id, field="group_id")).first()
            if group is None:
                raise NotFound()
            rows = rows.filter(pk__in=GroupMember.objects.filter(group=group).values("membership_id"))
        show_email = has_capability(_user(actor), MEMBERS_VIEW)

        def convert(membership: Any) -> PersonSummary:
            person = membership.user
            return PersonSummary(
                sysid=person.pk,
                display_name=_display_name(person, getattr(person, "profile", None)),
                email=person.email if show_email else None,
                kind=_kind(person),
            )

        return paginate(rows, convert=convert, page_size=page_size, cursor=cursor, key=("user__sysid",))

    @service_method(read_only=True, since="0.5.0")
    def groups(
        self, *, actor: AbstractBaseUser, cursor: str | None = None, page_size: int = DEFAULT_PAGE_SIZE
    ) -> Page[GroupSummary]:
        """Return a page of the organization's live groups.

        Autorização: vínculo ativo do ator.
        Erros: InvalidInput para cursor ou tamanho de página inválidos.
        Efeitos: nenhum.
        """
        from skeleton.access.models import AccessGroup
        from skeleton.access.policies import active_membership

        active_membership(_user(actor))
        return paginate(
            AccessGroup.objects.all(),
            convert=lambda group: GroupSummary(group.pk, group.code, group.name),
            page_size=page_size,
            cursor=cursor,
            key=("code", "sysid"),
        )

    @service_method(read_only=True, since="0.5.0")
    def is_member(self, *, actor: AbstractBaseUser, user_id: UUID) -> bool:
        """Whether a person has an active membership in the current organization.

        Autorização: vínculo ativo do ator.
        Erros: InvalidInput para identificador inválido.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership

        active_membership(_user(actor))
        return _active_members().filter(user_id=as_uuid(user_id, field="user_id")).exists()

    @service_method(read_only=True, since="0.6.0", allow_system=True)
    def member_refs(
        self, *, actor: AbstractBaseUser | None, user_ids: tuple[UUID, ...], include_service_accounts: bool = False
    ) -> tuple[MemberRef, ...]:
        """Active memberships of the current organization among ``user_ids`` (people only by default).

        Autorização: vínculo ativo do ator; em ``system_context`` (jobs), ``actor=None``. Não
        devolve nome nem email; pessoa de outra organização ou com vínculo inativo não aparece (#343).
        Erros: InvalidInput para identificador inválido ou mais de 500 identificadores.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership

        if actor is not None:
            active_membership(_user(actor))
        if not isinstance(user_ids, tuple) or len(user_ids) > MAX_MEMBER_REFS:
            raise InvalidInput({"user_ids": [gettext("Informe até %(value1)s pessoas.") % {"value1": MAX_MEMBER_REFS}]})
        identifiers = [as_uuid(value, field="user_ids") for value in user_ids]
        if not identifiers:
            return ()
        rows = (
            _active_members(include_service_accounts=include_service_accounts)
            .filter(user_id__in=identifiers)
            .values_list("user_id", "sysid")
        )
        return tuple(
            MemberRef(user_id, membership_id) for user_id, membership_id in sorted(rows, key=lambda row: str(row[0]))
        )

    @service_method(read_only=True, since="0.5.0")
    def settings(self, *, actor: AbstractBaseUser) -> OrganizationSettingsInfo:
        """Return the settings of the current organization (revision 0 while never written).

        Autorização: vínculo ativo do ator.
        Erros: ContextRequired sem organização no contexto.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership
        from skeleton.tenancy.organization_settings import current_settings

        active_membership(_user(actor))
        return _settings_info(current_settings())

    @service_method(read_only=False, since="0.5.0")
    def update_settings(
        self,
        *,
        actor: AbstractBaseUser,
        display_name: str,
        default_language: str,
        default_timezone: str,
        brand_color: str,
        expected_revision: int,
    ) -> OrganizationSettingsInfo:
        """Replace the settings of the current organization with CAS (virtual revision 0).

        Autorização: administrador da organização.
        Erros: Forbidden para quem não é administrador; InvalidInput para idioma fora de
        ``LANGUAGES``, fuso inválido ou cor fora de ``#RRGGBB``; Conflict("stale_revision").
        Efeitos: grava ``OrganizationSettings`` (auditado) na transação do chamador.
        """
        from skeleton.tenancy.organization_settings import update_settings

        record = update_settings(
            actor=_user(actor),
            data={
                "display_name": display_name,
                "default_language": default_language,
                "default_timezone": default_timezone,
                "brand_color": brand_color,
            },
            expected_revision=expected_revision,
        )
        return _settings_info(record)
