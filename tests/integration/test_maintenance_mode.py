"""Administrative maintenance mode (#446, #447) with the full stack in PostgreSQL as the runtime role.

The unit tests prove the gate before the database; here the real identities, tokens and the
platform guard run against the mode stored in a per-test instance SQLite.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.instance import maintenance
from skeleton.instance.maintenance import enter, leave
from skeleton.instance.store import InstanceStore
from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context
from tools.security_fixtures import build_security_fixture

NOTE = "example_notes.note"


@pytest.fixture
def store(tmp_path, settings):
    path = tmp_path / "instance" / "instance.sqlite3"
    settings.SKELETON_MAINTENANCE_STORE = str(path)
    yield InstanceStore(path)
    maintenance._readers.pop(str(path), None)


def _refresh(store):
    """Forget the per-process cache: the tests do not wait the 5 s a running process may take."""
    maintenance.reader_for(store.path)._cached = None


@pytest.mark.needs_example_app
def test_api_token_gets_the_maintenance_answer(skeleton_db, store):
    access = build_security_fixture()
    with request_context(actor=access.alice, request_id=uuid4(), source="test:maintenance"):
        issued = issue_personal_token(
            actor=access.alice, name="CLI", scopes=[f"{NOTE}:read"], expires_at=timezone.now() + timedelta(days=7)
        )
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    url = reverse("api:records", kwargs={"organization_id": access.a.pk, "table_key": NOTE})
    assert client.get(url).status_code == 200

    enter(store, message="Migração", ends_at=timezone.now() + timedelta(hours=1), pause_jobs=True, source="test")
    _refresh(store)
    response = client.get(url)
    assert response.status_code == 503
    body = response.json()
    assert body["code"] == "maintenance" and body["detail"] == "Migração" and body["ends_at"]
    assert int(response["Retry-After"]) > 3000

    leave(store, source="test")
    _refresh(store)
    assert client.get(url).status_code == 200
