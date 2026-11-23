"""The price that sells a bundle of apps (#714): one offer per bundle and currency.

Only an active price of a ``base_plan`` whose ``included_apps`` cover the bundle's apps, or of an
``app`` product of an app of the bundle. Only the platform administrator writes offers.
"""

from __future__ import annotations

from typing import Any

from django.core.exceptions import ValidationError
from django.db import transaction
from django.utils.translation import gettext


@transaction.atomic
def set_bundle_offer(*, actor: Any, bundle: Any, price: Any) -> Any:
    """Create or replace the offer of ``bundle`` in the currency of ``price``."""
    from skeleton.accounts.platform import require_platform_context

    from .models import BundleOffer

    require_platform_context(actor)
    product = price.product
    if not price.is_active or price.deleted_at is not None:
        raise ValidationError({"price": gettext("Escolha um preço ativo.")})
    covers = (product.kind == "base_plan" and set(bundle.apps) <= set(price.included_apps or ())) or (
        product.kind == "app" and product.app_label in bundle.apps
    )
    if not covers:
        raise ValidationError(
            {"price": gettext("O preço precisa ser de um plano que inclua as apps do pacote, ou de uma app dele.")}
        )
    current = BundleOffer.objects.select_for_update().filter(bundle=bundle, price__currency=price.currency).first()
    if current is not None and current.price_id == price.pk:
        return current
    if current is not None:
        current.delete()
    previous = BundleOffer.all_objects.filter(bundle=bundle, price=price).first()
    if previous is not None:
        previous.restore()
        return previous
    offer = BundleOffer(bundle=bundle, price=price)
    offer.save()
    return offer


@transaction.atomic
def remove_bundle_offer(*, actor: Any, bundle: Any, currency: str) -> None:
    from skeleton.accounts.platform import require_platform_context

    from .models import BundleOffer

    require_platform_context(actor)
    current = BundleOffer.objects.select_for_update().filter(bundle=bundle, price__currency=currency).first()
    if current is not None:
        current.delete()
