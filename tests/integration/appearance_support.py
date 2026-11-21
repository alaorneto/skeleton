"""Shared helpers of the instance appearance tests (epic #591)."""

import io
import struct
import zlib
from contextlib import contextmanager
from uuid import uuid4

from django.test import Client
from django.urls import reverse

PASSWORD = "Appearance-fixture-Password-2026!"  # noqa: S105


def platform_admin(email="appearance-admin@example.test"):
    from skeleton.accounts.services import bootstrap_identity

    user, _ = bootstrap_identity(email=email, organization="appearance-platform", name="Plataforma", password=PASSWORD)
    return user


def member(email="appearance-member@example.test"):
    from skeleton.accounts.services import create_identity
    from skeleton.tenancy.context import system_context

    with system_context(reason="test:appearance-member"):
        return create_identity(email=email, password=PASSWORD)


@contextmanager
def acting(user):
    from skeleton.tenancy.context import request_context

    with request_context(actor=user, request_id=uuid4(), source="test:appearance"):
        yield


def login(email, password=PASSWORD):
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


def png(width=512, height=512):
    """A real PNG of the given size (one grey pixel row repeated), built in memory."""
    raw = b"".join(b"\x00" + b"\x80" * width for _ in range(height))

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")


def upload(content, name="image.png"):
    stream = io.BytesIO(content)
    stream.name, stream.size = name, len(content)
    return stream


def audit_events(user, record_id):
    from skeleton.audit.models import AuditEvent

    with acting(user):
        return list(AuditEvent.objects.filter(record_sysid=record_id).order_by("created_at", "sysid"))
