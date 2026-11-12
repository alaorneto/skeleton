"""Blocking rules of the erasure request (#191) that need no database."""

from types import SimpleNamespace
from uuid import uuid4

import pytest
from django.core.exceptions import PermissionDenied, ValidationError

from skeleton.accounts.erasure import anonymized_email, is_anonymized, reject_erasure, request_erasure


def test_a_service_account_cannot_ask_for_erasure():
    with pytest.raises(ValidationError):
        request_erasure(SimpleNamespace(pk=uuid4(), is_service_account=True))


def test_the_anonymized_email_is_derived_from_the_sysid_only():
    person = SimpleNamespace(pk=uuid4(), email="maria@example.test")
    assert anonymized_email(person) == f"anon-{person.pk}@anonymized.invalid"
    assert not is_anonymized(person)
    person.email = anonymized_email(person)
    assert is_anonymized(person)


def test_a_rejection_needs_a_short_note():
    for note in ("", "   ", "x" * 501):
        with pytest.raises(ValidationError):
            reject_erasure(actor=None, request_id=uuid4(), expected_revision=1, note=note)


def test_only_a_platform_administrator_decides():
    from skeleton.accounts.erasure import approve_erasure, requests_for_platform

    member = SimpleNamespace(pk=uuid4(), is_authenticated=True, is_active=True, is_staff=False, is_superuser=False)
    with pytest.raises(PermissionDenied):
        requests_for_platform(actor=member)
    with pytest.raises(PermissionDenied):
        approve_erasure(actor=member, request_id=uuid4(), expected_revision=1)
