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
    # The sidebar is Início and the apps (#621): data and administration live in the settings area.
    menu = dashboard.content.decode().split('id="org-sidebar"', 1)[1].split("</nav>", 1)[0]
    assert "Início" in menu and "Tabelas" not in menu and "Auditoria" not in menu
    assert 'aria-label="Configuração da organização"' in menu  # The bootstrap identity administers it.
    assert "current_organization" not in client.get("/").context
    # The switcher never grants access: a foreign organization stays closed.
    assert client.get(f"/o/{foreign.pk}/").status_code == 404


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
    # One form in the page; the user menu has the colour scheme switch, a form of its own (#666).
    assert html[html.index("<main") : html.index("</main>")].count('name="csrfmiddlewaretoken"') == 1
    assert html.count(f'action="{reverse("web:color-scheme")}"') == 1


def test_access_lists_show_one_column_per_field_without_uuid_or_revision_columns():
    """Each field of a dedicated access resource has its own column; the UUID is a second line (#470)."""
    from skeleton.access.resources import dedicated_model
    from skeleton.web.forms import dedicated_label

    client, _, organization = logged_client("nav-columns")
    response = client.get(reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": "roles"}))
    assert response.status_code == 200
    model, fields = dedicated_model("roles")
    # The name comes first and the code goes on the second line (#767); no column for either id.
    shown = ["name", *(name for name in fields if name not in ("code", "name"))]
    assert response.context["headings"] == [dedicated_label(model, name) for name in shown]
    html = response.content.decode()
    header = html.split("<thead>", 1)[1].split("</thead>", 1)[0]
    assert header.count('<th scope="col"') == len(shown) + 1
    assert ">UUID<" not in header and ">Revisão<" not in header and ">Código<" not in header
    row = response.context["rows"].object_list[0]
    assert len(row["cells"]) == len(shown)
    assert f'<span class="cell-secondary"><code class="uuid">{row["secondary"]}</code></span>' in html
    assert 'class="btn btn-danger-outline btn-sm"' in html
    memberships = client.get(
        reverse("web:access-list", kwargs={"organization_id": organization.pk, "resource": "memberships"})
    )
    member = memberships.context["rows"].object_list[0]
    assert member["secondary"] == member["sysid"]  # Without a code, the UUID is the second line.
    assert 'class="badge badge-success badge-dot">Ativo<' in memberships.content.decode()


def test_forms_mark_required_and_optional_fields_once():
    """Every simple form shows * on required fields, "(opcional)" on the others and the legend once (#474)."""
    client, _, organization = logged_client("nav-marks")
    for path in (
        reverse("web:access-create", kwargs={"organization_id": organization.pk, "resource": "invitations"}),
        reverse("web:access-create", kwargs={"organization_id": organization.pk, "resource": "groups"}),
    ):
        html = client.get(path).content.decode()
        assert '<span class="required" aria-hidden="true"> *</span>' in html, path
        assert html.count("Campos com * são obrigatórios.") == 1, path
    login = Client().get("/accounts/login/").content.decode()
    assert "Campos com * são obrigatórios." not in login and "(opcional)" not in login


def test_audit_shows_the_actor_by_name_and_the_identifier_in_the_details():
    """One name lookup for the page (#475); the UUID only in the details panel (#834)."""
    client, actor, organization = logged_client("nav-audit")
    created = client.post(
        reverse("web:access-create", kwargs={"organization_id": organization.pk, "resource": "groups"}),
        {"code": "audit-group", "name": "Grupo auditado", "description": ""},
    )
    assert created.status_code == 302
    html = client.get(reverse("web:audit", kwargs={"organization_id": organization.pk})).content.decode()
    assert f'<span title="{actor.pk}">nav-audit</span>' in html
    assert actor.email not in html.split("<tbody>", 1)[1]  # The account menu shows it; the table never.
    # The full identifier lives in the row's details panel (#834).
    assert f'<dt>Ator</dt><dd><code class="uuid">{actor.pk}</code></dd>' in html


def test_person_filter_names_members_and_hides_the_rest():
    import uuid

    from skeleton.web.templatetags.skeleton_format import person

    known, gone = uuid.uuid4(), uuid.uuid4()
    people = {str(known): "Ana"}
    assert str(person(None, people)) == "Sistema"
    assert str(person(known, people)) == f'<span title="{known}">Ana</span>'
    unavailable = str(person(gone, people))
    assert "Pessoa indisponível" in unavailable and f">{str(gone)[:8]}</code>" in unavailable
    assert str(person(known, {str(known): "<b>x</b>"})).endswith(">&lt;b&gt;x&lt;/b&gt;</span>")


def test_page_titles_end_with_the_product_name(subtests):
    """A sample of screens: every tab title ends in " · Skeleton" (#476)."""
    import re

    client, _, organization = logged_client("nav-titles")
    org = {"organization_id": organization.pk}
    pages = [
        "/",
        f"/o/{organization.pk}/",
        reverse("web:profile"),
        reverse("web:audit", kwargs=org),
        reverse("web:access-list", kwargs={**org, "resource": "memberships"}),
        reverse("web:access-list", kwargs={**org, "resource": "roles"}),
        reverse("web:access-create", kwargs={**org, "resource": "groups"}),
        "/platform/",
        "/platform/users/",
        "/platform/jobs/",
    ]
    for path in pages:
        with subtests.test(path=path):
            response = client.get(path)
            assert response.status_code == 200
            title = re.search(r"<title>(.*?)</title>", response.content.decode(), re.S).group(1).strip()
            assert title.endswith("· Skeleton"), title


def test_jobs_filter_bar_offers_clear_only_with_a_filter():
    client, _, _ = logged_client("nav-jobs")
    plain = client.get("/platform/jobs/").content.decode()
    assert 'class="filter-bar"' in plain and 'class="stat-grid"' in plain and ">Limpar<" not in plain
    filtered = client.get("/platform/jobs/?state=dead").content.decode()
    assert ">Limpar<" in filtered
