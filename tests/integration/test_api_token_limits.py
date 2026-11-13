"""Token rate limits and last use (#48): 429 without writes, brute force, operational activity."""

import json
from datetime import timedelta
from uuid import uuid4

import pytest
from django.core.cache import cache
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.integrations.tokens.models import ApiTokenActivity
from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context
from tools.security_fixtures import actor_tenant, build_security_fixture

pytestmark = pytest.mark.needs_example_app

NOTE = "example_notes.note"


@pytest.fixture
def access(skeleton_db):
    cache.clear()
    yield build_security_fixture()
    cache.clear()


def token_for(user, scopes):
    with request_context(actor=user, request_id=uuid4(), source="test:tokens"):
        return issue_personal_token(
            actor=user, name="CLI", scopes=list(scopes), expires_at=timezone.now() + timedelta(days=7)
        )


def bearer(text):
    return Client(HTTP_AUTHORIZATION=f"Bearer {text}")


def records(organization):
    return reverse("api:records", kwargs={"organization_id": organization.pk, "table_key": NOTE})


def test_token_above_the_limit_gets_429_and_writes_nothing(access, settings):
    from examples.notes.models import Note

    settings.SKELETON_API_TOKEN_RATE_PER_MINUTE = 2
    client = bearer(token_for(access.alice, [f"{NOTE}:read", f"{NOTE}:create"]).text)
    assert client.get(records(access.a)).status_code == 200
    assert client.get(records(access.a)).status_code == 200
    blocked = client.post(
        records(access.a), data=json.dumps({"code": "over", "title": "x", "body": "y"}), content_type="application/json"
    )
    assert blocked.status_code == 429 and blocked.json()["code"] == "rate_limited"
    assert 1 <= int(blocked["Retry-After"]) <= 60
    with actor_tenant(access.admin, access.a):
        assert not Note.objects.filter(code="over").exists()
    other = bearer(token_for(access.bob, [f"{NOTE}:read"]).text)
    assert other.get(records(access.a)).status_code == 200  # The limit is per token.


def test_failed_authentications_from_one_ip_are_limited(access, settings):
    settings.SKELETON_API_TOKEN_FAILURES_PER_MINUTE = 3
    valid = token_for(access.alice, [f"{NOTE}:read"]).text
    for index in range(3):
        assert bearer(f"skt_{index:08x}_" + "A" * 43).get(records(access.a)).status_code == 401
    blocked = bearer(valid).get(records(access.a))
    assert blocked.status_code == 429 and "Retry-After" in blocked
    elsewhere = Client(HTTP_AUTHORIZATION=f"Bearer {valid}", REMOTE_ADDR="198.51.100.7")
    assert elsewhere.get(records(access.a)).status_code == 200


def test_last_use_is_written_at_most_once_per_window(access):
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.audit.models import AuditEvent

    issued = token_for(access.alice, [f"{NOTE}:read"])
    client = bearer(issued.text)
    for _ in range(3):
        assert client.get(records(access.a)).status_code == 200
    activity = ApiTokenActivity.all_objects.get(token_id=issued.token.pk)
    first = activity.last_used_at
    cache.clear()  # Even without the cache marker, the database window holds.
    assert client.get(records(access.a)).status_code == 200
    assert ApiTokenActivity.all_objects.get(pk=activity.pk).last_used_at == first
    ApiTokenActivity.all_objects.filter(pk=activity.pk).update(last_used_at=first - timedelta(minutes=6))
    cache.clear()
    assert client.get(records(access.a)).status_code == 200
    assert ApiTokenActivity.all_objects.get(pk=activity.pk).last_used_at > first
    platform, _ = bootstrap_identity(
        email="limits-audit@example.test", organization="limits-audit", name="Audit", password="Limits-audit-Pass-48!"
    )
    with request_context(actor=platform, request_id=None, source="test:activity-audit"):
        assert not AuditEvent.objects.filter(model_label="sk_integrations.apitokenactivity").exists()


class _DownCache:
    """A cache whose server is gone: every call raises."""

    def _fail(self, *args, **kwargs):
        raise ConnectionError("cache down")

    add = incr = get = set = delete = _fail


def test_rate_limits_let_requests_through_when_the_cache_is_down(access, monkeypatch):
    """The limiter is a brake, never the authorization: no 429 nor 500 without the cache (#328)."""
    from skeleton.common import ratelimit

    monkeypatch.setattr(ratelimit, "cache", _DownCache())
    client = bearer(token_for(access.alice, [f"{NOTE}:read"]).text)
    assert client.get(records(access.a)).status_code == 200
    invalid = bearer("skt_00000000_" + "A" * 43).get(records(access.a))
    assert invalid.status_code == 401


def test_last_use_is_still_bounded_by_the_database_without_the_cache(access, monkeypatch):
    """Without the cache marker, the database window still writes at most once (#328)."""
    from skeleton.integrations.tokens import activity
    from skeleton.tenancy.context import system_context

    monkeypatch.setattr(activity, "cache", _DownCache())
    issued = token_for(access.alice, [f"{NOTE}:read"])
    client = bearer(issued.text)

    def stored():
        with system_context(reason="test:token-activity"):
            return list(
                ApiTokenActivity.objects.filter(token_id=issued.token.pk).values_list("last_used_at", flat=True)
            )

    assert client.get(records(access.a)).status_code == 200
    first = stored()
    for _ in range(2):
        assert client.get(records(access.a)).status_code == 200
    assert len(first) == 1 and stored() == first
