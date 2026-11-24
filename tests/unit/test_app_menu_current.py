"""Exactly one app menu entry is current, on every page of the app (#842)."""

from skeleton.system.applications import mark_current_entry

ORG = "/o/7a0c1f7e-0000-4000-8000-000000000001/"


def entries():
    return [
        {"app_label": "books", "url": ORG + "apps/books/"},
        {"app_label": "books", "url": ORG + "apps/books/accounts/"},
        {"app_label": "people", "url": ORG + "apps/people/list/"},
    ]


def current(path):
    return [entry["url"].removeprefix(ORG) for entry in mark_current_entry(entries(), path) if entry["current"]]


def test_the_longest_address_wins_and_other_pages_mark_the_first_entry(subtests):
    cases = [
        (ORG + "apps/books/", ["apps/books/"]),
        (ORG + "apps/books/accounts/", ["apps/books/accounts/"]),
        (ORG + "apps/books/accounts/1/", ["apps/books/accounts/"]),
        (ORG + "apps/books/journal/9/", ["apps/books/"]),
        (ORG + "apps/people/42/", ["apps/people/list/"]),  # A page outside every entry, inside the app.
        (ORG + "apps/peoples/", []),
        (ORG + "settings/", []),
        ("/", []),
    ]
    for path, expected in cases:
        with subtests.test(path=path):
            assert current(path) == expected
