"""Names of the settings area (#621): every route name exists and record screens stay outside."""

from django.urls import get_resolver
from django.urls.resolvers import URLPattern, URLResolver

from skeleton.web.navigation import SETTINGS_GROUPS, SETTINGS_ROUTE_NAMES


def _names(patterns, namespace=""):
    for pattern in patterns:
        if isinstance(pattern, URLResolver):
            inner = f"{namespace}{pattern.namespace}:" if pattern.namespace else namespace
            yield from _names(pattern.url_patterns, inner)
        elif isinstance(pattern, URLPattern) and pattern.name:
            yield namespace + pattern.name


def test_every_settings_route_name_resolves():
    names = set(_names(get_resolver().url_patterns))
    assert SETTINGS_ROUTE_NAMES - names == set()
    for _, _, items in SETTINGS_GROUPS:
        for item in items:
            assert item.url_name in names and set(item.routes) <= names, item


def test_record_screens_and_the_home_keep_the_functional_menu():
    for name in ("web:dashboard", "web:records", "web:record", "web:record-edit", "web:search", "flows:tasks"):
        assert name not in SETTINGS_ROUTE_NAMES
