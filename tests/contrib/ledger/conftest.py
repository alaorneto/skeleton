"""Ledger suite (#542): collected only with SKELETON_BUNDLED_APPS=calendar,ledger (CI job "calendar")."""

import pytest
from django.apps import apps

if not apps.is_installed("skeleton.contrib.ledger"):
    collect_ignore_glob = ["test_*.py"]


@pytest.fixture
def scenario(skeleton_db):
    """Organizations A/B with the ledger available and active, a member with every table action."""
    from skeleton.contrib.ledger.models import Book
    from skeleton.testing.service_api import build_service_scenario

    return build_service_scenario(Book, slug="ledger")


@pytest.fixture
def book(scenario):
    """The default book of A (cash basis, BRL), owned by the member."""
    from skeleton.contrib.ledger.service_api import LedgerService
    from skeleton.testing.service_api import acting_as

    with acting_as(scenario.member, scenario.a) as actor:
        return LedgerService().create_default_book(actor=actor, basis="cash")
