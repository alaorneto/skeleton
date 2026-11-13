"""skeleton.W035: notification links need SKELETON_PUBLIC_URL to be an origin (#279)."""

import pytest
from django.core.checks import run_checks
from django.test import override_settings

from skeleton.notifications.checks import public_url_checks


@pytest.mark.parametrize(
    ("value", "warns"),
    [
        ("", True),
        ("app.example.com", True),
        ("ftp://x", True),
        ("https://app.example.com/caminho", True),
        ("https://app.example.com", False),
        ("https://app.example.com/", False),
    ],
)
def test_public_url_check_warns_when_empty_or_not_an_origin(value, warns):
    with override_settings(SKELETON_PUBLIC_URL=value):
        assert [message.id for message in public_url_checks()] == (["skeleton.W035"] if warns else [])


def test_public_url_check_is_a_deploy_check():
    with override_settings(SKELETON_PUBLIC_URL=""):
        assert "skeleton.W035" not in {message.id for message in run_checks(include_deployment_checks=False)}
        assert "skeleton.W035" in {message.id for message in run_checks(include_deployment_checks=True)}
