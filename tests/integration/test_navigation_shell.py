"""Navigation shell: organization switcher, organization sidebar and account area."""

import pytest
from django.test import Client
from django.urls import reverse

from skeleton.accounts.services import bootstrap_identity

pytestmark = pytest.mark.usefixtures("skeleton_db")
PASSWORD = "Navigation-shell-password!83"


def logged_client(slug):
    actor, organization = bootstrap_identity(
        email=f"{slug}@example.test", organization=slug, name=f"Org {slug}", password=PASSWORD
    )
    client = Client(raise_request_exception=False)
    assert client.post("/accounts/login/", {"login": actor.email, "password": PASSWORD}).status_code == 302
    return client, actor, organization


def test_switcher_lists_only_own_memberships_and_marks_current_organization():
    client, _, organization = logged_client("nav-a")
    _, foreign = bootstrap_identity(email="nav-b@example.test", organization="nav-b", name="Foreign", password=PASSWORD)
    for path in ("/", f"/o/{organization.pk}/", "/profile/"):
        response = client.get(path)
        assert response.status_code == 200
        assert [item["organization_id"] for item in response.context["organization_memberships"]] == [organization.pk]
        html = response.content.decode()
        assert "Foreign" not in html and str(foreign.pk) not in html
    dashboard = client.get(f"/o/{organization.pk}/")
    assert dashboard.context["current_organization"]["organization_id"] == organization.pk
    assert 'aria-label="Organização"' in dashboard.content.decode()
    assert "current_organization" not in client.get("/").context
    # The switcher never grants access: a foreign organization stays closed.
    assert client.get(f"/o/{foreign.pk}/").status_code in (403, 404)


@pytest.mark.parametrize(
    "resource,section,siblings",
    (
        ("member-roles", "people", ["memberships", "invitations", "member-roles"]),
        ("group-roles", "groups", ["groups", "group-members", "group-roles"]),
        ("record-grants", "roles", ["roles", "table-permissions", "record-grants"]),
    ),
)
def test_access_screens_are_grouped_by_section_with_sibling_tabs(resource, section, siblings):
    client, _, organization = logged_client(f"nav-{section}")
    response = client.get(reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": resource}))
    assert response.status_code == 200
    current = response.context["access_section"]
    assert current["id"] == section
    assert [tab["resource"] for tab in current["tabs"]] == siblings
    assert [tab["resource"] for tab in current["tabs"] if tab["current"]] == [resource]
    html = response.content.decode()
    for sibling in siblings:
        url = reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": sibling})
        assert f'href="{url}"' in html
    assert [entry["id"] for entry in response.context["access_sections"] if entry["current"]] == [section]


def test_profile_lives_in_account_area():
    client, _, _ = logged_client("nav-profile")
    response = client.get(reverse("web:profile"))
    assert response.status_code == 200
    assert [template.name for template in response.templates][0] == "account/profile.html"
    html = response.content.decode()
    for name in ("web:profile", "account_email", "account_change_password", "mfa_index"):
        assert f'href="{reverse(name)}"' in html
    assert html.count('name="csrfmiddlewaretoken"') == 1
