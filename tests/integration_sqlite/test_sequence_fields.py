"""Sequence fields filled by the generic form and API (#156)."""

import json
from dataclasses import replace

import pytest
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.sequences import registry as sequences
from skeleton.sequences.registry import SequenceSpec, register_sequence, sequence_problems
from skeleton.system import registry as tables
from tools.security_fixtures import actor_tenant, build_security_fixture

PASSWORD = "Fixture-only-password-2026!"
TABLE = "example_notes.note"
KEY = "example_notes.note_code"


@pytest.fixture
def numbered(skeleton_db, monkeypatch):
    monkeypatch.setattr(sequences, "_sequences", dict(sequences._sequences))
    register_sequence(SequenceSpec(KEY, template="NT-{year}-{number:03d}", owner_app="example_notes"))
    spec = tables.get_spec(TABLE)
    monkeypatch.setitem(
        tables._registry,
        TABLE,
        replace(spec, write_fields=("title", "body"), sequence_fields=(("code", KEY),)),
    )
    return build_security_fixture()


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def records(organization, sysid=None):
    kwargs = {"organization_id": organization.pk, "table_key": TABLE}
    if sysid:
        kwargs["sysid"] = sysid
    return reverse("api:record" if sysid else "api:records", kwargs=kwargs)


def post(client, url, data, method="post", revision=None):
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    if revision is not None:
        headers["HTTP_IF_MATCH"] = revision
    return getattr(client, method)(url, data=json.dumps(data), content_type="application/json", **headers)


def counter(fixture, organization):
    from skeleton.sequences.models import SequenceCounter

    user = fixture.admin if organization == fixture.a else fixture.bob
    with actor_tenant(user, organization):
        row = SequenceCounter.objects.filter(key=KEY).first()
        return row.last_value if row else 0


def year():
    return timezone.now().year


def test_api_and_form_fill_the_code_from_the_sequence(numbered):
    fixture = numbered
    client = login(fixture.admin)
    first = post(client, records(fixture.a), {"title": "First", "body": ""})
    assert first.status_code == 201, first.content
    assert first.json()["code"] == f"NT-{year()}-001"
    page = client.get(reverse("web:record-create", kwargs={"organization_id": fixture.a.pk, "table_key": TABLE}))
    assert page.status_code == 200 and 'name="code"' not in page.content.decode()
    form = client.post(
        reverse("web:record-create", kwargs={"organization_id": fixture.a.pk, "table_key": TABLE}),
        {"title": "Second", "body": "", "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert form.status_code == 302
    codes = sorted(
        row["code"] for row in client.get(records(fixture.a)).json()["results"] if row["title"] in ("First", "Second")
    )
    assert codes == [f"NT-{year()}-001", f"NT-{year()}-002"]
    # B numbers independently.
    other = post(login(fixture.bob), records(fixture.b), {"title": "In B", "body": ""})
    assert other.status_code == 201 and other.json()["code"] == f"NT-{year()}-001"


def test_clients_cannot_choose_or_change_the_number(numbered):
    fixture = numbered
    client = login(fixture.admin)
    assert post(client, records(fixture.a), {"title": "Mine", "body": "", "code": "NT-1999-999"}).status_code == 400
    assert counter(fixture, fixture.a) == 0
    created = post(client, records(fixture.a), {"title": "Real", "body": ""})
    sysid, etag = created.json()["sysid"], created["ETag"]
    changed = post(client, records(fixture.a, sysid), {"code": "NT-1999-999"}, method="patch", revision=etag)
    assert changed.status_code == 400
    assert client.get(records(fixture.a, sysid)).json()["code"] == f"NT-{year()}-001"


def test_a_refused_creation_consumes_no_number(numbered):
    fixture = numbered
    client = login(fixture.admin)
    assert post(client, records(fixture.a), {"title": "x" * 1000, "body": ""}).status_code == 400  # Too long.
    assert counter(fixture, fixture.a) == 0
    outsider = login(fixture.outsider)  # No membership in A.
    assert post(outsider, records(fixture.a), {"title": "Denied", "body": ""}).status_code in (403, 404)
    assert counter(fixture, fixture.a) == 0
    assert post(client, records(fixture.a), {"title": "Next", "body": ""}).json()["code"] == f"NT-{year()}-001"


def test_check_rejects_invalid_sequence_fields(numbered, monkeypatch):
    spec = tables.get_spec(TABLE)
    assert sequence_problems() == []
    for broken, problem in (
        (replace(spec, sequence_fields=(("missing", KEY),)), "does not exist"),
        (replace(spec, write_fields=("code", "title"), sequence_fields=(("code", KEY),)), "cannot be in write_fields"),
        (
            replace(spec, write_fields=("title",), sequence_fields=(("code", "example_notes.unknown"),)),
            "is not declared",
        ),
    ):
        monkeypatch.setitem(tables._registry, TABLE, broken)
        assert any(problem in item for item in sequence_problems()), problem
