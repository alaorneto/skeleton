"""The organization identifier (slug): availability while a person types and the change itself (#765).

The slug names the organization in its own address (``<slug>.<base domain>``) and must be
unique among every organization, deleted ones included. ``slug_availability`` is only
guidance for the pages; the barrier is ``prepare_slug`` plus ``save_with_slug``, which check
again under the organization's row lock and translate the database's ``unique`` violation
(two concurrent changes to the same slug) into the same refusal.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.utils.translation import gettext

AVAILABLE = "available"
CURRENT = "current"
TAKEN = "taken"
FORMAT = "format"
RESERVED = "reserved"


def availability_message(status: str) -> str:
    from .services import slug_messages

    messages = {
        AVAILABLE: gettext("Identificador disponível."),
        CURRENT: gettext("É o identificador atual desta organização."),
        TAKEN: taken_message(),
        **slug_messages(),
    }
    return messages[status]


def taken_message() -> str:
    return gettext("Este identificador já está em uso. Escolha outro.")


def slug_availability(slug: Any, *, organization_id: Any = None, using: str = "default") -> str:
    """``available``, ``current``, ``taken``, ``format`` or ``reserved`` for ``slug``.

    ``organization_id`` is the organization being edited: its own slug answers ``current``.
    Deleted organizations keep their slug (the constraint covers every row), so they count.
    """
    from skeleton.common.hostnames import slug_problem

    from .models import Organization

    problem = slug_problem(slug)
    if problem is not None:
        return problem
    owners = list(Organization.all_objects.using(using).filter(slug=slug).values_list("pk", flat=True)[:2])
    if not owners:
        return AVAILABLE
    if organization_id is not None and str(owners[0]) == str(organization_id) and len(owners) == 1:
        return CURRENT
    return TAKEN


def bound_identity_providers(organization: Any, *, using: str = "default") -> bool:
    """True when an identity provider (database or file) is bound to the organization.

    The binding fingerprint of a provider includes the organization's slug, and linked
    accounts keep that fingerprint: changing the slug would silently break their login.
    """
    from skeleton.accounts.models import IdentityProvider
    from skeleton.accounts.providers import configured_file_providers

    if IdentityProvider.objects.using(using).filter(bound_organization_id=organization.pk).exists():
        return True
    return any(binding.organization_slug == organization.slug for binding in configured_file_providers().bindings)


def prepare_slug(organization: Any, slug: Any, *, using: str = "default") -> bool:
    """Validate ``slug`` for the locked ``organization`` and set it in memory; ``False`` when unchanged.

    The caller holds the organization's row lock and saves; ``save_with_slug`` translates a
    concurrent winner of the same slug.
    """
    from .services import validate_new_slug

    if slug == organization.slug:
        return False
    validate_new_slug(slug)
    if bound_identity_providers(organization, using=using):
        raise ValidationError(
            {
                "slug": gettext(
                    "Esta organização tem provedor de identidade ligado a ela: o identificador não pode mudar "
                    "enquanto o provedor existir."
                )
            }
        )
    if slug_availability(slug, organization_id=organization.pk, using=using) == TAKEN:
        raise ValidationError({"slug": taken_message()})
    organization.slug = slug
    return True


def save_with_slug(record: Any, *, using: str = "default", **kwargs: Any) -> None:
    """Save ``record`` (an organization, new or changed) and refuse a slug another transaction just took."""
    try:
        with transaction.atomic(using=using):
            record.save(using=using, **kwargs)
    except ValidationError as exc:
        # The model's own unique check (full_clean) saw the committed winner first.
        problems = getattr(exc, "error_dict", {}).get("slug", ())
        if any(problem.code == "unique" for problem in problems):
            raise ValidationError({"slug": taken_message()}) from exc
        raise
    except IntegrityError as exc:
        from .models import Organization

        if Organization.all_objects.using(using).filter(slug=record.slug).exclude(pk=record.pk).exists():
            raise ValidationError({"slug": taken_message()}) from exc
        raise


def change_own_slug(*, actor: Any, slug: Any, expected_revision: Any) -> Any:
    """The organization administrator changes the slug of the organization in context, with CAS."""
    from skeleton.access.services import organization_lock
    from skeleton.common.exceptions import ConcurrentUpdate

    if type(expected_revision) is not int:
        raise ValidationError({"expected_revision": gettext("Informe a revisão.")})
    with organization_lock(actor) as organization:
        if organization.revision != expected_revision:
            raise ConcurrentUpdate("The organization changed.")
        if prepare_slug(organization, slug):
            save_with_slug(organization, update_fields={"slug"})
        return organization
