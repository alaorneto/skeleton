"""The generic access route of the REST never captures another package's routes (#340)."""

from uuid import uuid4

import pytest
from django.urls import resolve

from skeleton.api.views import DedicatedView, OwnMembershipView

ORGANIZATION = uuid4()


@pytest.mark.parametrize("resource", ["webhooks", "service-accounts", "apps", "membership", "catalog", "audit"])
def test_generic_access_route_does_not_shadow_other_api_routes(resource):
    match = resolve(f"/api/v1/o/{ORGANIZATION}/{resource}/")
    assert getattr(match.func, "view_class", None) is not DedicatedView, resource


def test_generic_access_route_serves_the_access_resources():
    assert resolve(f"/api/v1/o/{ORGANIZATION}/roles/").func.view_class is DedicatedView


def test_own_membership_view_has_a_docstring():
    assert OwnMembershipView.__doc__.startswith("The actor's own membership in this organization")
