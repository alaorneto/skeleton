"""One function normalizes email everywhere, with the same result as before (#269)."""

import pytest
from django.core.exceptions import ValidationError

SAMPLES = ["  Alice@Example.TEST  ", "bob@example.test", "\tCarol@EXAMPLE.test\n", "Dave@Ex.Ample"]


@pytest.mark.parametrize("value", SAMPLES)
def test_normalize_email_strips_and_lowercases(value):
    from skeleton.accounts.emails import normalize_email

    assert normalize_email(value) == value.strip().lower()


@pytest.mark.parametrize("value", SAMPLES)
def test_entry_points_agree(value):
    from skeleton.accounts.emails import normalize_email
    from skeleton.accounts.managers import UserManager
    from skeleton.accounts.services import normalized_email

    assert normalized_email(value) == UserManager.normalize_email(value) == normalize_email(value)


def test_validation_and_empty_values_keep_their_behavior():
    from skeleton.accounts.managers import UserManager
    from skeleton.accounts.services import normalized_email

    assert UserManager.normalize_email("") == "" and UserManager.normalize_email(None) == ""
    for bad in ("", "   ", "not-an-email"):
        with pytest.raises(ValidationError):
            normalized_email(bad)
