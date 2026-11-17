"""System checks of the ledger (#545)."""

from typing import Any

from django.core.checks import Error, register


@register()
def posting_rule_checks(app_configs: Any = None, **kwargs: Any) -> list[Error]:
    """E041: every posting rule names an installed SecuredRecord model or derived table as its source (#565)."""
    from django.apps import apps

    from skeleton.access.derivation import is_derived
    from skeleton.common.models import SecuredRecord

    from .registry import all_rules

    errors = []
    for rule in all_rules():
        try:
            model = apps.get_model(rule.source_table_key)
        except (LookupError, ValueError):
            model = None
        if model is None or not (issubclass(model, SecuredRecord) or is_derived(model)):
            errors.append(
                Error(
                    f"{rule.code}: source table {rule.source_table_key} is not an installed SecuredRecord model "
                    "or derived table.",
                    id="skeleton.E041",
                )
            )
    return errors
