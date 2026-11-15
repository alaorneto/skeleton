"""DNS label rules of the organization slugs (#478)."""

import pytest

from skeleton.common.hostnames import RESERVED_LABELS, slug_problem


@pytest.mark.parametrize("value", ["acme", "a", "a-1", "0x", "x" * 63, "acme-sa"])
def test_valid_labels(value):
    assert slug_problem(value) is None


@pytest.mark.parametrize(
    "value",
    ["Acme", "WWW", "a_b", "-a", "a-", "", "x" * 64, "xn--abc", "a.b", "acme\n", " acme", None, 1],
)
def test_labels_that_are_not_dns_labels(value):
    assert slug_problem(value) == "format"


@pytest.mark.parametrize("value", ["www", "api", "platform", "login", "admin", "o", "site"])
def test_reserved_labels(value):
    assert value in RESERVED_LABELS
    assert slug_problem(value) == "reserved"


@pytest.mark.parametrize(("slug", "valid"), [("acme", True), ("Acme", False), ("www", False), ("x" * 64, False)])
def test_setup_admin_form_validates_the_slug(slug, valid):
    from skeleton.setup.forms import AdminForm

    form = AdminForm(
        {
            "email": "admin@example.test",
            "password1": "Mosaic-47!River-Pebble",
            "password2": "Mosaic-47!River-Pebble",
            "organization_name": "Acme",
            "organization_slug": slug,
        }
    )
    assert form.is_valid() is valid
    if not valid:
        assert "organization_slug" in form.errors
