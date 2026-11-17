"""Attachments in the portal (#534): only through the table's external reader, download only.

Runs on PostgreSQL with the runtime role.
"""

import io

import pytest
from django.urls import reverse

from examples.notes.models import Note
from skeleton.service_api import Forbidden, NotFound
from skeleton.service_api.files import FileService
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.external import acting_external, enable_portal, grant_external_access
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, acting_as, build_service_scenario

pytestmark = [pytest.mark.needs_example_app, pytest.mark.no_api_contract]
KEY = "example_notes.note"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 92
SHARED = "Compartilhado"


def shared_only(record, user):
    return record.title == SHARED


@pytest.fixture
def portal(skeleton_db, request):
    from allauth.account.models import EmailAddress

    from skeleton.accounts.services import create_identity

    reader = getattr(request, "param", shared_only)
    with table_spec_override(
        KEY, attachments=AttachmentPolicy(max_bytes=1000, types=("image/png",), external_reader=reader)
    ):
        scenario = build_service_scenario(Note, slug="portal-files")
        with system_context(reason="test:portal-person"):
            person = create_identity(email="person@portal-files.example.test", password=FIXTURE_PASSWORD)
            EmailAddress.objects.create(user=person, email=person.email, verified=True, primary=True)
        for organization in (scenario.a, scenario.b):
            enable_portal(organization)
        grant_external_access(scenario.a, person)
        yield scenario, person


def attach(scenario, organization, title):
    from skeleton.files.models import Attachment

    note = scenario.create(organization, code=title[:8], title=title, visibility="organization")
    with acting_as(scenario.member, organization) as actor:
        info = FileService().attach(
            actor=actor, table_key=KEY, record_id=note.pk, filename="f.png", stream=io.BytesIO(PNG)
        )
    with system_context(reason="test:portal-files-available", organization_id=organization.pk):
        Attachment.objects.filter(pk=info.sysid).update(state=Attachment.State.AVAILABLE)
    return note, info


def url(organization, attachment_id):
    return reverse("files:portal-download", kwargs={"organization_id": organization.pk, "attachment_id": attachment_id})


def client_for(user):
    from tests import support

    return support.login(user, password=FIXTURE_PASSWORD)


def test_the_reader_decides_on_the_live_record(portal):
    scenario, person = portal
    _, shared = attach(scenario, scenario.a, SHARED)
    _, private = attach(scenario, scenario.a, "Particular")
    client = client_for(person)
    response = client.get(url(scenario.a, shared.sysid))
    assert response.status_code == 200 and b"".join(response.streaming_content) == PNG
    assert client.get(url(scenario.a, private.sysid)).status_code == 404
    with acting_external(person, scenario.a) as actor:
        assert FileService().external_download_url(actor=actor, attachment_id=shared.sysid) == url(
            scenario.a, shared.sysid
        )
        with pytest.raises(NotFound):
            FileService().external_download_url(actor=actor, attachment_id=private.sysid)
    with acting_as(scenario.member, scenario.a) as member, pytest.raises(Forbidden):
        FileService().external_download_url(actor=member, attachment_id=shared.sysid)


def test_deleted_foreign_and_member_requests_are_404(portal):
    scenario, person = portal
    _, shared = attach(scenario, scenario.a, SHARED)
    _, foreign = attach(scenario, scenario.b, SHARED)
    client = client_for(person)
    assert client.get(url(scenario.b, foreign.sysid)).status_code == 404  # No access in B.
    assert client.get(url(scenario.a, foreign.sysid)).status_code == 404  # B's attachment through A.
    assert client_for(scenario.member).get(url(scenario.a, shared.sysid)).status_code == 404  # A member, not external.
    with acting_as(scenario.member, scenario.a) as actor:
        FileService().remove(actor=actor, attachment_id=shared.sysid, expected_revision=shared.revision + 1)
    assert client.get(url(scenario.a, shared.sysid)).status_code == 404


def test_download_only(portal):
    scenario, person = portal
    _, shared = attach(scenario, scenario.a, SHARED)
    client = client_for(person)
    assert (
        client.post(
            url(scenario.a, shared.sysid), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value}
        ).status_code
        == 405
    )
    for suffix in ("upload/", "remove/", "restore/"):
        assert client.post(f"/portal/{scenario.a.pk}/files/{shared.sysid}/{suffix}").status_code == 404


@pytest.mark.parametrize("portal", [None], indirect=True)
def test_without_an_external_reader_nothing_leaves(portal):
    scenario, person = portal
    _, shared = attach(scenario, scenario.a, SHARED)
    assert client_for(person).get(url(scenario.a, shared.sysid)).status_code == 404
