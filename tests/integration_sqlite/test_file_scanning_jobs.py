"""Antivirus job states and orphan cleanup of the private storage (#62).

Runs on SQLite and, in the PostgreSQL job, with the runtime role.
"""

import io
from dataclasses import replace
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from skeleton.files import scanning, services
from skeleton.files.models import Attachment
from skeleton.files.orphans import clean_orphans, orphan_keys
from skeleton.files.scanning import ScannerUnavailable, Verdict
from skeleton.files.storage import list_prefix
from skeleton.system import registry
from skeleton.system.registry import AttachmentPolicy
from skeleton.tenancy.context import system_context
from skeleton.testing.jobs import run_pending_jobs
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario

pytestmark = pytest.mark.usefixtures("skeleton_db")
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
TXT = b"plain notes\n"


class FakeScanner:
    available = True

    def scan(self, stream):
        if not self.available:
            raise ScannerUnavailable("down")
        data = stream.read()
        return Verdict(clean=False, signature="Eicar-Signature") if EICAR in data else Verdict(clean=True)


@pytest.fixture
def scanner(monkeypatch):
    fake = FakeScanner()
    monkeypatch.setattr(scanning, "get_scanner", lambda: fake)
    return fake


@pytest.fixture
def scenario(monkeypatch, settings, tmp_path):
    from django.apps import apps

    if not apps.is_installed("examples.notes"):
        pytest.skip("SKELETON_ENABLE_EXAMPLE_APP=true is required")
    from examples.notes.models import Note

    key = "example_notes.note"
    monkeypatch.setitem(
        registry._registry,
        key,
        replace(registry._registry[key], attachments=AttachmentPolicy(max_bytes=1000, types=("text/plain",))),
    )
    settings.STORAGES = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
        },
    }
    settings.SKELETON_FILES_SCANNER = "clamav"
    return build_service_scenario(Note, slug="scan")


def login(user):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": FIXTURE_PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def upload(client, scenario, note, content, name="notes.txt"):
    target = reverse(
        "api:attachments",
        kwargs={"organization_id": scenario.a.pk, "table_key": "example_notes.note", "sysid": note.pk},
    )
    return client.post(
        target, {"file": SimpleUploadedFile(name, content)}, HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value
    )


def download(client, scenario, note, attachment_id):
    return client.get(
        reverse(
            "files:download",
            kwargs={
                "organization_id": scenario.a.pk,
                "table_key": "example_notes.note",
                "sysid": note.pk,
                "attachment_id": attachment_id,
            },
        )
    )


def state(scenario, attachment_id):
    with system_context(reason="test:scan-state", organization_id=scenario.a.pk):
        return Attachment.objects.with_deleted().get(pk=attachment_id)


def test_clean_file_becomes_available_and_eicar_is_quarantined(scenario, scanner):
    note = scenario.create(scenario.a, owner=scenario.member, code="S-1", title="S")
    client = login(scenario.member)
    clean = upload(client, scenario, note, TXT).json()
    infected = upload(client, scenario, note, b"note " + EICAR + b"\n", "eicar.txt").json()
    assert clean["state"] == infected["state"] == "pending"
    assert download(client, scenario, note, clean["sysid"]).status_code == 404  # Blocked until available.
    run_pending_jobs()
    assert state(scenario, clean["sysid"]).state == "available"
    quarantined = state(scenario, infected["sysid"])
    assert (quarantined.state, quarantined.scan_result_code) == ("quarantined", "infected:Eicar-Signature")
    assert quarantined.scanned_at is not None
    assert download(client, scenario, note, clean["sysid"]).status_code == 200
    assert download(client, scenario, note, infected["sysid"]).status_code == 404


def test_scanner_down_keeps_the_file_pending_and_retries(scenario, scanner):
    from skeleton.jobs.models import Job

    scanner.available = False
    note = scenario.create(scenario.a, owner=scenario.member, code="S-2", title="S")
    client = login(scenario.member)
    pending = upload(client, scenario, note, TXT).json()
    outcomes = run_pending_jobs()
    assert [outcome.state for outcome in outcomes if outcome.job_type == "files.scan"] == ["queued"]
    assert state(scenario, pending["sysid"]).state == "pending"
    assert download(client, scenario, note, pending["sysid"]).status_code == 404
    with system_context(reason="test:scan-retry"):
        job = Job.all_objects.get(job_type="files.scan")
        assert (job.state, job.attempts, job.last_error_class) == ("queued", 1, "ScannerUnavailable")
        assert job.run_after > timezone.now()


def test_without_scanner_files_are_available_at_once(scenario, settings):
    settings.SKELETON_FILES_SCANNER = "none"
    note = scenario.create(scenario.a, owner=scenario.member, code="S-3", title="S")
    assert upload(login(scenario.member), scenario, note, TXT).json()["state"] == "available"


def test_orphans_of_rolled_back_uploads_go_and_referenced_objects_stay(scenario, settings):
    settings.SKELETON_FILES_SCANNER = "none"
    spec = registry.get_spec("example_notes.note")
    note = scenario.create(scenario.a, owner=scenario.member, code="S-4", title="S")
    client = login(scenario.member)
    live = upload(client, scenario, note, TXT).json()
    removed = upload(client, scenario, note, TXT, "removed.txt").json()
    csrf = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value}
    detail = reverse(
        "api:attachment",
        kwargs={
            "organization_id": scenario.a.pk,
            "table_key": "example_notes.note",
            "sysid": note.pk,
            "attachment_id": removed["sysid"],
        },
    )
    assert client.delete(detail, HTTP_IF_MATCH='"1"', **csrf).status_code == 204
    with pytest.raises(RuntimeError), scenario.acting(scenario.member, scenario.a), transaction.atomic():
        services.upload(spec, note.pk, user=scenario.member, stream=io.BytesIO(TXT), filename="lost.txt")
        raise RuntimeError("rollback after the bytes were written")
    prefix = f"org/{scenario.a.pk}/"
    assert len(list_prefix(prefix)) == 3
    with system_context(reason="test:orphans", organization_id=scenario.a.pk):
        assert orphan_keys(scenario.a.pk) == []  # Younger than the grace period.
        assert clean_orphans(now=timezone.now() + timedelta(hours=2)) == 1
    keys = set(list_prefix(prefix))
    assert keys == {f"{prefix}{live['sysid']}", f"{prefix}{removed['sysid']}"}  # Live and soft-deleted stay.
