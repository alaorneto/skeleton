"""CSV export by job (#182), PostgreSQL with the runtime role.

Request, job, notification, download and retention; the negatives cover A/B, default deny,
another person (even the organization admin), revocation between request and download,
formula injection, sensitive fields and the row limit.
"""

import pytest
from django.test import Client, override_settings

from skeleton.service_api.errors import Forbidden, InvalidInput, NotFound
from skeleton.service_api.transfer import TransferService
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import (
    FIXTURE_PASSWORD,
    acting_as,
    assert_rejects_other_actor,
    assert_service_contract,
    build_service_scenario,
)
from skeleton.transfer import csv_format

pytestmark = pytest.mark.usefixtures("skeleton_db")
TABLE = "example_notes.note"


def test_contract():
    assert_service_contract(TransferService)


@pytest.fixture
def scenario():
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    scenario = build_service_scenario(Note, slug="transfer-export")
    scenario.visible = scenario.create(scenario.a, code="E-1", title="Contrato ação", body="segredo do corpo")
    scenario.formula = scenario.create(scenario.a, code="E-2", title='=HYPERLINK("http://x")', body="")
    scenario.hidden = scenario.create(
        scenario.a, owner=scenario.admin_a, code="E-3", title="Privado do admin", visibility="private"
    )
    scenario.foreign = scenario.create(scenario.b, code="E-4", title="Outra organização")
    return scenario


def _login(user):
    client = Client()
    assert client.post("/accounts/login/", {"login": user.email, "password": FIXTURE_PASSWORD}).status_code == 302
    return client


def _request(client, organization, **filters):
    response = client.post(
        f"/api/v1/o/{organization.pk}/exports/",
        {"table_key": TABLE, "filters": filters},
        content_type="application/json",
    )
    return response


def _download(client, organization, transfer_id):
    response = client.get(f"/o/{organization.pk}/transfers/{transfer_id}/download/")
    if response.status_code != 200:
        return response, None
    return response, b"".join(response.streaming_content)


def test_export_is_generated_notified_and_downloaded(scenario, subtests):
    from skeleton.notifications.models import Notification
    from skeleton.tenancy.context import system_context

    client = _login(scenario.member)
    created = _request(client, scenario.a)
    assert created.status_code == 202, created.content
    transfer = created.json()
    assert transfer["state"] == "queued" and transfer["download_url"] is None
    run_pending_jobs()
    detail = client.get(f"/api/v1/o/{scenario.a.pk}/transfers/{transfer['sysid']}/").json()
    with subtests.test("job finished"):
        assert detail["state"] == "succeeded" and detail["row_count"] == 2
        assert detail["download_url"] == f"/o/{scenario.a.pk}/transfers/{transfer['sysid']}/download/"
    with subtests.test("notified"):
        with system_context(reason="test:transfer-notification", organization_id=scenario.a.pk):
            notification = Notification.objects.get(type_code="transfer.finished")
        assert notification.payload == {"transfer": transfer["sysid"], "state": "succeeded"}
    response, body = _download(client, scenario.a, transfer["sysid"])
    with subtests.test("download headers"):
        assert response.status_code == 200
        assert response["Content-Disposition"].startswith("attachment;")
        assert response["Content-Type"].startswith("text/csv")
        assert (response["X-Content-Type-Options"], response["Cache-Control"]) == ("nosniff", "no-store")
    rows = csv_format.read(body)
    with subtests.test("CSV contents"):
        assert body.startswith(b"\xef\xbb\xbf")
        assert rows[0] == ["sysid", "revision", "code", "title", "owner", "visibility", "owning_group"]
        by_code = {row[2]: row for row in rows[1:]}
        assert set(by_code) == {"E-1", "E-2"}
        assert by_code["E-1"][3] == "Contrato ação" and by_code["E-1"][0] == str(scenario.visible.pk)
        assert by_code["E-2"][3] == '\'=HYPERLINK("http://x")'
        assert b"segredo" not in body and b"Privado" not in body and "Outra organização".encode() not in body
    with subtests.test("listing"):
        listed = client.get(f"/api/v1/o/{scenario.a.pk}/transfers/").json()
        assert [item["sysid"] for item in listed["results"]] == [transfer["sysid"]]
        page = client.get(f"/o/{scenario.a.pk}/transfers/")
        assert page.status_code == 200 and b"Baixar CSV" in page.content


def test_filters_are_the_list_filters(scenario, subtests):
    client = _login(scenario.member)
    created = _request(client, scenario.a, code="E-1")
    assert created.status_code == 202
    run_pending_jobs()
    _, body = _download(client, scenario.a, created.json()["sysid"])
    assert [row[2] for row in csv_format.read(body)[1:]] == ["E-1"]
    for filters in ({"body": "x"}, {"page": "1"}, {"unknown": "1"}):
        with subtests.test(filters=filters):
            response = _request(client, scenario.a, **filters)
            assert response.status_code == 400
    with subtests.test("web request keeps the filters"):
        response = client.post(f"/o/{scenario.a.pk}/tables/{TABLE}/export/", {"title": "Contrato ação"})
        assert response.status_code == 302 and response["Location"] == f"/o/{scenario.a.pk}/transfers/"


def test_only_the_requester_downloads(scenario, subtests):
    member = _login(scenario.member)
    transfer_id = _request(member, scenario.a).json()["sysid"]
    run_pending_jobs()
    admin = _login(scenario.admin_a)
    with subtests.test("organization admin"):
        assert _download(admin, scenario.a, transfer_id)[0].status_code == 404
        assert admin.get(f"/api/v1/o/{scenario.a.pk}/transfers/{transfer_id}/").status_code == 404
        assert admin.get(f"/api/v1/o/{scenario.a.pk}/transfers/").json()["count"] == 0
    with subtests.test("another organization"):
        assert _download(member, scenario.b, transfer_id)[0].status_code == 404
        assert member.get(f"/api/v1/o/{scenario.b.pk}/transfers/{transfer_id}/").status_code == 404
    with subtests.test("B export has B rows only"):
        other = _request(member, scenario.b).json()["sysid"]
        run_pending_jobs()
        _, body = _download(member, scenario.b, other)
        assert [row[2] for row in csv_format.read(body)[1:]] == ["E-4"]


def test_revocation_between_request_and_download(scenario):
    from skeleton.access import services as access_services
    from skeleton.access.models import TablePermission

    member = _login(scenario.member)
    transfer_id = _request(member, scenario.a).json()["sysid"]
    run_pending_jobs()
    assert _download(member, scenario.a, transfer_id)[0].status_code == 200
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for grant in TablePermission.objects.filter(table__model_label=TABLE, action="read"):
            access_services.revoke_link(grant, user=admin)
    assert _download(member, scenario.a, transfer_id)[0].status_code == 404


def test_revocation_before_the_job_fails_closed(scenario):
    from skeleton.access import services as access_services
    from skeleton.access.models import TablePermission
    from skeleton.transfer.models import TransferJob

    member = _login(scenario.member)
    transfer_id = _request(member, scenario.a).json()["sysid"]
    with acting_as(scenario.admin_a, scenario.a) as admin:
        for grant in TablePermission.objects.filter(table__model_label=TABLE, action="read"):
            access_services.revoke_link(grant, user=admin)
    run_pending_jobs()
    with acting_as(scenario.member, scenario.a):
        transfer = TransferJob.objects.get(pk=transfer_id)
    assert (transfer.state, transfer.error_code, transfer.storage_key) == ("failed", "unauthorized", "")


def test_default_deny_and_service_errors(scenario, subtests):
    service = TransferService()
    with subtests.test("REST default deny"):
        assert _request(_login(scenario.denied), scenario.a).status_code == 403
    with acting_as(scenario.denied, scenario.a) as denied, pytest.raises(Forbidden):
        service.request_export(actor=denied, table_key=TABLE)
    with acting_as(scenario.member, scenario.a) as member:
        with subtests.test("unknown table"), pytest.raises(NotFound):
            service.request_export(actor=member, table_key="sk_accounts.user")
        with subtests.test("invalid filters"), pytest.raises(InvalidInput):
            service.request_export(actor=member, table_key=TABLE, filters=(("body", "x"),))
        with subtests.test("missing transfer"), pytest.raises(NotFound):
            service.transfer(actor=member, transfer_id="00000000-0000-0000-0000-000000000001")
        with subtests.test("idempotency"):
            first = service.request_export(actor=member, table_key=TABLE, idempotency_key="k-1")
            again = service.request_export(actor=member, table_key=TABLE, idempotency_key="k-1")
            assert first.sysid == again.sysid
            with pytest.raises(InvalidInput):
                service.request_export(actor=member, table_key=TABLE, filters=(("code", "E-1"),), idempotency_key="k-1")
    assert_rejects_other_actor(scenario.caller(lambda actor: service.transfers(actor=scenario.denied)))


@override_settings(SKELETON_EXPORT_MAX_ROWS=1)
def test_over_the_limit_fails_without_a_partial_file(scenario):
    from skeleton.files import storage
    from skeleton.transfer.models import TransferJob

    member = _login(scenario.member)
    transfer_id = _request(member, scenario.a).json()["sysid"]
    run_pending_jobs()
    with acting_as(scenario.member, scenario.a):
        transfer = TransferJob.objects.get(pk=transfer_id)
    assert (transfer.state, transfer.error_code, transfer.row_count, transfer.storage_key) == (
        "failed",
        "too_many_rows",
        2,
        "",
    )
    assert not storage.private_storage().exists(f"org/{scenario.a.pk}/transfers/{transfer_id}")
    assert _download(member, scenario.a, transfer_id)[0].status_code == 404


def test_retention_deletes_row_and_bytes(scenario):
    from datetime import timedelta

    from django.utils import timezone

    from skeleton.files import storage
    from skeleton.tenancy.context import system_context
    from skeleton.transfer.models import TransferJob
    from skeleton.transfer.retention import purge_expired_transfers

    member = _login(scenario.member)
    transfer_id = _request(member, scenario.a).json()["sysid"]
    run_pending_jobs()
    key = f"org/{scenario.a.pk}/transfers/{transfer_id}"
    assert storage.private_storage().exists(key)
    with system_context(reason="test:transfer-retention", organization_id=scenario.a.pk):
        assert purge_expired_transfers(now=timezone.now()) == {"transfers": 0, "objects": 0}
        assert purge_expired_transfers(now=timezone.now() + timedelta(days=8)) == {"transfers": 1, "objects": 1}
        assert not TransferJob.objects.filter(pk=transfer_id).exists()
    assert not storage.private_storage().exists(key)
    assert _download(member, scenario.a, transfer_id)[0].status_code == 404
