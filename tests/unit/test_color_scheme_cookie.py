"""Resolution of the colour scheme and its mirror cookie (#665, #666): no database."""

from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.http import HttpResponse
from django.test import RequestFactory

from skeleton.web.color_scheme import COOKIE, resolve, scheme_value, store_cookie, with_scheme


def request_with(cookie=None, user=None, host="testserver"):
    request = RequestFactory().get("/", HTTP_HOST=host)
    if cookie is not None:
        request.COOKIES[COOKIE] = cookie
    request.user = user if user is not None else AnonymousUser()
    return request


def person(color_scheme):
    return SimpleNamespace(is_authenticated=True, profile=SimpleNamespace(color_scheme=color_scheme))


def test_only_the_two_forced_values_survive(subtests):
    for value, expected in (("light", "light"), ("dark", "dark"), ("", ""), ("sepia", ""), ('"><x', ""), (None, "")):
        with subtests.test(value=value):
            assert scheme_value(value) == expected


def test_anonymous_reads_the_cookie_and_the_profile_wins_when_signed_in(subtests):
    with subtests.test(case="anonymous, cookie"):
        assert resolve(request_with("dark")) == "dark"
    with subtests.test(case="anonymous, garbage cookie"):
        assert resolve(request_with("DARK")) == ""
    with subtests.test(case="anonymous, no cookie"):
        assert resolve(request_with()) == ""
    with subtests.test(case="profile light over cookie dark"):
        assert resolve(request_with("dark", person("light"))) == "light"
    with subtests.test(case="profile system over cookie dark"):
        assert resolve(request_with("dark", person(""))) == ""


def test_the_theme_url_carries_the_scheme_only_when_forced():
    assert with_scheme("/brand/theme.css?v=abc", "dark") == "/brand/theme.css?v=abc&scheme=dark"
    assert with_scheme("/x/preview.css", "light") == "/x/preview.css?scheme=light"
    assert with_scheme("/brand/theme.css?v=abc", "") == "/brand/theme.css?v=abc"
    assert with_scheme("", "dark") == ""


def test_the_cookie_follows_the_session_domain_rule(settings, subtests):
    settings.SKELETON_ORGANIZATION_BASE_DOMAIN = "example.test"
    settings.ALLOWED_HOSTS = [".example.test", "custom.test", "testserver"]
    settings.SESSION_COOKIE_SECURE = True
    with subtests.test(host="organization host"):
        response = HttpResponse()
        store_cookie(request_with(host="acme.example.test"), response, "dark")
        morsel = response.cookies[COOKIE]
        assert morsel.value == "dark" and morsel["domain"] == ".example.test"
        assert morsel["httponly"] and morsel["secure"] and morsel["samesite"] == "Lax" and morsel["path"] == "/"
        assert int(morsel["max-age"]) == 365 * 24 * 60 * 60
    with subtests.test(host="custom domain"):
        response = HttpResponse()
        store_cookie(request_with(host="custom.test"), response, "light")
        assert response.cookies[COOKIE]["domain"] == ""
    with subtests.test(scheme="system deletes"):
        response = HttpResponse()
        store_cookie(request_with(host="acme.example.test"), response, "")
        morsel = response.cookies[COOKIE]
        assert morsel.value == "" and morsel["max-age"] == 0 and morsel["domain"] == ".example.test"
