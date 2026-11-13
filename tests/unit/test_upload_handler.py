"""BoundedUploadHandler stops a multipart body before it reaches memory or disk (#242)."""

import inspect

import pytest
from django.conf import settings
from django.core.files.uploadhandler import StopUpload
from django.test import override_settings

from skeleton.common.uploads import OVERHEAD, BoundedUploadHandler

LIMIT = 1000


def handler():
    return BoundedUploadHandler(None)


@override_settings(SKELETON_UPLOAD_MAX_BYTES=LIMIT)
def test_handler_stops_when_declared_length_is_over_the_limit():
    bounded = handler()
    bounded.handle_raw_input(None, {}, LIMIT + OVERHEAD + 1, b"boundary")
    with pytest.raises(StopUpload):
        bounded.new_file("file", "a.bin", "application/octet-stream", 10)
    assert bounded.exceeded


@override_settings(SKELETON_UPLOAD_MAX_BYTES=LIMIT)
def test_handler_stops_when_received_bytes_pass_the_limit():
    bounded = handler()
    bounded.handle_raw_input(None, {}, 0, b"boundary")
    bounded.new_file("file", "a.bin", "application/octet-stream", 10)
    assert bounded.receive_data_chunk(b"x" * (LIMIT + OVERHEAD), 0) is not None
    with pytest.raises(StopUpload):
        bounded.receive_data_chunk(b"x", LIMIT + OVERHEAD)
    assert bounded.exceeded


@override_settings(SKELETON_UPLOAD_MAX_BYTES=LIMIT)
def test_handler_passes_chunks_through_under_the_limit():
    bounded = handler()
    bounded.handle_raw_input(None, {}, 100, b"boundary")
    bounded.new_file("file", "a.bin", "application/octet-stream", 10)
    assert bounded.receive_data_chunk(b"abc", 0) == b"abc"
    assert bounded.file_complete(3) is None and not bounded.exceeded


def test_upload_handlers_setting_puts_the_bounded_handler_first():
    assert settings.FILE_UPLOAD_HANDLERS[0] == "skeleton.common.uploads.BoundedUploadHandler"
    assert settings.FILE_UPLOAD_HANDLERS[1:] == [
        "django.core.files.uploadhandler.MemoryFileUploadHandler",
        "django.core.files.uploadhandler.TemporaryFileUploadHandler",
    ]


def test_package_upload_swaps_the_global_bound_for_its_own(monkeypatch):
    """The package view keeps its larger limit: the global handler leaves only this request (#242)."""
    from django.test import RequestFactory

    from skeleton.web.platform import updates

    request = RequestFactory().post("/platform/updates/upload/")
    seen = {}

    def capture(request):
        seen["handlers"] = list(request.upload_handlers)
        return None

    monkeypatch.setattr(updates, "_upload", capture)
    inspect.unwrap(updates.upload)(request)  # The view body, without the platform guard.
    handlers = seen["handlers"]
    assert isinstance(handlers[0], updates.BoundedPackageUpload)
    assert not any(isinstance(handler, BoundedUploadHandler) for handler in handlers)
    assert len(handlers) == len(settings.FILE_UPLOAD_HANDLERS)
