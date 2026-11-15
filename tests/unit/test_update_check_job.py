"""The informative update check of installations without agent (#502). No network: a double."""

import json
from types import SimpleNamespace

import pytest

from skeleton.common import outbound_http
from skeleton.jobs import periodic, registry
from skeleton.system import jobs
from skeleton.system.releases import update_notice

TOKEN = "ghp_secret-update-token"


@pytest.fixture
def cache(settings):
    from django.core.cache import cache

    settings.CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "check"}}
    settings.SKELETON_AGENT_SOCKET = None
    settings.SKELETON_UPDATE_REPOSITORY = "acme/skeleton"
    settings.SKELETON_UPDATE_GITHUB_TOKEN_FILE = None
    settings.SKELETON_UPDATE_CHECK_HOURS = 24
    cache.clear()
    yield cache
    cache.clear()


@pytest.fixture
def github(monkeypatch):
    state = SimpleNamespace(calls=[], answer=None)

    def request(method, url, *, headers=None, max_response_bytes=None, **options):
        state.calls.append({"method": method, "url": url, "headers": dict(headers or {}), "limit": max_response_bytes})
        if isinstance(state.answer, Exception):
            raise state.answer
        return state.answer

    monkeypatch.setattr(outbound_http, "request", request)
    monkeypatch.setattr(jobs, "_installed_version", lambda: "0.6.0")
    return state


def response(body, status=200, truncated=False):
    data = body if isinstance(body, bytes) else json.dumps(body).encode()
    return outbound_http.OutboundResponse(status=status, headers={}, body=data, truncated=truncated)


def test_newer_release_becomes_an_informative_notice(cache, github):
    github.answer = response({"tag_name": "v0.7.1", "html_url": "https://evil.example/phish", "body": "<script>"})
    jobs.update_check()
    (call,) = github.calls
    assert call["method"] == "GET" and call["url"] == "https://api.github.com/repos/acme/skeleton/releases/latest"
    assert call["headers"] == {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "django-skeleton",
    }
    saved = cache.get(jobs.GITHUB_NOTICE_KEY)
    assert saved["newer"] is True and saved["url"] == "https://github.com/acme/skeleton/releases/tag/v0.7.1"
    notice = update_notice()
    assert notice["mode"] == "informative" and notice["core"] == "0.7.1" and "evil" not in notice["url"]


@pytest.mark.parametrize("tag", ["v0.6.0", "0.5.9"])
def test_same_or_older_release_is_no_notice(cache, github, tag):
    github.answer = response({"tag_name": tag})
    jobs.update_check()
    assert cache.get(jobs.GITHUB_NOTICE_KEY)["newer"] is False
    assert update_notice() is None


@pytest.mark.parametrize(
    "answer",
    [
        response({"message": "Not Found"}, 404),
        response({}, 403),
        response({}, 429),
        response(b"", 302),
        response(b"", 500),
        response(b"not json"),
        response({"name": "no tag"}),
        response({"tag_name": "v1.0 beta"}),
        response({"tag_name": "v1.0/../x"}),
        response({"tag_name": "<b>1.0</b>"}),
        response({"tag_name": 7}),
        response({"tag_name": "v9.9.9"}, truncated=True),
        outbound_http.OutboundRequestFailed("timeout"),
    ],
)
def test_failures_keep_the_previous_result(cache, github, answer):
    previous = {
        "latest": "0.6.5",
        "newer": True,
        "url": "https://github.com/acme/skeleton/releases/tag/v0.6.5",
        "checked_at": "x",
    }
    cache.set(jobs.GITHUB_NOTICE_KEY, previous)
    github.answer = answer
    jobs.update_check()
    assert cache.get(jobs.GITHUB_NOTICE_KEY) == previous


def test_token_goes_only_to_the_authorization_header(cache, github, settings, tmp_path, caplog):
    token = tmp_path / "token"
    token.write_text(TOKEN + "\n")
    settings.SKELETON_UPDATE_GITHUB_TOKEN_FILE = str(token)
    github.answer = response({"tag_name": "v0.7.1"})
    jobs.update_check()
    assert github.calls[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert TOKEN not in json.dumps(cache.get(jobs.GITHUB_NOTICE_KEY)) and TOKEN not in caplog.text
    settings.SKELETON_UPDATE_GITHUB_TOKEN_FILE = str(tmp_path / "missing")
    jobs.update_check()  # An unreadable token never raises and sends nothing.
    assert len(github.calls) == 1


@pytest.mark.parametrize(
    "changes",
    [
        {"SKELETON_UPDATE_REPOSITORY": ""},
        {"SKELETON_AGENT_SOCKET": "/run/skeleton/agent.sock"},
        {"SKELETON_UPDATE_REPOSITORY": "https://github.com/acme/skeleton"},
        {"SKELETON_UPDATE_REPOSITORY": "acme/skeleton/../x"},
    ],
)
def test_no_request_when_off_with_agent_or_invalid(cache, github, settings, changes):
    for name, value in changes.items():
        setattr(settings, name, value)
    jobs.update_check()
    assert github.calls == []


@pytest.fixture
def clean_registry(monkeypatch):
    monkeypatch.setattr(periodic, "_periodic", {})
    monkeypatch.setattr(registry, "_registry", dict(registry._registry))
    return periodic


@pytest.mark.parametrize(
    "repository,socket,scheduled",
    [("", None, False), ("acme/skeleton", "/run/agent.sock", False), ("acme/skeleton", None, True)],
)
def test_schedule_only_with_a_repository_and_without_agent(
    settings, clean_registry, monkeypatch, repository, socket, scheduled
):
    registered = []
    monkeypatch.setattr("skeleton.jobs.registry.register_job_type", lambda job_type: registered.append(job_type))
    settings.SKELETON_UPDATE_REPOSITORY, settings.SKELETON_AGENT_SOCKET = repository, socket
    settings.SKELETON_UPDATE_CHECK_HOURS = 0
    jobs.register_system_jobs()
    assert [job.code for job in registered] == [jobs.UPDATE_CHECK_JOB]
    assert registered[0].scope == "global" and registered[0].max_attempts == 1 and registered[0].run_as == "system"
    planned = periodic.all_periodic()
    assert bool(planned) is scheduled
    if scheduled:
        assert planned[0].per_organization is False and planned[0].every.total_seconds() == 3600


def test_instance_setting_accepts_empty_and_owner_name():
    from skeleton.instance.schema import _SPECS

    spec = next(spec for spec in _SPECS if spec.key == "update_repository")
    assert spec.validate("") == "" and spec.validate("acme/skeleton") == "acme/skeleton"
    for invalid in ("https://github.com/acme/skeleton", "acme", "acme/skeleton/x"):
        with pytest.raises(ValueError):
            spec.validate(invalid)
