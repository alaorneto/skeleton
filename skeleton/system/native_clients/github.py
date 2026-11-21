"""Read the releases of a native client from GitHub (#606), only through ``skeleton.common.outbound_http``.

The host is fixed (the API host); the asset download answers a redirect to GitHub's download
host, followed by hand once, only to ``*.githubusercontent.com`` over HTTPS and without the
token, as the deployment agent does. GitHub only hands bytes over: nothing read here is trusted
before ``verification`` checks it.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

API = "https://api.github.com"
DOWNLOAD_HOST_SUFFIX = ".githubusercontent.com"
RELEASES_PER_PAGE = 20
logger = logging.getLogger("skeleton.system.native_clients")


class SourceError(Exception):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class SourceAsset:
    id: int
    name: str
    size: int


@dataclass(frozen=True)
class SourceRelease:
    tag: str
    assets: dict[str, SourceAsset] = field(default_factory=dict)


class GitHubSource:
    def __init__(self, repository: str, token_file: Path | None = None) -> None:
        self.repository = repository
        self.token_file = token_file

    def _headers(self, accept: str, *, token: bool = True) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "django-skeleton"}
        if token and self.token_file is not None:
            try:
                value = self.token_file.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                raise SourceError("token_unreadable") from None
            if not value or any(character.isspace() for character in value):
                raise SourceError("token_unreadable")
            headers["Authorization"] = f"Bearer {value}"
        return headers

    def releases(self) -> list[SourceRelease]:
        """The newest releases (drafts left out), oldest first so the sync publishes in order."""
        from skeleton.common import outbound_http

        url = f"{API}/repos/{self.repository}/releases?per_page={RELEASES_PER_PAGE}"
        try:
            response = outbound_http.request(
                "GET", url, headers=self._headers("application/vnd.github+json"), max_response_bytes=1024 * 1024
            )
        except (outbound_http.OutboundRequestFailed, outbound_http.OutboundRequestRefused):
            raise SourceError("source_unreachable") from None
        if response.status != 200 or response.truncated:
            raise SourceError("source_refused")
        try:
            rows = json.loads(response.body)
        except ValueError:
            raise SourceError("source_invalid") from None
        if not isinstance(rows, list):
            raise SourceError("source_invalid")
        releases = []
        for row in reversed(rows):
            if not isinstance(row, dict) or row.get("draft") or not isinstance(row.get("tag_name"), str):
                continue
            assets = {}
            for asset in row.get("assets") or ():
                if isinstance(asset, dict) and isinstance(asset.get("id"), int) and isinstance(asset.get("name"), str):
                    assets[asset["name"]] = SourceAsset(asset["id"], asset["name"], int(asset.get("size") or 0))
            releases.append(SourceRelease(row["tag_name"][:200], assets))
        return releases

    def fetch(self, asset: SourceAsset, sink: Any, *, limit: int) -> int:
        """Write the asset's bytes into ``sink``; returns the size. The caller verifies the hash."""
        from skeleton.common import outbound_http

        url = f"{API}/repos/{self.repository}/releases/assets/{asset.id}"
        try:
            first = outbound_http.download(
                url, sink, headers=self._headers("application/octet-stream"), max_bytes=limit
            )
            if first.status in (301, 302, 303, 307, 308):
                location = first.headers.get("location", "")
                parts = urlsplit(location)
                if parts.scheme != "https" or not (parts.hostname or "").endswith(DOWNLOAD_HOST_SUFFIX):
                    raise SourceError("redirect_refused")
                first = outbound_http.download(
                    location, sink, headers=self._headers("application/octet-stream", token=False), max_bytes=limit
                )
        except (outbound_http.OutboundRequestFailed, outbound_http.OutboundRequestRefused):
            raise SourceError("source_unreachable") from None
        if first.status != 200:
            raise SourceError("source_refused")
        if first.truncated:
            raise SourceError("file_too_large")
        return first.size

    def read(self, asset: SourceAsset, *, limit: int) -> bytes:
        buffer = io.BytesIO()
        self.fetch(asset, buffer, limit=limit)
        return buffer.getvalue()
