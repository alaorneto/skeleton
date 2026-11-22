"""Light, dark or the system's (#665, #666): the rendering, the profile preference and its cookie."""

import html as html_lib
import json
import re

import psycopg
import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import update_profile
from skeleton.web.color_scheme import COOKIE
from tests.integration.appearance_support import PASSWORD, acting, login, platform_admin, png, upload
from tools.integration_database import disposable_database_pair

pytestmark = pytest.mark.usefixtures("skeleton_db", "live_appearance")
THEME_URL = re.compile(r'href="(/brand/theme\.css\?[^"]+)"')


def anonymous(scheme=None):
    client = Client()
    if scheme is not None:
        client.cookies[COOKIE] = scheme
    return client


def page(client, path="/accounts/login/"):
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response, response.content.decode()


def with_theme(admin, *, logo=False):
    from skeleton.appearance.images import replace_image
    from skeleton.appearance.services import activate_theme, current_brand, revision_of

    if logo:
        for kind in ("logo", "logo_dark"):
            with acting(admin):
                replace_image(
                    actor=admin,
                    kind=kind,
                    upload=upload(png(64, 32), "logo.png"),
                    expected_revision=revision_of(current_brand()),
                )
    with acting(admin):
        activate_theme(actor=admin, builtin="esmeralda", expected_revision=revision_of(current_brand()))


def theme_url(html):
    found = THEME_URL.search(html)
    assert found, "the page must link the theme"
    return html_lib.unescape(found.group(1))


def profile_revision(user):
    from skeleton.accounts.models import UserProfile

    return UserProfile.objects.get(user_id=user.pk).revision


def set_scheme(user, scheme):
    with acting(user):
        update_profile(user, data={"color_scheme": scheme})


def audit_changes(user):
    """The audit rows of the profile, read as the owner (RLS hides the global audit from the runtime role)."""
    query = (
        "SELECT e.changes FROM skeleton.sk_audit_auditevent e JOIN skeleton.sk_accounts_userprofile p"
        " ON p.sysid = e.record_sysid WHERE e.table_name = 'sk_accounts_userprofile' AND p.user_id = %s"
        " ORDER BY e.record_revision"
    )
    with psycopg.connect(disposable_database_pair().owner) as connection:
        return [row[0] for row in connection.execute(query, [user.pk]).fetchall()]


def switch(client, data, *, csrf=True, referer=None):
    payload = dict(data)
    if csrf:
        payload["csrfmiddlewaretoken"] = client.cookies["csrftoken"].value
    headers = {"HTTP_REFERER": referer} if referer is not None else {}
    return client.post(reverse("web:color-scheme"), payload, **headers)


# --- Rendering (#665) ---------------------------------------------------------------------------


def test_the_cookie_forces_the_scheme_of_the_page_before_login(subtests):
    for cookie, attribute, meta in (
        ("dark", ' data-color-scheme="dark"', "dark"),
        ("light", ' data-color-scheme="light"', "light"),
        (None, "", "light dark"),
        ("sepia", "", "light dark"),
        ('"><script>', "", "light dark"),
    ):
        with subtests.test(cookie=cookie):
            response, html = page(anonymous(cookie))
            assert re.search(r"<html lang=\"[^\"]+\"" + re.escape(attribute) + ">", html)
            assert f'<meta name="color-scheme" content="{meta}">' in html
            assert "<script>" not in html.split("</head>")[0].replace('<script src="', "")
            assert 'class="dark"' not in html
            assert "Cookie" in response["Vary"]


def test_the_theme_stylesheet_is_resolved_for_the_forced_scheme(subtests):
    admin = platform_admin()
    with_theme(admin)
    _, system = page(anonymous())
    plain = theme_url(system)
    assert "scheme=" not in plain
    for scheme, literal in (("dark", "@media (min-width:0)"), ("light", "@media (not (min-width:0))")):
        with subtests.test(scheme=scheme):
            _, html = page(anonymous(scheme))
            url = theme_url(html)
            assert url == f"{plain}&scheme={scheme}"
            response = Client().get(url)
            css = response.content.decode()
            assert response.status_code == 200 and response["Content-Type"].startswith("text/css")
            assert response["Cache-Control"] == "public, max-age=31536000, immutable"
            assert response["ETag"].endswith(f'-{scheme}"')
            assert literal in css and "prefers-color-scheme" not in css
    with subtests.test(scheme="system"):
        assert "@media (prefers-color-scheme: dark)" in Client().get(plain).content.decode()
    with subtests.test(scheme="invalid"):
        assert Client().get(f"{plain}&scheme=sepia").status_code == 400
    with subtests.test(scheme="stale version"):
        stale = Client().get("/brand/theme.css?v=0000&scheme=dark")
        assert stale.status_code == 200 and stale["Cache-Control"] == "public, max-age=300"


def test_the_logo_variant_is_picked_on_the_server_when_forced(subtests):
    admin = platform_admin()
    with_theme(admin, logo=True)
    with subtests.test(scheme="system"):
        _, html = page(anonymous())
        assert '<source media="(prefers-color-scheme: dark)" srcset="/brand/logo-dark/?v=' in html
    with subtests.test(scheme="dark"):
        _, html = page(anonymous("dark"))
        assert "<picture>" not in html and 'src="/brand/logo-dark/?v=' in html
    with subtests.test(scheme="light"):
        _, html = page(anonymous("light"))
        assert "<picture>" not in html and "/brand/logo-dark/" not in html and 'src="/brand/logo/?v=' in html


def test_the_organization_brand_follows_the_profile_scheme():
    from skeleton.accounts.services import bootstrap_identity
    from skeleton.tenancy.organization_settings import update_settings
    from skeleton.testing.service_api import acting_as

    admin, organization = bootstrap_identity(
        email="scheme-org@example.test", organization="scheme-org", name="Org", password=PASSWORD
    )
    with acting_as(admin, organization) as actor:
        update_settings(
            actor=actor,
            data={"display_name": "Org", "default_language": "", "default_timezone": "", "brand_color": "#9f1239"},
            expected_revision=0,
        )
    set_scheme(admin, "dark")
    _, html = page(login(admin.email), f"/o/{organization.pk}/")
    style = re.search(r"<style nonce=\"[^\"]+\">(.*?)</style>", html, re.S).group(1)
    assert "--color-brand:#9f1239" in style and "@media (min-width:0)" in style
    assert "prefers-color-scheme" not in style


# --- Preference (#666) --------------------------------------------------------------------------


def test_the_profile_page_saves_the_scheme_and_mirrors_it_in_the_cookie():
    admin = platform_admin()
    client = login(admin.email)
    _, html = page(client, "/profile/")
    assert 'name="color_scheme"' in html and "Seguir o sistema" in html
    form = {
        "first_name": "",
        "last_name": "",
        "display_name": "",
        "timezone": "",
        "language": "",
        "color_scheme": "dark",
        "user_revision": type(admin).objects.get(pk=admin.pk).revision,
        "profile_revision": profile_revision(admin),
        "csrfmiddlewaretoken": client.cookies["csrftoken"].value,
    }
    response = client.post("/profile/", form)
    assert response.status_code == 302, response.content.decode()[:500]
    assert response.cookies[COOKIE].value == "dark"
    _, html = page(client, "/profile/")
    assert ' data-color-scheme="dark"' in html
    assert any(changes.get("color_scheme", {}).get("new") == "dark" for changes in audit_changes(admin))


def test_the_menu_switch_saves_and_goes_back(subtests):
    admin = platform_admin()
    client = login(admin.email)
    _, html = page(client, "/profile/privacy/")
    assert f'action="{reverse("web:color-scheme")}"' in html
    assert re.search(r'value="" aria-pressed="true"', html)  # The system's, by default.
    with subtests.test(step="dark"):
        response = switch(
            client,
            {"color_scheme": "dark", "expected_revision": profile_revision(admin)},
            referer="http://testserver/profile/privacy/?aba=1",
        )
        assert (response.status_code, response["Location"]) == (303, "http://testserver/profile/privacy/?aba=1")
        assert response.cookies[COOKIE].value == "dark"
        _, html = page(client, "/profile/privacy/")
        assert ' data-color-scheme="dark"' in html and re.search(r'value="dark" aria-pressed="true"', html)
    with subtests.test(step="system deletes the cookie"):
        response = switch(client, {"color_scheme": "", "expected_revision": profile_revision(admin)})
        assert response.status_code == 303 and response.cookies[COOKIE]["max-age"] == 0
        assert response["Location"] == reverse("web:profile")  # No Referer: the profile.
    with subtests.test(step="foreign referer"):
        for target in ("https://evil.test/", "//evil.test/x", "http://testserver.evil.test/", "javascript:alert(1)"):
            response = switch(
                client, {"color_scheme": "light", "expected_revision": profile_revision(admin)}, referer=target
            )
            assert response["Location"] == reverse("web:profile"), target
    with subtests.test(step="the form never carries the path"):
        _, html = page(client, "/profile/privacy/")
        form = html[
            html.index('action="/me/appearance/"') : html.index("</form>", html.index('action="/me/appearance/"'))
        ]
        assert "/profile/privacy/" not in form and 'name="next"' not in form


def test_the_menu_switch_refuses_what_it_must(subtests):
    admin = platform_admin()
    client = login(admin.email)
    revision = profile_revision(admin)
    with subtests.test(case="stale revision"):
        response = switch(client, {"color_scheme": "dark", "expected_revision": revision + 5})
        assert response.status_code == 303 and COOKIE not in response.cookies
        assert profile_revision(admin) == revision
    with subtests.test(case="invalid value"):
        assert switch(client, {"color_scheme": "sepia", "expected_revision": revision}).status_code == 400
    with subtests.test(case="extra field"):
        for extra in ({"user": str(admin.pk)}, {"next": "/"}):
            response = switch(client, {"color_scheme": "dark", "expected_revision": revision, **extra})
            assert response.status_code == 400, extra
    with subtests.test(case="no revision"):
        assert switch(client, {"color_scheme": "dark"}).status_code == 400
    with subtests.test(case="no csrf"):
        assert switch(client, {"color_scheme": "dark", "expected_revision": revision}, csrf=False).status_code == 403
    with subtests.test(case="anonymous"):
        response = Client().post(reverse("web:color-scheme"), {"color_scheme": "dark", "expected_revision": 1})
        assert response.status_code == 302 and reverse("account_login") in response["Location"]
    with subtests.test(case="get"):
        assert client.get(reverse("web:color-scheme")).status_code == 405
    assert profile_revision(admin) == revision


def test_login_brings_the_cookie_and_logout_keeps_it():
    admin = platform_admin()
    set_scheme(admin, "dark")
    client = login(admin.email)
    assert client.cookies[COOKIE].value == "dark"  # A new browser gets it on the first signed-in response.
    response = client.post(reverse("account_logout"), {"csrfmiddlewaretoken": client.cookies["csrftoken"].value})
    assert response.status_code == 302
    assert client.cookies[COOKIE].value == "dark"
    _, html = page(client)
    assert ' data-color-scheme="dark"' in html


def test_the_profile_wins_over_an_old_cookie():
    admin = platform_admin()
    client = login(admin.email)
    client.cookies[COOKIE] = "dark"
    response, html = page(client, "/profile/")
    assert "data-color-scheme" not in html
    assert response.cookies[COOKIE]["max-age"] == 0  # The mirror follows the profile (system).


def test_the_me_api_reads_and_changes_the_scheme(subtests):
    admin = platform_admin()
    client = login(admin.email)
    url = reverse("api:me")
    initial = client.get(url)
    assert initial.json()["profile"]["color_scheme"] == ""
    headers = {"HTTP_X_CSRFTOKEN": client.cookies["csrftoken"].value, "HTTP_IF_MATCH": initial["ETag"]}
    with subtests.test(case="invalid"):
        bad = client.patch(url, data=json.dumps({"color_scheme": "sepia"}), content_type="application/json", **headers)
        assert bad.status_code == 400
    with subtests.test(case="dark"):
        edited = client.patch(
            url, data=json.dumps({"color_scheme": "dark"}), content_type="application/json", **headers
        )
        assert edited.status_code == 200 and edited.json()["profile"]["color_scheme"] == "dark"
        assert COOKIE not in edited.cookies  # /api/ never gets the cookie.
    with subtests.test(case="no If-Match"):
        missing = client.patch(
            url,
            data=json.dumps({"color_scheme": "light"}),
            content_type="application/json",
            HTTP_X_CSRFTOKEN=client.cookies["csrftoken"].value,
        )
        assert missing.status_code == 428
