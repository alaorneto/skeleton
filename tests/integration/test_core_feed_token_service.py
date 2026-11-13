"""FeedTokenService of the core service API (#76), exercised without the calendar installed (#319)."""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.service_api.errors import InvalidInput
from skeleton.service_api.tokens import FeedTokenService
from skeleton.tenancy.context import request_context
from skeleton.testing.registries import isolated_token_scopes
from skeleton.testing.service_api import assert_rejects_other_actor, assert_service_contract

pytestmark = pytest.mark.usefixtures("skeleton_db")
SCOPE = "coretest.feed"


def test_contract():
    assert_service_contract(FeedTokenService)


@pytest.fixture
def person():
    from skeleton.accounts.services import create_identity
    from skeleton.integrations.tokens.scopes import register_token_scope
    from skeleton.tenancy.context import system_context

    with isolated_token_scopes():
        register_token_scope(SCOPE, "Feed de teste", feed=True)
        with system_context(reason="test:feed-person"):
            user = create_identity(email="feed-person@example.test", password="Feed-person-2026!")
        yield user


def issue(user, *, actor=None, scope=SCOPE, expires=timedelta(days=30)):
    with request_context(actor=user, request_id=uuid4(), source="test:feed-service"):
        return FeedTokenService().issue(
            actor=actor or user, name="Feed", scope=scope, expires_at=timezone.now() + expires
        )


def test_issue_returns_the_text_once_for_a_feed_scope(person):
    issued = issue(person)
    assert issued.scope == SCOPE and issued.prefix in issued.text
    assert issued.expires_at > timezone.now()


def test_issue_rejects_scope_that_is_not_a_feed(person):
    with pytest.raises(InvalidInput):
        issue(person, scope="example_notes.note:read")


def test_issue_rejects_invalid_expiry(person):
    with pytest.raises(InvalidInput):
        issue(person, expires=timedelta(days=-1))


def test_issue_rejects_other_actor(person):
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:feed-other"):
        other = create_identity(email="feed-other@example.test", password=None)
    assert_rejects_other_actor(lambda: issue(person, actor=other))


def test_feed_token_never_authenticates_the_rest_api(person):
    issued = issue(person)
    response = Client().get(reverse("api:me"), HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    assert response.status_code == 401
