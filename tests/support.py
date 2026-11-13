"""Helpers shared by the test modules of this repository (#336)."""

from django.test import Client
from django.urls import reverse


def login(user, *, password, csrf=True):
    """Client logged in through the real allauth form; fails the test if the login does not redirect.

    With ``csrf`` (the default) the client enforces CSRF, as a browser does: it reads the form
    first and posts its token. Without it, a plain client posts the credentials directly.
    """
    if not csrf:
        client = Client()
        assert client.post(reverse("account_login"), {"login": user.email, "password": password}).status_code == 302
        return client
    client = Client(enforce_csrf_checks=True)
    client.get(reverse("account_login"))
    response = client.post(
        reverse("account_login"),
        {"login": user.email, "password": password, "csrfmiddlewaretoken": client.cookies["csrftoken"].value},
    )
    assert response.status_code == 302
    return client


class ReadBasedStorage:
    """A storage that saves as django-storages' S3Storage does: by ``read()``, never ``chunks()`` (#225).

    Its ``_save`` copies the S3 backend's steps (rewind when seekable, then read in blocks), so
    the gateway's size and limit are proven on a backend that never calls ``File.chunks()``.
    """

    def __new__(cls):
        from django.core.files.base import ContentFile
        from django.core.files.storage import Storage

        class _Storage(Storage):
            def __init__(self):
                self.objects = {}

            def _save(self, name, content):
                if not hasattr(content, "seekable") or content.seekable():
                    content.seek(0)
                data = b""
                while True:
                    block = content.read(8192)
                    if not block:
                        break
                    data += block
                self.objects[name] = data
                return name

            def _open(self, name, mode="rb"):
                return ContentFile(self.objects[name], name=name)

            def exists(self, name):
                return name in self.objects

            def delete(self, name):
                self.objects.pop(name, None)

            def size(self, name):
                return len(self.objects[name])

            def listdir(self, path):
                prefix = path.rstrip("/") + "/"
                return [], [key[len(prefix) :] for key in self.objects if key.startswith(prefix)]

        return _Storage()
