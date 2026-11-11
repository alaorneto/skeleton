"""Header parsing and token-accepting views (#46), without the database."""

from django.test import RequestFactory
from django.urls import resolve

from skeleton.integrations.tokens.middleware import bearer, token_methods


def test_bearer_header_parsing():
    factory = RequestFactory()
    assert bearer(factory.get("/api/v1/me/", HTTP_AUTHORIZATION="Bearer skt_x")) == "skt_x"
    assert bearer(factory.get("/api/v1/me/", HTTP_AUTHORIZATION="bearer  skt_x ")) == "skt_x"
    assert bearer(factory.get("/api/v1/me/", HTTP_AUTHORIZATION="Basic dXNlcjpwYXNz")) is None
    assert bearer(factory.get("/api/v1/me/")) is None


def test_only_declared_views_accept_tokens():
    me = token_methods(resolve("/api/v1/me/"))
    assert "GET" in me and "HEAD" in me and "PATCH" not in me
    organization = "00000000-0000-0000-0000-000000000001"
    records = token_methods(resolve(f"/api/v1/o/{organization}/tables/example_notes.note/records/"))
    assert set(records) >= {"GET", "POST", "PATCH", "DELETE"}
    assert token_methods(resolve(f"/api/v1/o/{organization}/memberships/")) == ()
    assert token_methods(resolve(f"/api/v1/o/{organization}/audit/")) == ()
    assert token_methods(resolve(f"/api/v1/o/{organization}/webhooks/")) == ()
