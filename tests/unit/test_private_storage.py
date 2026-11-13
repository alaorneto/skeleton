"""Private attachment storage: UUID-only keys and production checks (#58)."""

import io
from uuid import uuid4

import pytest
from django.conf import settings
from django.test import override_settings

from skeleton.files import storage
from skeleton.files.checks import private_storage_checks
from skeleton.files.storage import StorageError, object_key, storage_problems


def test_keys_are_two_uuids_and_never_a_user_file_name():
    org, attachment = uuid4(), uuid4()
    assert object_key(org, attachment) == f"org/{org}/{attachment}"
    for bad in ("org/x/../../etc/passwd", f"org/{org}/report.pdf", f"{org}/{attachment}", "../secret"):
        with pytest.raises(StorageError):
            storage.open_object(bad)
    with pytest.raises(ValueError):
        object_key(org, "report.pdf")


def test_put_open_list_delete_and_size_limit(tmp_path):
    config = {
        **settings.STORAGES,
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": str(tmp_path), "base_url": None},
        },
    }
    with override_settings(STORAGES=config):
        from django.core.files.storage import storages

        storages._storages.pop("skeleton_files", None)
        org = uuid4()
        key = object_key(org, uuid4())
        assert storage.put_stream(key, io.BytesIO(b"hello"), max_bytes=10) == 5
        with storage.open_object(key) as handle:
            assert handle.read() == b"hello"
        with pytest.raises(StorageError):
            storage.put_stream(key, io.BytesIO(b"again"), max_bytes=10)
        assert storage.list_prefix(f"org/{org}/") == [key]
        too_big = object_key(org, uuid4())
        with pytest.raises(StorageError):
            storage.put_stream(too_big, io.BytesIO(b"x" * 11), max_bytes=10)
        assert storage.list_prefix(f"org/{org}") == [key]
        assert storage.presigned_url(key, filename="a.txt") is None
        storage.delete(key)
        assert storage.list_prefix(f"org/{org}") == []
        storages._storages.pop("skeleton_files", None)


@pytest.mark.parametrize(
    "config,message",
    [
        (None, "missing"),
        ({"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {}}, "explicit location"),
        (
            {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": "/srv/static/files"}},
            "STATIC_ROOT",
        ),
        (
            {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": "/srv/files", "base_url": "/media/"},
            },
            "base_url",
        ),
        ({"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"default_acl": "public-read"}}, "public ACL"),
        ({"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"querystring_auth": False}}, "sign URLs"),
        ({"BACKEND": "example.PublicCDN"}, "Unsupported"),
    ],
)
def test_public_or_missing_storage_fails_the_check(config, message):
    problems = storage_problems(config, static_root="/srv/static")
    assert any(message in problem for problem in problems), problems


def test_private_configurations_pass_and_the_deploy_check_reports_errors():
    assert (
        storage_problems(
            {"BACKEND": "django.core.files.storage.FileSystemStorage", "OPTIONS": {"location": "/srv/files"}},
            static_root="/srv/static",
        )
        == []
    )
    assert (
        storage_problems(
            {
                "BACKEND": "storages.backends.s3.S3Storage",
                "OPTIONS": {"default_acl": "private", "querystring_auth": True},
            },
            static_root="/srv/static",
        )
        == []
    )
    with override_settings(STORAGES={"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"}}):
        assert [error.id for error in private_storage_checks()] == ["skeleton.E024"]
    assert private_storage_checks() == []


class _ReadOnly:
    """A stream with only read(), like the upload service's hashing stream."""

    def __init__(self, data):
        self._data = io.BytesIO(data)

    def read(self, size=-1):
        return self._data.read(size)


def _read_based(monkeypatch):
    from tests.support import ReadBasedStorage

    backend = ReadBasedStorage()
    monkeypatch.setattr(storage, "private_storage", lambda: backend)
    return backend


def test_put_stream_counts_bytes_when_the_backend_reads_with_read(monkeypatch):
    """The S3 backend never calls chunks(): the size is measured in read() (#225)."""
    backend = _read_based(monkeypatch)
    key = object_key(uuid4(), uuid4())
    assert storage.put_stream(key, _ReadOnly(b"x" * 5000), max_bytes=10_000) == 5000
    assert len(backend.objects[key]) == 5000


def test_put_stream_enforces_the_limit_when_the_backend_reads_with_read(monkeypatch):
    backend = _read_based(monkeypatch)
    with pytest.raises(storage.ObjectTooLarge):
        storage.put_stream(object_key(uuid4(), uuid4()), _ReadOnly(b"x" * 5000), max_bytes=1000)
    assert backend.objects == {}


def test_put_stream_still_counts_and_limits_on_the_local_backend(tmp_path, monkeypatch):
    from django.core.files.storage import FileSystemStorage

    local = FileSystemStorage(location=tmp_path)
    monkeypatch.setattr(storage, "private_storage", lambda: local)
    key = object_key(uuid4(), uuid4())
    assert storage.put_stream(key, _ReadOnly(b"y" * 3000), max_bytes=10_000) == 3000
    with pytest.raises(storage.ObjectTooLarge):
        storage.put_stream(object_key(uuid4(), uuid4()), _ReadOnly(b"y" * 3000), max_bytes=100)
