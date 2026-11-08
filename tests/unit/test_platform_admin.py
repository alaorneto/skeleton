"""Truth table of the single platform administrator predicate (#16)."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.contrib.auth.models import AnonymousUser

from skeleton.accounts.platform import (
    LastPlatformAdministrator,
    assert_platform_admin_remains,
    has_partial_platform_flags,
    is_platform_admin,
)


def identity(**overrides):
    values = {
        "pk": uuid4(),
        "is_authenticated": True,
        "is_active": True,
        "deleted_at": None,
        "is_staff": True,
        "is_superuser": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, True),
        ({"is_staff": False}, False),
        ({"is_superuser": False}, False),
        ({"is_staff": False, "is_superuser": False}, False),
        ({"is_active": False}, False),
        ({"deleted_at": datetime(2026, 1, 1, tzinfo=UTC)}, False),
        ({"is_authenticated": False}, False),
        ({"is_staff": 1, "is_superuser": 1}, False),
    ],
)
def test_predicate_truth_table(overrides, expected):
    assert is_platform_admin(identity(**overrides)) is expected


@pytest.mark.parametrize("value", [None, AnonymousUser(), uuid4(), "user"])
def test_non_identities_are_never_platform_admins(value):
    assert is_platform_admin(value) is False


def test_partial_flags_are_detected_but_grant_nothing():
    assert has_partial_platform_flags(identity(is_staff=False))
    assert has_partial_platform_flags(identity(is_superuser=False))
    assert not has_partial_platform_flags(identity())
    assert not has_partial_platform_flags(identity(is_staff=False, is_superuser=False))


def test_last_admin_rule_over_the_locked_set():
    first, second, outsider = identity(), identity(), identity(is_staff=False)
    assert_platform_admin_remains([first, second], first, keeps_role=False)
    assert_platform_admin_remains([first], outsider, keeps_role=False)
    assert_platform_admin_remains([first], first, keeps_role=True)
    with pytest.raises(LastPlatformAdministrator) as raised:
        assert_platform_admin_remains([first], first, keeps_role=False)
    assert raised.value.code == "last_platform_administrator"
