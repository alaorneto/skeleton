"""Rolling updates (#682): which plans may switch node by node, and the drain of the readiness."""

import json

import pytest
from django.test import RequestFactory

from deployment_agent.planning import rollout_eligibility


def inventory(version, apps=()):
    return {
        "core": {"version": version},
        "apps": [{"manifest": {"app_id": app}, "state": "installed"} for app in apps],
    }


@pytest.mark.parametrize(
    ("migrations", "before", "after", "expected"),
    [
        ([], inventory("0.4.0", ["a"]), inventory("0.4.1", ["a"]), ("rolling", None)),
        ([["app", "0002"]], inventory("0.4.0"), inventory("0.4.1"), ("window", "migrations_present")),
        ([], inventory("0.4.1"), inventory("0.5.0"), ("window", "minor_change")),
        ([], inventory("0.4.0", ["a"]), inventory("0.4.1", ["a", "b"]), ("window", "app_set_changed")),
    ],
)
def test_only_a_patch_without_migrations_and_with_the_same_apps_rolls(migrations, before, after, expected):
    assert rollout_eligibility({"migrations": migrations}, before, after) == expected


def test_the_readiness_answers_draining_while_the_marker_exists(tmp_path, settings, monkeypatch):
    from skeleton.api import health

    marker = tmp_path / "drain"
    settings.SKELETON_DRAIN_MARKER = str(marker)
    marker.write_text("{}")
    response = health.probe_response(RequestFactory().get("/health/ready/"))
    assert response.status_code == 503 and json.loads(response.content) == {"status": "draining"}
    live = health.probe_response(RequestFactory().get("/health/live/"))
    assert live.status_code == 200  # Only the readiness drains; the process is alive.
