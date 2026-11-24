"""Storage locations of the platform (#704), per organization (#705) and chosen at creation (#706).

PostgreSQL with the runtime role. The secret is encrypted and audited as ``[REDACTED]``;
organization A's objects live in its location and B's in the instance's storage; changing a
location is refused while the organization has files; the creation picks the chosen location,
the default one or none.
"""

import io
from uuid import uuid4

import pytest
from cryptography.fernet import Fernet
from django.core.exceptions import PermissionDenied, ValidationError
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity, create_identity
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.files import locations, storage
from skeleton.tenancy.context import request_context, system_context

pytestmark = [pytest.mark.usefixtures("skeleton_db")]
PASSWORD = "Storage-locations-Password!704"


@pytest.fixture
def setup(settings, tmp_path):
    settings.FILES_STORAGE_ENCRYPTION_KEYS = [Fernet.generate_key().decode()]
    settings.SKELETON_FILES_LOCATIONS_ROOT = str(tmp_path)
    platform, org_a = bootstrap_identity(
        email="locations-platform@example.test", organization="locations-a", name="A", password=PASSWORD
    )
    admin_b, org_b = bootstrap_identity(
        email="locations-b@example.test", organization="locations-b", name="B", password=PASSWORD
    )
    with system_context(reason="test:locations-demote"):
        admin_b.is_staff = admin_b.is_superuser = False
        admin_b.save(update_fields=["is_staff", "is_superuser"])
    yield platform, org_a, admin_b, org_b, tmp_path
    locations._cache.clear()
    locations._storages.clear()


def as_platform(user, fn):
    with request_context(actor=user, request_id=uuid4(), source="test:locations"):
        return fn()


S3 = {
    "code": "s3-main",
    "label": "Principal",
    "backend": "s3",
    "bucket_name": "skeleton-files",
    "region_name": "sa-east-1",
    "endpoint_url": "https://s3.example.test",
    "key_prefix": "tenants",
    "access_key_id": "AKIAEXAMPLE",
    "secret_access_key": "very-secret-value",
}


def save(platform, data, **kwargs):
    return as_platform(platform, lambda: locations.save_location(actor=platform, data=data, **kwargs))


def directory(platform, code, *, default=False):
    return save(
        platform,
        {"code": code, "label": code.upper(), "backend": "filesystem", "directory": code, "is_default": default},
    )


def test_s3_location_secret_is_encrypted_and_redacted(setup):
    from django.db import connection

    from skeleton.audit.models import AuditEvent

    platform, *_ = setup
    location = save(platform, S3)
    with connection.cursor() as cursor:
        cursor.execute("SELECT secret_access_key FROM sk_files_storagelocation WHERE sysid = %s", [location.pk])
        [stored] = cursor.fetchone()
    assert "very-secret-value" not in stored
    events = as_platform(
        platform,
        lambda: list(AuditEvent.objects.filter(record_sysid=location.pk).values_list("changes", flat=True)),
    )
    assert events and all("very-secret-value" not in str(event) for event in events)
    # An empty secret keeps the stored one.
    edited = save(
        platform, {**S3, "label": "Renomeado", "secret_access_key": ""}, sysid=location.pk, expected_revision=1
    )
    assert edited.secret_access_key.reveal() == "very-secret-value"
    pytest.importorskip("storages")  # The optional [s3] extra.
    built = locations.build_storage(edited)
    assert (built.bucket_name, built.location, built.default_acl, built.querystring_auth) == (
        "skeleton-files",
        "tenants",
        "private",
        True,
    )


def test_invalid_locations(setup, settings):
    platform, *_ = setup
    save(platform, S3)
    for case in (
        {**S3, "code": "other", "endpoint_url": "http://s3.example.test"},
        {**S3, "code": "other", "endpoint_url": "https://user:pw@s3.example.test"},
        {**S3, "code": "other", "bucket_name": "Bad_Bucket"},
        {**S3, "code": "other", "key_prefix": "../up"},
        {**S3},  # The same code again.
    ):
        with pytest.raises(ValidationError):
            save(platform, case)
    settings.FILES_STORAGE_ENCRYPTION_KEYS = []
    with pytest.raises(ValidationError) as missing:
        save(platform, {**S3, "code": "no-keys"})
    assert "FILES_STORAGE_ENCRYPTION_KEYS" in str(missing.value)
    settings.SKELETON_FILES_DIRECTORY_LOCATIONS = False  # The production profile.
    with pytest.raises(ValidationError):
        directory(platform, "prod-dir")


def test_one_default_and_guard(setup):
    platform, _, admin_b, _, _ = setup
    first = directory(platform, "l1", default=True)
    second = directory(platform, "l2", default=True)
    first.refresh_from_db()
    assert not first.is_default and second.is_default
    with request_context(actor=admin_b, request_id=uuid4(), source="test:locations-denied"):
        with pytest.raises(PermissionDenied):
            locations.save_location(actor=admin_b, data={**S3, "code": "x"})


def test_objects_follow_the_organization_location(setup):
    platform, org_a, _, org_b, root = setup
    l1 = directory(platform, "l1")
    as_platform(platform, lambda: locations.assign_storage(actor=platform, organization=org_a, location=l1))
    key_a = storage.object_key(org_a.pk, uuid4())
    key_b = storage.object_key(org_b.pk, uuid4())
    storage.put_stream(key_a, io.BytesIO(b"a"), max_bytes=10)
    storage.put_stream(key_b, io.BytesIO(b"b"), max_bytes=10)
    assert (root / "l1" / key_a).exists()
    assert not (root / "l1" / key_b).exists()
    assert storage.open_object(key_a).read() == b"a"
    assert storage.list_prefix(f"org/{org_a.pk}") == [key_a]
    assert storage.storage_for_key(key_b) is storage.private_storage()
    assert storage.presigned_url(key_a, filename="a.txt") is None  # A directory has no URLs.
    # A has files now: changing its location is refused; deactivating L1 too.
    l2 = directory(platform, "l2")
    with pytest.raises(ValidationError) as in_use:
        as_platform(platform, lambda: locations.assign_storage(actor=platform, organization=org_a, location=l2))
    assert in_use.value.error_dict["location"][0].code == "storage_in_use"
    with pytest.raises(ValidationError):
        as_platform(
            platform, lambda: locations.deactivate_location(actor=platform, sysid=l1.pk, expected_revision=l1.revision)
        )
    storage.delete(key_a)
    assert not (root / "l1" / key_a).exists()
    as_platform(platform, lambda: locations.assign_storage(actor=platform, organization=org_a, location=l2))
    assert storage.storage_for_organization(org_a.pk).location.endswith("l2")
    with pytest.raises(ConcurrentUpdate):
        as_platform(platform, lambda: locations.deactivate_location(actor=platform, sysid=l1.pk, expected_revision=99))
    as_platform(
        platform, lambda: locations.deactivate_location(actor=platform, sysid=l1.pk, expected_revision=l1.revision)
    )


def test_creation_picks_the_location(setup):
    from skeleton.files.models import OrganizationStorage
    from skeleton.tenancy.services import create_organization

    platform, *_ = setup

    def create(slug, **kwargs):
        with system_context(reason="test:locations-owner"):
            owner = create_identity(email=f"{slug}@locations.example.test", password=PASSWORD)
        with request_context(actor=platform, request_id=uuid4(), source="test:locations-create"):
            return create_organization(owner=owner, slug=slug, name=slug, **kwargs)

    def location_of(organization):
        row = OrganizationStorage.objects.filter(storage_organization=organization).first()
        return row.location.code if row else None

    assert location_of(create("loc-none")) is None  # No default: the instance's storage.
    l1 = directory(platform, "l1")
    directory(platform, "l2", default=True)
    assert location_of(create("loc-chosen", storage_location=l1)) == "l1"
    assert location_of(create("loc-default")) == "l2"
    assert location_of(create("loc-instance", storage_location=None)) is None
    with pytest.raises(ValidationError):  # L1 serves an organization now.
        as_platform(
            platform, lambda: locations.deactivate_location(actor=platform, sysid=l1.pk, expected_revision=l1.revision)
        )
    l3 = directory(platform, "l3")
    as_platform(
        platform, lambda: locations.deactivate_location(actor=platform, sysid=l3.pk, expected_revision=l3.revision)
    )
    l3.refresh_from_db()
    from skeleton.tenancy.models import Organization

    with pytest.raises(ValidationError):
        create("loc-inactive", storage_location=l3)
    with system_context(reason="test:locations-none"):
        assert not Organization.objects.filter(slug="loc-inactive").exists()


def login(user):
    client = Client(enforce_csrf_checks=True)
    assert client.get(reverse("account_login")).status_code == 200
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": PASSWORD, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


# The location's code, label and directory in ``test_pages``. The hyphens keep it from turning up by
# chance inside the page's CSRF tokens, which are only letters and digits.
HIDDEN = "hidden-location-l1"


def test_pages(setup):
    platform, org_a, admin_b, _, root = setup
    client = login(platform)

    def post(url, data, who=client):
        return who.post(url, {**data, "csrfmiddlewaretoken": who.cookies["csrftoken"].value})

    assert client.get(reverse("platform:storage")).status_code == 200
    created = post(
        reverse("platform:storage-create"),
        {
            "code": HIDDEN,
            "label": HIDDEN.upper(),
            "backend": "filesystem",
            "directory": HIDDEN,
            "addressing_style": "auto",
            "expected_revision": 0,
        },
    )
    assert created.status_code == 302
    from skeleton.files.models import StorageLocation

    l1 = StorageLocation.objects.get(code=HIDDEN)
    page = client.get(reverse("platform:storage-location", kwargs={"sysid": l1.pk}))
    assert page.status_code == 200 and "very-secret" not in page.content.decode()
    tested = post(reverse("platform:storage-location-test", kwargs={"sysid": l1.pk}), {})
    assert tested.context["result"].ok
    moved = post(
        reverse("platform:organization-storage-location", kwargs={"sysid": org_a.pk}), {"location": str(l1.pk)}
    )
    assert moved.status_code == 302
    shown = client.get(reverse("platform:organization", kwargs={"sysid": org_a.pk}))
    assert shown.context["storage_location"]["current"] == l1
    other = login(admin_b)
    assert other.get(reverse("platform:storage")).status_code in (403, 404)
    # The organization's own pages never name the location: not its code, label or directory
    # (all ``HIDDEN``, case aside), nor its sysid.
    settings_page = client.get(reverse("web:organization-settings", kwargs={"organization_id": org_a.pk}))
    assert settings_page.status_code == 200
    main = settings_page.content.decode().lower().split("<main")[-1]
    assert HIDDEN not in main and str(l1.pk) not in main
