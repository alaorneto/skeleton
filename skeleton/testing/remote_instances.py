"""A fake remote instance for tests of the central instance (#712): no network.

``with fake_remote(instance_id=..., organizations=[...]) as remote:`` replaces the transport of
``skeleton.integrations.remote_instances.client``; ``remote.calls`` lists the paths requested
and ``remote.status``/``remote.body`` force an answer. Pages hold ``page_size`` organizations.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4


@dataclass
class FakeRemote:
    instance_id: UUID
    organizations: list[dict[str, Any]]
    page_size: int = 2
    calls: list[str] = field(default_factory=list)
    status: int | None = None
    body: bytes | None = None
    token: str | None = None

    def answer(self, url: str, headers: Mapping[str, str]) -> tuple[int, bytes, bool]:
        parts = urlsplit(url)
        path = parts.path.removeprefix("/api/v1/platform/")
        self.calls.append(path)
        if self.token is not None and headers.get("Authorization") != f"Bearer {self.token}":
            return 401, b'{"code": "invalid_token"}', False
        if self.status is not None:
            return self.status, self.body or b"{}", False
        if self.body is not None:
            return 200, self.body, len(self.body) > 1024 * 1024
        query = parse_qs(parts.query)
        if path == "instance/":
            data: Any = {
                "instance_id": str(self.instance_id),
                "name": "Remota",
                "purpose": "",
                "product_name": "P",
                "core_version": "0.6.0",
                "billing_enabled": False,
                "apps": [],
                "generated_at": "2026-01-01T00:00:00+00:00",
            }
        elif path == "organizations/":
            start = int(query.get("cursor", ["0"])[0] or 0)
            page = self.organizations[start : start + self.page_size]
            more = start + self.page_size < len(self.organizations)
            data = {"results": page, "next_cursor": str(start + self.page_size) if more else None}
        else:
            return 404, b"{}", False
        return 200, json.dumps(data).encode(), False


def organization(name: str, *, updated_at: str = "2026-01-01T00:00:00+00:00") -> dict[str, Any]:
    return {
        "id": str(uuid4()),
        "name": name,
        "slug": name.lower(),
        "is_active": True,
        "created_at": updated_at,
        "members": {"active": 1, "limit": None, "external": 0, "service_accounts": 0},
        "apps": [],
        "bundle": None,
        "storage": {"used_bytes": 0, "quota_bytes": None, "location": None},
        "refreshed_at": updated_at,
        "updated_at": updated_at,
    }


@contextmanager
def fake_remote(
    *, instance_id: UUID | None = None, organizations: list[dict[str, Any]] | None = None, token: str | None = None
) -> Iterator[FakeRemote]:
    from skeleton.integrations.remote_instances import client

    remote = FakeRemote(instance_id or uuid4(), list(organizations or ()), token=token)
    original = client.transport
    client.transport = remote.answer
    try:
        yield remote
    finally:
        client.transport = original
