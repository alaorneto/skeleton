"""A person creates her own organization and picks a bundle (#715).

With ``SKELETON_ORGANIZATION_SIGNUP_ENABLED``, an authenticated person with a verified email
creates an organization at ``/organizations/new/`` and becomes its administrator. The platform's
policy of new organizations (#703) and the default storage location (#706) apply, and the chosen
bundle (#714) activates its apps. A paid bundle (a ``BundleOffer`` in the account's currency,
billing on) contracts its price instead: the apps come with the payment, and the caller opens
the checkout after the commit. Refused without creating anything: signup off, unverified email,
a service account, the per-person ceiling, the rate limit, a bundle not offered at the signup,
an invalid or taken identifier, or a ``required`` rule of the policy (only the platform creates).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from django.conf import settings
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.utils.translation import gettext

from skeleton.tenancy.context import system_context

SIGNUP_REASON = "organization:signup"
RATE_PER_HOUR = 5


@dataclass(frozen=True)
class SignupResult:
    organization: Any
    invoice_id: Any  # The invoice to pay for a paid bundle, else None.


def enabled() -> bool:
    return bool(getattr(settings, "SKELETON_ORGANIZATION_SIGNUP_ENABLED", False))


def unavailable_reason() -> str | None:
    """Why the signup cannot create organizations now (shown to the platform), or ``None``."""
    from .provisioning import required_rules

    if not enabled():
        return gettext("O cadastro de organizações está desligado.")
    if required_rules():
        return gettext("Uma regra da política das organizações novas exige um valor que só a plataforma informa.")
    return None


def signup_bundles() -> list[Any]:
    from skeleton.system.models import AppBundle

    return list(AppBundle.objects.filter(is_active=True, signup_visible=True).order_by("position", "label"))


def remaining(user: Any) -> int:
    """How many more organizations the person may create by the signup."""
    from .models import OrganizationSignup

    ceiling = int(getattr(settings, "SKELETON_ORGANIZATION_SIGNUP_MAX_PER_PERSON", 1) or 0)
    return max(ceiling - OrganizationSignup.objects.filter(signup_user=user).count(), 0)


def eligible(user: Any) -> bool:
    from .admission import has_verified_email

    return bool(
        enabled()
        and getattr(user, "is_authenticated", False)
        and getattr(user, "account_type", "human") == "human"
        and has_verified_email(user)
        and remaining(user) > 0
    )


def _offer(bundle: Any, currency: str) -> Any:
    if not getattr(settings, "SKELETON_BILLING_ENABLED", False):
        return None
    from skeleton.billing.models import BundleOffer

    return (
        BundleOffer.objects.select_related("price", "price__product")
        .filter(bundle=bundle, price__currency=currency, price__is_active=True)
        .first()
    )


def signup_organization(
    *, user: Any, name: str, slug: str, bundle_code: str, accept_terms: bool = False
) -> SignupResult:
    """Create the person's organization with the chosen bundle; see the module docstring."""
    from skeleton.common.ratelimit import hit

    from .admission import has_verified_email
    from .models import OrganizationSignup
    from .services import create_organization

    if not enabled():
        raise PermissionDenied(gettext("O cadastro de organizações está desligado."))
    if not getattr(user, "is_authenticated", False) or getattr(user, "account_type", "human") != "human":
        raise PermissionDenied(gettext("Só uma pessoa cria uma organização pelo cadastro."))
    if not has_verified_email(user):
        raise PermissionDenied(gettext("Confirme o seu email antes de criar uma organização."))
    if not hit(f"organization-signup:{user.pk}", limit=RATE_PER_HOUR, window_s=3600).allowed:
        raise ValidationError(gettext("Muitas tentativas. Tente de novo mais tarde."), code="rate_limited")
    if remaining(user) <= 0:
        raise ValidationError(gettext("Você já criou o número máximo de organizações."), code="signup_ceiling")
    if unavailable_reason() is not None:
        raise ValidationError(gettext("O cadastro de organizações está indisponível."), code="signup_unavailable")
    bundle = next((item for item in signup_bundles() if item.code == bundle_code), None)
    if bundle is None:
        raise ValidationError({"bundle": gettext("Escolha um dos pacotes oferecidos.")})
    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
        raise ValidationError({"name": gettext("Informe o nome (até 200 caracteres).")})
    with transaction.atomic(), system_context(reason=SIGNUP_REASON):
        organization = create_organization(owner=user, slug=slug, name=name.strip(), settings={})
        OrganizationSignup(signup_organization=organization, signup_user=user, bundle_code=bundle.code).save()
        invoice_id = _bundle(organization, bundle, accept_terms=accept_terms)
    return SignupResult(organization, invoice_id)


def _bundle(organization: Any, bundle: Any, *, accept_terms: bool) -> Any:
    """Free bundle: activate its apps. Paid bundle: contract its price and return the open invoice."""
    from skeleton.system.bundles import apply_bundle, record_bundle

    with system_context(reason=SIGNUP_REASON, organization_id=organization.pk):
        currency = _currency()
        offer = _offer(bundle, currency) if currency else None
        if offer is None:
            apply_bundle(organization, bundle)
            return None
        if not accept_terms:
            raise ValidationError({"accept_terms": gettext("Aceite os termos para contratar o pacote.")})
        from skeleton.billing import engine
        from skeleton.billing.models import Invoice

        if offer.price.product.kind == "base_plan":
            engine.change_plan(offer.price)
        else:
            engine.add_item(offer.price)
        record_bundle(organization, bundle)
        invoice = Invoice.objects.filter(status=Invoice.Status.OPEN).order_by("-issued_at").first()
        return invoice.pk if invoice is not None else None


def _currency() -> str:
    if not getattr(settings, "SKELETON_BILLING_ENABLED", False):
        return ""
    from skeleton.billing import engine

    account = engine.account()
    return account.currency if account is not None else ""
