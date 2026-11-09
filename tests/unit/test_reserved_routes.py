"""Apps cannot capture the core-owned platform/ root (nor the retired admin/ one)."""

import pytest
from django.core.exceptions import ValidationError
from django.http import HttpResponse
from django.urls import include, path, re_path

from skeleton.system.presentation import _validate_reserved_routes


def view(request):
    return HttpResponse()


@pytest.mark.parametrize(
    "patterns",
    [
        [path("platform/", view)],
        [path("platform/users/", view)],
        [path("admin/", view)],
        [path("<str:anything>/", view)],
        [path("", include([path("platform/", view)]))],
        [re_path(r"^plat", view)],
        [re_path(r"^(?:tasks|platform)/", view)],
        [re_path(r"tasks/", view)],
    ],
)
def test_reserved_roots_are_refused(patterns):
    with pytest.raises(ValidationError):
        _validate_reserved_routes(patterns)


@pytest.mark.parametrize(
    "patterns",
    [
        [path("tasks/", view)],
        [path("tasks/<uuid:sysid>/", view)],
        [path("platforms-report/", view)],
        [re_path(r"^tasks/(?P<sysid>[0-9a-f-]+)/$", view)],
    ],
)
def test_other_roots_are_accepted(patterns):
    _validate_reserved_routes(patterns)


def test_the_core_inbox_root_is_reserved():
    with pytest.raises(ValidationError):
        _validate_reserved_routes([path("notifications/", view)])
