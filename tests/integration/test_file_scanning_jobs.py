"""Antivirus job states and orphan cleanup of the private storage (#62).

Runs on PostgreSQL with the runtime role.
"""

import io
from datetime import timedelta

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import transaction
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
from skeleton.testing.registries import table_spec_override
from skeleton.testing.service_api import FIXTURE_PASSWORD, build_service_scenario
from tests import support

pytestmark = [pytest.mark.usefixtures("skeleton_db"), pytest.mark.needs_example_app]
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

    from examples.notes.models import Note

    key = "example_notes.note"
    with table_spec_override(key, attachments=AttachmentPolicy(max_bytes=1000, types=("text/plain",))):
        settings.STORAGES = {
            **settings.STORAGES,
            "skeleton_files": {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": str(tmp_path / "files"), "base_url": None},
            },
        }
        settings.SKELETON_FILES_SCANNER = "clamav"
        yield build_service_scenario(Note, slug="scan")


def login(user):
    return support.login(user, password=FIXTURE_PASSWORD)


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


def _rescan(organization, *, hours=2):
    with system_context(reason="test:rescan", organization_id=organization.pk):
        return scanning.rescan_pending(now=timezone.now() + timedelta(hours=hours))


def _kill_scan_jobs():
    from skeleton.jobs.models import Job

    with system_context(reason="test:scan-dead"):
        Job.all_objects.filter(job_type="files.scan").update(state=Job.State.DEAD, locked_by="", locked_until=None)


def test_pending_attachment_is_rescanned_after_the_scanner_returns(scenario, scanner):
    """The scan job ran out of attempts while the antivirus was down; the hourly sweep retries (#277)."""
    scanner.available = False
    note = scenario.create(scenario.a, owner=scenario.member, code="S-R1", title="S")
    pending = upload(login(scenario.member), scenario, note, TXT).json()
    _kill_scan_jobs()
    scanner.available = True
    run_pending_jobs()
    assert state(scenario, pending["sysid"]).state == "pending"  # Nothing retries without the sweep.
    assert _rescan(scenario.a) == 1
    run_pending_jobs()
    assert state(scenario, pending["sysid"]).state == "available"


def test_rescan_skips_recent_attachments_and_open_scans(scenario, scanner):
    scanner.available = False
    note = scenario.create(scenario.a, owner=scenario.member, code="S-R2", title="S")
    upload(login(scenario.member), scenario, note, TXT)
    assert _rescan(scenario.a, hours=0) == 0  # Pending for less than an hour.
    assert _rescan(scenario.a) == 0  # The original scan job is still queued.


def test_without_scanner_pending_attachments_stay_closed_and_jobs_do_not_fail(scenario, scanner, settings):
    scanner.available = False
    note = scenario.create(scenario.a, owner=scenario.member, code="S-R3", title="S")
    client = login(scenario.member)
    pending = upload(client, scenario, note, TXT).json()
    settings.SKELETON_FILES_SCANNER = "none"
    assert _rescan(scenario.a) == 0
    with system_context(reason="test:scan-off", organization_id=scenario.a.pk):
        assert scanning.scan_attachment(attachment_id=pending["sysid"]) == "skipped"
    assert state(scenario, pending["sysid"]).state == "pending"
    assert download(client, scenario, note, pending["sysid"]).status_code == 404


def test_rescan_is_scoped_to_its_organization(scenario, scanner):
    from skeleton.jobs.models import Job

    scanner.available = False
    note = scenario.create(scenario.a, owner=scenario.member, code="S-R4", title="S")
    upload(login(scenario.member), scenario, note, TXT)
    _kill_scan_jobs()
    assert _rescan(scenario.b) == 0
    with system_context(reason="test:scan-scope"):
        assert not Job.all_objects.filter(job_type="files.scan", job_organization_id=scenario.b.pk).exists()
