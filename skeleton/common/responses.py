"""Responses that stream only after the tenant transaction has committed.

The tenant middleware refuses streaming responses inside its database
context. A download view authorizes inside the context and returns a
``DetachedFileResponse`` holding only ready values (object key, file name,
type); the middleware turns it into the real response after the context
ended, so streaming the bytes never touches the ORM or holds the transaction.
"""

from __future__ import annotations

from urllib.parse import quote

from django.conf import settings
from django.http import FileResponse, HttpResponse, HttpResponseRedirect

SAFE_TYPES = frozenset(
    {
        "application/pdf",
        "image/png",
        "image/jpeg",
        "image/gif",
        "image/webp",
        "text/plain",
        "text/csv",
        "application/zip",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }
)


def content_disposition(filename: str) -> str:
    fallback = "".join(
        char if char.isascii() and char.isprintable() and char not in '"\\;' else "_" for char in filename
    )
    return f"attachment; filename=\"{fallback or 'download'}\"; filename*=UTF-8''{quote(filename)}"


def protect(response: HttpResponse, *, filename: str) -> HttpResponse:
    """Headers that keep any file from executing in the browser; always a download, never inline."""
    response["Content-Disposition"] = content_disposition(filename)
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "sandbox"
    response["Cache-Control"] = "no-store"
    response["Referrer-Policy"] = "no-referrer"
    return response


class DetachedFileResponse(HttpResponse):
    """A placeholder carrying ready values; ``materialize()`` runs after the context, without the ORM."""

    detached = True

    def __init__(self, *, storage_key: str, filename: str, content_type: str):
        super().__init__(status=200)
        self.storage_key = storage_key
        self.filename = filename
        self.file_type = content_type if content_type in SAFE_TYPES else "application/octet-stream"

    def materialize(self) -> HttpResponse:
        from skeleton.files import storage

        accel = getattr(settings, "SKELETON_FILES_ACCEL", "")
        if accel == "nginx":
            response = HttpResponse(content_type=self.file_type)
            prefix = getattr(settings, "SKELETON_FILES_ACCEL_PREFIX", "/_protected/")
            response["X-Accel-Redirect"] = prefix.rstrip("/") + "/" + self.storage_key
        elif accel == "sendfile":
            response = HttpResponse(content_type=self.file_type)
            response["X-Sendfile"] = storage.private_storage().path(self.storage_key)
        else:
            url = storage.presigned_url(self.storage_key, filename=self.filename, expires=60)
            if url is not None:
                response = HttpResponseRedirect(url)
            else:
                response = FileResponse(storage.open_object(self.storage_key), content_type=self.file_type)
        for name, value in self.items():
            if name.lower() not in {"content-type", "content-length"} and name not in response:
                response[name] = value
        return protect(response, filename=self.filename)
