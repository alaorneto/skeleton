"""The deployment gate with the full stack in PostgreSQL as the runtime role (#446, #674).

The administrative maintenance mode left the application (#674): planned maintenance is done in
the infrastructure. What remains is the agent's marker, which closes every request during an
update; the unit tests prove it runs before the session and the database.
"""

from datetime import timedelta
from uuid import uuid4

import pytest
from django.test import Client
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

from skeleton.accounts.services import bootstrap_identity
from skeleton.integrations.tokens.services import issue_personal_token
from skeleton.tenancy.context import request_context
from tools.security_fixtures import build_security_fixture

NOTE = "example_notes.note"
PASSWORD = "Maintenance-platform-Password-2026!"


@pytest.mark.needs_example_app
def test_an_api_token_gets_the_deployment_answer_behind_the_marker(skeleton_db, tmp_path, settings):
    access = build_security_fixture()
    with request_context(actor=access.alice, request_id=uuid4(), source="test:maintenance"):
        issued = issue_personal_token(
            actor=access.alice, name="CLI", scopes=[f"{NOTE}:read"], expires_at=timezone.now() + timedelta(days=7)
        )
    url = reverse("api:records", kwargs={"organization_id": access.a.pk, "table_key": NOTE})
    marker = tmp_path / "maintenance"
    settings.SKELETON_MAINTENANCE_MARKER = str(marker)  # Read when the middleware is built.
    client = Client(HTTP_AUTHORIZATION=f"Bearer {issued.text}")
    assert client.get(url).status_code == 200
    marker.touch()
    response = client.get(url)
    assert response.status_code == 503 and response["Retry-After"] == "60"
    assert response.json() == {
        "code": "maintenance",
        "detail": "O sistema está sendo atualizado. Tente novamente em instantes.",
        "ends_at": None,
    }
    marker.unlink()
    assert client.get(url).status_code == 200


def test_the_platform_has_no_maintenance_page(skeleton_db):
    with pytest.raises(NoReverseMatch):
        reverse("platform:maintenance")
    user, _ = bootstrap_identity(
        email="no-maintenance@example.test", organization="no-maintenance", name="Platform", password=PASSWORD
    )
    client = Client()
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    assert client.get("/platform/maintenance/").status_code == 404
    html = client.get(reverse("platform:overview")).content.decode()
    assert "/platform/maintenance/" not in html
