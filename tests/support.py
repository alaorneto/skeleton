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
