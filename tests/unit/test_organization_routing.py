"""The organization route converter (#479): canonical URLs outside a site scope, clean URLs inside."""

import re
import uuid
from pathlib import Path

import pytest
from django.urls import URLPattern, URLResolver, get_resolver, resolve, reverse
from django.urls.exceptions import Resolver404

from skeleton.tenancy.routing import ORG, organization_prefix, site_organization_id, site_scope

U = uuid.UUID("11111111-2222-4333-8444-555555555555")
OTHER = uuid.UUID("99999999-8888-4777-8666-555555555555")
ROOT = Path(__file__).resolve().parents[2]
FILL = {"uuid": str(U), "str": "x", "slug": "x", "int": 1, "path": "x", "flowaction": "cancel"}
PARAMETER = re.compile(r"<(?:(?P<converter>[^>:]+):)?(?P<name>[^>]+)>")


def _walk(patterns, prefix="", namespace=""):
    for pattern in patterns:
        route = prefix + str(pattern.pattern)
        if isinstance(pattern, URLResolver):
            inner = namespace
            if pattern.namespace:
                inner = f"{namespace}{pattern.namespace}:"
            yield from _walk(pattern.url_patterns, route, inner)
        elif isinstance(pattern, URLPattern) and pattern.name:
            yield namespace + pattern.name, route


def organization_routes():
    routes = {}
    for name, route in _walk(get_resolver().url_patterns):
        if ORG not in route or name in routes:
            continue
        kwargs = {}
        for match in PARAMETER.finditer(route.replace(ORG, "")):
            converter = match["converter"] or "str"
            if converter not in FILL:
                break
            kwargs[match["name"]] = FILL[converter]
        else:
            routes[name] = kwargs
    return routes


def test_every_organization_route_is_canonical_outside_and_clean_inside_the_site_scope():
    routes = organization_routes()
    assert len(routes) > 60  # The core alone has ~70 organization web routes.
    for name, kwargs in routes.items():
        url = reverse(name, kwargs={"organization_id": U, **kwargs})
        assert url.startswith(f"/o/{U}/"), name
        assert resolve(url).view_name == name, name
        with site_scope(U):
            clean = reverse(name, kwargs={"organization_id": U, **kwargs})
            assert "/o/" not in clean, name
            assert clean == url[len(f"/o/{U}") :], name
            # The canonical form resolves in the scope; the host middleware (#482) rewrites the clean
            # form to it, because a few clean forms collide with global routes (/, /notifications/).
            assert resolve(url).view_name == name, name
            assert resolve(url).kwargs["organization_id"] == U, name


@pytest.mark.parametrize(
    ("name", "kwargs", "clean"),
    [
        ("web:dashboard", {}, "/"),
        ("web:records", {"table_key": "x"}, "/tables/x/"),
        ("notifications:organization", {}, "/notifications/"),
        ("flows:tasks", {}, "/flows/tasks/"),
        ("flows:instance-action", {"sysid": OTHER, "action": "retry"}, f"/flows/instances/{OTHER}/retry/"),
    ],
)
def test_explicit_routes(name, kwargs, clean):
    assert reverse(name, kwargs={"organization_id": U, **kwargs}) == f"/o/{U}" + clean
    with site_scope(U):
        assert reverse(name, kwargs={"organization_id": U, **kwargs}) == clean
        assert reverse(name, kwargs={"organization_id": OTHER, **kwargs}) == f"/o/{OTHER}" + clean


def test_mounted_application_routes_use_the_converter():
    mounted = [name for name, route in _walk(get_resolver().url_patterns) if f"{ORG}apps/" in route]
    applications = [route for _name, route in _walk(get_resolver().url_patterns) if "/apps/" in route]
    assert all("o/<uuid:organization_id>/apps/" not in route or "api/v1/" in route for route in applications)
    if mounted:  # The example app is installed in the suite; a bare core has no mounted app.
        name = mounted[0]
        kwargs = organization_routes().get(name)
        if kwargs is not None:
            with site_scope(U):
                assert reverse(name, kwargs={"organization_id": U, **kwargs}).startswith("/apps/")


def test_clean_forms_do_not_resolve_to_organization_routes_outside_a_site():
    assert resolve("/").view_name == "web:home"
    assert resolve("/notifications/").view_name == "notifications:global"
    assert resolve("/flows/tasks/").view_name == "flows:global-tasks"
    for path in ("/tables/x/", "/settings/", "/flows/instances/"):
        with pytest.raises(Resolver404):
            resolve(path)


def test_other_organization_and_malformed_uuid_inside_the_site_scope():
    with site_scope(U):
        assert resolve(f"/o/{U}/tables/x/").kwargs["organization_id"] == U
        with pytest.raises(Resolver404):
            resolve(f"/o/{OTHER}/tables/x/")
    lettered = "abcdef12-3456-4789-8abc-def012345678"
    for malformed in (f"/o/{str(U)[:-1]}/tables/x/", f"/o/{lettered.upper()}/tables/x/", "/o/not-a-uuid/tables/x/"):
        with pytest.raises(Resolver404):
            resolve(malformed)


def test_instance_action_accepts_exactly_the_api_actions():
    from skeleton.flows.api_views import INSTANCE_ACTIONS

    for action in INSTANCE_ACTIONS:
        assert resolve(f"/o/{U}/flows/instances/{OTHER}/{action}/").view_name == "flows:instance-action"
    for action in ("delete", "Cancel", "cancelx", ""):
        try:
            match = resolve(f"/o/{U}/flows/instances/{OTHER}/{action}/")
        except Resolver404:
            continue
        assert match.view_name != "flows:instance-action", action


def test_organization_prefix():
    assert organization_prefix(U) == f"/o/{U}/"
    with site_scope(str(U)):
        assert organization_prefix(U) == "/"
        assert organization_prefix(OTHER) == f"/o/{OTHER}/"


def test_the_site_scope_never_leaks():
    assert site_organization_id() is None
    with pytest.raises(RuntimeError):
        with site_scope(U):
            assert site_organization_id() == U
            raise RuntimeError("view failed")
    assert site_organization_id() is None
    assert reverse("web:dashboard", args=[U]) == f"/o/{U}/"


def test_api_routes_are_unchanged():
    assert reverse("api:records", kwargs={"organization_id": U, "table_key": "x"}).startswith(f"/api/v1/o/{U}/")
    with site_scope(U):
        assert reverse("api:records", kwargs={"organization_id": U, "table_key": "x"}).startswith(f"/api/v1/o/{U}/")


def test_no_web_route_hardcodes_the_organization_prefix():
    """Guard: web organization routes use ``ORG``; only API routes keep ``o/<uuid:organization_id>``."""
    offenders = []
    for path in (ROOT / "skeleton").rglob("*.py"):
        if path.parent.name == "api" or not (path.name.endswith("urls.py") or path.name == "app_routing.py"):
            continue
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "o/<uuid:organization_id>" in line and "api/v1/o/" not in line:
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert offenders == []
