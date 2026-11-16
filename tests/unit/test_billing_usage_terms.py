"""Usage meters and the terms of a price (#522): registration, validation and the pure resolution."""

from types import SimpleNamespace

import pytest
from django.core.exceptions import ValidationError

from skeleton.billing import quota, usage


@pytest.fixture
def meters(monkeypatch):
    """An isolated meter and metric registry with one meter of acme_reminders."""
    monkeypatch.setattr(usage, "_meters", {})
    monkeypatch.setattr(quota, "_metrics", {"members": lambda organization_id: 0})
    usage.register_usage_meter("acme_reminders.messages", label="Mensagens", unit_label="mensagem")
    return usage


def test_meter_registration_refuses_bad_codes_duplicates_and_metrics(meters):
    for code in ("messages", "Acme.messages", "acme.", "acme.messages.extra", "", None):
        with pytest.raises(ValueError, match="Invalid usage meter"):
            usage.register_usage_meter(code, label="x", unit_label="x")
    usage.register_usage_meter("acme_reminders.messages", label="Mensagens", unit_label="mensagem")  # Same: no-op.
    with pytest.raises(ValueError, match="already registered"):
        usage.register_usage_meter("acme_reminders.messages", label="Outra", unit_label="mensagem")
    quota.register_quota_metric("acme_reminders.contacts", lambda organization_id: 0)
    with pytest.raises(ValueError, match="quota metric"):
        usage.register_usage_meter("acme_reminders.contacts", label="x", unit_label="x")
    with pytest.raises(ValueError, match="usage meter"):
        quota.register_quota_metric("acme_reminders.messages", lambda organization_id: 0)
    assert [meter.code for meter in usage.registered_usage_meters()] == ["acme_reminders.messages"]


def test_the_public_surface_re_exports_the_registry(meters):
    from skeleton.service_api import billing

    billing.register_usage_meter("acme_signatures.documents", label="Documentos", unit_label="documento")
    assert billing.registered_usage_meters() == (
        ("acme_reminders.messages", "Mensagens", "mensagem"),
        ("acme_signatures.documents", "Documentos", "documento"),
    )


@pytest.mark.parametrize(
    ("terms", "kind"),
    [
        ([], "base_plan"),
        ({"acme_reminders.unknown": {"included": 1}}, "base_plan"),
        ({"messages": {"included": 1}}, "base_plan"),
        ({"acme_reminders.messages": {"included": 1, "extra": 1}}, "base_plan"),
        ({"acme_reminders.messages": {"included": -1}}, "base_plan"),
        ({"acme_reminders.messages": {"included": 1.5}}, "base_plan"),
        ({"acme_reminders.messages": {"included": True}}, "base_plan"),
        ({"acme_reminders.messages": {"overage": "free"}}, "base_plan"),
        ({"acme_reminders.messages": {"overage": "bill"}}, "base_plan"),
        ({"acme_reminders.messages": {"overage": "bill", "unit": 100}}, "base_plan"),
        ({"acme_reminders.messages": {"overage": "bill", "unit": 0, "unit_amount": 9}}, "base_plan"),
        ({"acme_reminders.messages": {"unit": 100, "unit_amount": 9}}, "base_plan"),
        ({"acme_reminders.messages": {"hard_cap": 10}}, "base_plan"),
        (
            {
                "acme_reminders.messages": {
                    "included": 10,
                    "overage": "bill",
                    "unit": 1,
                    "unit_amount": 9,
                    "hard_cap": 5,
                }
            },
            "app",
        ),
        ({"acme_reminders.messages": {"overage": "bill", "unit": 1, "unit_amount": 9}}, "usage_pack"),
        ({"acme_reminders.messages": {"overage": "bill", "unit": 1, "unit_amount": 9}}, "storage_pack"),
        ({"acme_reminders.messages": {"included": 10, "overage": "block"}}, "usage_pack"),
        ({"acme_reminders.messages": "1000"}, "base_plan"),
    ],
)
def test_invalid_terms_are_refused_on_the_usage_field(meters, terms, kind):
    with pytest.raises(ValidationError) as error:
        usage.validate_usage(terms, product_kind=kind)
    assert list(error.value.message_dict) == ["usage"]


def test_valid_terms_and_the_registered_meters_in_the_error(meters):
    usage.validate_usage({}, product_kind="storage_pack")
    usage.validate_usage({"acme_reminders.messages": {"included": 1000}}, product_kind="usage_pack")
    usage.validate_usage({"acme_reminders.messages": {"included": 0, "overage": "block"}}, product_kind="app")
    usage.validate_usage(
        {
            "acme_reminders.messages": {
                "included": 1000,
                "overage": "bill",
                "unit": 100,
                "unit_amount": 900,
                "hard_cap": 1000,
            }
        },
        product_kind="base_plan",
    )
    with pytest.raises(ValidationError, match="acme_reminders.messages"):
        usage.validate_usage({"acme_other.items": {"included": 1}}, product_kind="app")


def item(kind, terms, *, quantity=1, app_label=""):
    product = SimpleNamespace(kind=kind, app_label=app_label)
    return SimpleNamespace(price=SimpleNamespace(product=product, usage=terms), quantity=quantity)


BILL = {"overage": "bill", "unit": 100, "unit_amount": 900}
M = "acme_reminders.messages"


def test_resolution_sums_allowances_and_prefers_the_app_over_the_base_plan():
    plan = item("base_plan", {M: {"included": 1000, **BILL, "unit_amount": 500, "hard_cap": 50000}})
    app = item("app", {M: {"included": 200, **BILL, "hard_cap": 9000}}, app_label="acme_reminders")
    pack = item("usage_pack", {M: {"included": 500}}, quantity=3)
    terms = usage.resolve_terms(M, [plan, app, pack])
    assert (terms.included, terms.overage, terms.unit_amount, terms.hard_cap, terms.limit) == (
        2700,
        "bill",
        900,
        9000,
        9000,
    )
    only_plan = usage.resolve_terms(M, [plan, pack])
    assert (only_plan.included, only_plan.unit_amount, only_plan.limit) == (2500, 500, 50000)


def test_resolution_of_block_packs_other_apps_trials_and_unlimited():
    pack = item("usage_pack", {M: {"included": 500}})
    assert (usage.resolve_terms(M, [pack]).overage, usage.resolve_terms(M, [pack]).limit) == ("block", 500)
    other_app = item("app", {M: {"included": 10, **BILL}}, app_label="acme_other")
    assert usage.resolve_terms(M, [other_app]).overage == "block"  # Only the meter's own app bills it.
    uncapped = item("base_plan", {M: {"included": 100, **BILL}})
    assert usage.resolve_terms(M, [uncapped]).limit is None  # Billed without a cap.
    for flags in ({"trialing": True}, {"cancel_scheduled": True}):
        trial = usage.resolve_terms(M, [uncapped], **flags)
        assert (trial.overage, trial.limit) == ("block", 100)
    unlimited = usage.resolve_terms(M, [item("base_plan", {"acme_other.items": {"included": 1}})])
    assert (unlimited.overage, unlimited.included, unlimited.limit) == ("unlimited", 0, None)
    assert usage.resolve_terms(M, []).overage == "unlimited"
