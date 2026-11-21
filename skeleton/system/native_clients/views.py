"""Anonymous download of native client updates (#606), outside ``/api/v1/``.

``/clients/<client_id>/<channel>/releases.<os>.json`` and ``.sig.json`` serve the live feed and
its envelope; ``/clients/<client_id>/<channel>/<file>`` serves a file that a live feed of the
channel lists. The bytes are exactly the verified ones (content-addressed storage), with
``ETag`` (the SHA-256), ``Content-Length`` and ``Cache-Control``; anything else is a 404.
Anonymous by design: a client too old to sign in must still update, and nothing here is
personal data.
"""

from __future__ import annotations

import re

from django.http import FileResponse, Http404, HttpResponse, HttpResponseNotFound
from django.views.decorators.http import require_safe

from . import services
from .config import CLIENT_ID, NAME
from .verification import FILE_NAME

FEED = re.compile(r"releases\.([a-z0-9][a-z0-9_-]{0,31})\.json\Z")
ENVELOPE = re.compile(r"releases\.([a-z0-9][a-z0-9_-]{0,31})\.sig\.json\Z")
FEED_CACHE = "public, max-age=60, must-revalidate"
FILE_CACHE = "public, max-age=31536000, immutable"


class DetachedBlob(HttpResponse):
    """Ready values only; the tenant middleware streams the bytes after its transaction (no ORM)."""

    detached = True

    def __init__(self, *, client_id: str, sha256: str, size: int | None, content_type: str, cache: str):
        super().__init__(status=200)
        self.client_id, self.sha256, self.size = client_id, sha256, size
        self.blob_type, self.cache = content_type, cache

    def materialize(self) -> HttpResponse:
        try:
            handle = services.open_blob(self.client_id, self.sha256)
        except OSError:
            return HttpResponseNotFound("Not found.")
        response = FileResponse(handle, content_type=self.blob_type)
        if self.size is not None:
            response["Content-Length"] = str(self.size)
        for name, value in self.items():
            if name.lower() not in {"content-type", "content-length"}:
                response[name] = value
        return _headers(response, self.sha256, self.cache)


def _headers(response: HttpResponse, sha256: str, cache: str) -> HttpResponse:
    response["ETag"] = f'"{sha256}"'
    response["Cache-Control"] = cache
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _blob(request, client_id: str, sha256: str, size: int | None, *, content_type: str, cache: str):
    if f'"{sha256}"' in {value.strip() for value in request.headers.get("If-None-Match", "").split(",")}:
        return _headers(HttpResponse(status=304), sha256, cache)
    return DetachedBlob(client_id=client_id, sha256=sha256, size=size, content_type=content_type, cache=cache)


@require_safe
def client_file(request, client_id: str, channel: str, name: str):
    if not CLIENT_ID.fullmatch(client_id) or not NAME.fullmatch(channel):
        raise Http404
    feed, envelope = FEED.fullmatch(name), ENVELOPE.fullmatch(name)
    if envelope is not None or feed is not None:
        release = services.live_release(client_id, channel, (envelope or feed).group(1))
        if release is None:
            raise Http404
        sha256 = release.envelope_sha256 if envelope is not None else release.feed_sha256
        return _blob(request, client_id, sha256, None, content_type="application/json", cache=FEED_CACHE)
    if not FILE_NAME.fullmatch(name):
        raise Http404
    entry = services.served_file(client_id, channel, name)
    if entry is None:
        raise Http404
    return _blob(
        request, client_id, entry["sha256"], entry["size"], content_type="application/octet-stream", cache=FILE_CACHE
    )
