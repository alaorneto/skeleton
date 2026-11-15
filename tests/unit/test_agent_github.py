"""GitHub release sources of the agent (#498). No network: the opener is a double.

The double records every Request (URL and headers) and answers from a table: bytes are a 200,
an HTTPError is raised (a 3xx carries the Location the agent then checks by hand).
"""

import json
from datetime import UTC, datetime, timedelta
from email.message import Message
from types import SimpleNamespace
from urllib.error import HTTPError

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import github as github_module  # noqa: E402
from deployment_agent.config import _validate_optional  # noqa: E402
from deployment_agent.github import GitHub  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.packages import build_catalog, release_catalog_entry, validate_release  # noqa: E402
from tests.unit.agent_helpers import bare_service, write_private  # noqa: E402
from tests.unit.release_helpers import PUBLISHER, build_test_release  # noqa: E402

pytestmark = pytest.mark.posix
API = "https://api.github.com"
REPO = "alaorneto/skeleton"
TOKEN = "ghp_secret-token-value-0123456789"
SIGNED = "https://objects.githubusercontent.com/github-production-release-asset/1?X-Amz-Signature=secret-signature"


class Response:
    def __init__(self, content):
        self.content = content

    def read(self, size):
        return self.content[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def http_error(url, code, **headers):
    message = Message()
    for key, value in headers.items():
        message[key.replace("_", "-")] = value
    return HTTPError(url, code, "error", message, None)


@pytest.fixture
def network(monkeypatch):
    state = SimpleNamespace(requests=[], responses={})

    class Opener:
        def open(self, request, timeout):
            state.requests.append(
                {
                    "url": request.full_url,
                    "authorization": request.get_header("Authorization"),
                    "headers": dict(request.header_items()),
                }
            )
            answer = state.responses.get(request.full_url, OSError("unreachable"))
            if isinstance(answer, BaseException):
                raise answer
            return Response(answer)

    monkeypatch.setattr(github_module, "build_opener", lambda *handlers: Opener())
    return state


def release_json(*assets, tag="v0.6.0"):
    return json.dumps({"tag_name": tag, "assets": list(assets), "body": "ignored"}).encode()


def asset(name, number, size=10, **extra):
    return {"name": name, "size": size, "url": f"{API}/repos/{REPO}/releases/assets/{number}", **extra}


def config(tmp_path, **github):
    return {"github": {"token_file": write_private(tmp_path / "github-token", TOKEN + "\n"), **github}}


def test_latest_release_keeps_only_the_needed_fields(tmp_path, network):
    evil = asset("skeleton-index.json", 1, browser_download_url="https://evil.example/index.json")
    weird = [asset("a/b", 2), {"name": "x", "size": "1", "url": "u"}, "nope", asset("ok.whl", 3)]
    network.responses[f"{API}/repos/{REPO}/releases/latest"] = release_json(evil, *weird)
    release = GitHub(config(tmp_path)).latest_release(REPO)
    assert release == {"tag": "v0.6.0", "assets": [asset("skeleton-index.json", 1), asset("ok.whl", 3)]}
    assert [request["url"] for request in network.requests] == [f"{API}/repos/{REPO}/releases/latest"]
    headers = network.requests[0]["headers"]
    assert headers["Accept"] == "application/vnd.github+json" and headers["X-github-api-version"] == "2022-11-28"
    assert headers["User-agent"].startswith("skeleton-deployment-agent/")
    assert network.requests[0]["authorization"] is None  # Public source: never the token.


def test_private_source_sends_the_token_only_to_the_api(tmp_path, network):
    network.responses[f"{API}/repos/{REPO}/releases/assets/7"] = http_error("x", 302, Location=SIGNED)
    network.responses[SIGNED] = b"index bytes"
    content = GitHub(config(tmp_path)).download_asset(REPO, asset("skeleton-index.json", 7), limit=100, private=True)
    assert content == b"index bytes"
    first, second = network.requests
    assert first["authorization"] == f"Bearer {TOKEN}" and first["headers"]["Accept"] == "application/octet-stream"
    assert second["url"] == SIGNED and second["authorization"] is None
    assert set(second["headers"]) == {"User-agent", "Accept"}


def test_private_source_without_token_is_refused(network):
    with pytest.raises(AgentError, match="github_token_missing"):
        GitHub({"github": {}}).latest_release(REPO, private=True)
    assert network.requests == []


@pytest.mark.parametrize(
    "location",
    [
        "http://objects.githubusercontent.com/x",
        "https://evil.example/x",
        "https://githubusercontent.com.evil.example/x",
        "https://user:pass@objects.githubusercontent.com/x",
        "https://objects.githubusercontent.com:8443/x",
        None,
    ],
)
def test_redirect_targets_outside_the_rule_are_refused(tmp_path, network, location):
    headers = {"Location": location} if location else {}
    network.responses[f"{API}/repos/{REPO}/releases/assets/7"] = http_error("x", 302, **headers)
    with pytest.raises(AgentError) as raised:
        GitHub(config(tmp_path)).download_asset(REPO, asset("a.skapp", 7), limit=100)
    assert raised.value.code == "github_asset_url_rejected"
    assert len(network.requests) == 1


def test_second_redirect_and_foreign_asset_urls_are_refused(tmp_path, network):
    network.responses[f"{API}/repos/{REPO}/releases/assets/7"] = http_error("x", 302, Location=SIGNED)
    network.responses[SIGNED] = http_error("x", 302, Location="https://objects.githubusercontent.com/again")
    client = GitHub(config(tmp_path))
    with pytest.raises(AgentError, match="github_asset_url_rejected"):
        client.download_asset(REPO, asset("a.skapp", 7), limit=100)
    for url in (
        "https://evil.example/repos/alaorneto/skeleton/releases/assets/7",
        f"{API}/repos/someone/else/releases/assets/7",
        f"{API}/repos/{REPO}/releases/assets/7/../8",
        f"{API}/repos/{REPO}/releases/assets/",
    ):
        with pytest.raises(AgentError, match="github_asset_url_rejected"):
            client.download_asset(REPO, {"name": "a", "size": 1, "url": url}, limit=100)


def test_sizes_are_bounded_before_and_while_reading(tmp_path, network):
    client = GitHub(config(tmp_path))
    with pytest.raises(AgentError, match="catalogue_download_too_large"):
        client.download_asset(REPO, asset("a.skapp", 7, size=101), limit=100)
    assert network.requests == []
    network.responses[f"{API}/repos/{REPO}/releases/assets/7"] = b"x" * 101
    with pytest.raises(AgentError, match="catalogue_download_too_large"):
        client.download_asset(REPO, asset("a.skapp", 7, size=1), limit=100)
    network.responses[f"{API}/repos/{REPO}/releases/latest"] = b" " * (github_module.MAX_JSON_BYTES + 1)
    with pytest.raises(AgentError, match="catalogue_download_too_large"):
        client.latest_release(REPO)


@pytest.mark.parametrize(
    "answer,code",
    [
        (http_error("x", 403, x_ratelimit_remaining="0"), "github_rate_limited"),
        (http_error("x", 429, retry_after="60"), "github_rate_limited"),
        (http_error("x", 404), "github_release_not_found"),
        (http_error("x", 401), "catalogue_unavailable"),
        (http_error("x", 502), "catalogue_unavailable"),
        (OSError("refused"), "catalogue_unavailable"),
        (b"not json", "catalogue_unavailable"),
        (json.dumps({"tag_name": "../x", "assets": []}).encode(), "catalogue_unavailable"),
        (json.dumps([1, 2]).encode(), "catalogue_unavailable"),
    ],
)
def test_api_failures_have_short_codes(tmp_path, network, answer, code):
    network.responses[f"{API}/repos/{REPO}/releases/latest"] = answer
    with pytest.raises(AgentError) as raised:
        GitHub(config(tmp_path)).latest_release(REPO, private=True)
    assert raised.value.code == code and TOKEN not in str(raised.value)


def test_invalid_repository_and_tag_are_refused_without_requests(tmp_path, network):
    client = GitHub(config(tmp_path))
    for repository in ("alaorneto", "-bad/repo", "a/b/c", "alaorneto/ske leton", "../x"):
        with pytest.raises(AgentError, match="invalid_catalogue_source"):
            client.latest_release(repository)
    for tag in ("..", "v0.6.0/x", "", "v 1"):
        with pytest.raises(AgentError, match="catalogue_artifact_path_rejected"):
            client.release_by_tag(REPO, tag)
    assert network.requests == []


@pytest.mark.parametrize(
    "values,code",
    [
        (
            {"catalogue_sources": [{"id": "s", "github": REPO, "url": "https://x.example/i.json", "publisher": "p"}]},
            "invalid_catalogue_sources",
        ),
        ({"catalogue_sources": [{"id": "s", "github": "not-a-repo", "publisher": "p"}]}, "invalid_catalogue_sources"),
        ({"catalogue_sources": [{"id": "s", "github": REPO}]}, "invalid_catalogue_sources"),
        ({"catalogue_sources": [{"id": "s", "github": REPO, "publisher": "unknown"}]}, "invalid_catalogue_sources"),
        (
            {"catalogue_sources": [{"id": "s", "github": REPO, "publisher": "p", "private": True}]},
            "invalid_catalogue_sources",
        ),
        (
            {"catalogue_sources": [{"id": "s", "github": REPO, "publisher": "p", "private": "yes"}]},
            "invalid_catalogue_sources",
        ),
        ({"catalogue_sources": [{"id": "s"}]}, "invalid_catalogue_sources"),
        ({"github": {"api_url": "http://api.github.com"}}, "invalid_github_settings"),
        ({"github": {"api_url": "https://api.github.com/?x=1"}}, "invalid_github_settings"),
        ({"github": {"download_hosts": []}}, "invalid_github_settings"),
        ({"github": {"token_file": "relative/token"}}, "invalid_github_settings"),
        ({"github": {"unknown": 1}}, "invalid_github_settings"),
    ],
)
def test_configuration_is_refused_at_load(values, code):
    with pytest.raises(AgentError) as raised:
        _validate_optional({"approved_publishers": {"p": "/k.pem"}, **values})
    assert raised.value.code == code


def test_valid_github_configuration_loads(tmp_path):
    _validate_optional(
        {
            "approved_publishers": {"p": "/k.pem"},
            "github": {"api_url": API, "download_hosts": [".githubusercontent.com"], "token_file": str(tmp_path / "t")},
            "catalogue_sources": [
                {"id": "s", "github": REPO, "publisher": "p", "private": True, "channels": ["stable"]}
            ],
        }
    )


# ----------------------------------------------------------------- the source end to end


@pytest.fixture
def published(tmp_path):
    """A real release package and its signed index, as #496 attaches them to a release."""
    (tmp_path / "publish").mkdir()
    built = build_test_release(tmp_path / "publish")
    verified = validate_release(built["release"], built["trust"])
    entry = release_catalog_entry(verified, artifact=f"v0.6.0/{built['release'].name}")
    index = build_catalog(
        [entry],
        tmp_path / "skeleton-index.json",
        built["private"],
        publisher=PUBLISHER,
        generation=3,
        expires_at=(datetime.now(UTC) + timedelta(days=30)).isoformat(),
    )
    return {
        **built,
        "index": index.read_bytes(),
        "package": built["release"].read_bytes(),
        "name": built["release"].name,
    }


def serve_release(network, published, *, tag="v0.6.0"):
    index_url, package_url = f"{API}/repos/{REPO}/releases/assets/1", f"{API}/repos/{REPO}/releases/assets/2"
    assets = [
        asset("skeleton-index.json", 1, size=len(published["index"]), browser_download_url="https://evil.example/i"),
        asset(published["name"], 2, size=len(published["package"])),
    ]
    network.responses[f"{API}/repos/{REPO}/releases/latest"] = release_json(*assets, tag=tag)
    network.responses[f"{API}/repos/{REPO}/releases/tags/{tag}"] = release_json(*assets, tag=tag)
    network.responses[index_url] = http_error("x", 302, Location=SIGNED + "-index")
    network.responses[SIGNED + "-index"] = published["index"]
    network.responses[package_url] = http_error("x", 302, Location=SIGNED + "-package")
    network.responses[SIGNED + "-package"] = published["package"]


@pytest.fixture
def agent(tmp_path, published):
    service = bare_service(tmp_path)
    service.config.update(
        approved_publishers={PUBLISHER: published["public"], "other": published["public"]},
        catalogue_sources=[{"id": "official", "github": REPO, "publisher": PUBLISHER, "private": True}],
        **config(tmp_path),
    )
    return service


def test_check_updates_reads_the_latest_release_index(agent, published, network):
    serve_release(network, published)
    result = agent.check_updates()
    assert result["sources"] == [{"id": "official", "state": "available", "error_code": None}]
    assert [(row["component_id"], row["version"]) for row in result["releases"]] == [("django-skeleton", "0.6.0")]
    api = [request for request in network.requests if request["url"].startswith(API)]
    assert len(api) == 2 and len(network.requests) == 3
    assert not any("evil.example" in request["url"] for request in network.requests)
    stored = "".join(path.read_text() for path in agent.store.root.rglob("*.json"))
    assert TOKEN not in stored and "secret-signature" not in stored


def test_fetch_release_downloads_by_tag_without_forwarding_the_token(agent, published, network):
    serve_release(network, published)
    agent.check_updates()
    network.requests.clear()
    result = agent.fetch_release("django-skeleton", "0.6.0", "official")
    assert (
        result["kind"] == "release"
        and result["digest"] == validate_release(published["release"], published["trust"]).digest
    )
    tagged, api_download, signed = network.requests
    assert tagged["url"].endswith("/releases/tags/v0.6.0") and tagged["authorization"] == f"Bearer {TOKEN}"
    assert api_download["authorization"] == f"Bearer {TOKEN}"
    assert signed["url"] == SIGNED + "-package" and signed["authorization"] is None


def test_index_of_another_publisher_marks_the_source_unavailable(agent, published, network):
    serve_release(network, published)
    agent.config["catalogue_sources"] = [{"id": "official", "github": REPO, "publisher": "other", "private": True}]
    result = agent.check_updates()
    assert result["sources"] == [
        {"id": "official", "state": "unavailable", "error_code": "catalogue_publisher_mismatch"}
    ]
    assert result["releases"] == []


def test_release_without_index_and_missing_asset(agent, published, network):
    network.responses[f"{API}/repos/{REPO}/releases/latest"] = release_json(asset("other.whl", 9))
    assert agent.check_updates()["sources"][0]["error_code"] == "source_index_missing"
    serve_release(network, published)
    agent.check_updates()
    network.responses[f"{API}/repos/{REPO}/releases/tags/v0.6.0"] = release_json(asset("skeleton-index.json", 1))
    with pytest.raises(AgentError, match="github_asset_missing"):
        agent.fetch_release("django-skeleton", "0.6.0", "official")


@pytest.mark.parametrize(
    "artifact", ["../x.skapp", "v0.6.0/a/b.skapp", "https://evil.example/x.skapp", "../../x", "x.skapp"]
)
def test_index_artifact_must_be_tag_and_asset(agent, published, network, artifact, monkeypatch):
    serve_release(network, published)
    agent.check_updates()
    from deployment_agent import catalogue

    original = catalogue.validate_catalog

    def with_artifact(*args, **kwargs):
        body = original(*args, **kwargs)
        return {**body, "entries": [{**entry, "artifact": artifact} for entry in body["entries"]]}

    monkeypatch.setattr(catalogue, "validate_catalog", with_artifact)
    network.requests.clear()
    with pytest.raises(AgentError, match="catalogue_artifact_path_rejected"):
        agent.fetch_release("django-skeleton", "0.6.0", "official")
    assert network.requests == []


def test_cache_follows_the_repository_not_the_source_id(agent, published, network):
    from deployment_agent.catalogue import source_cache

    first = {"id": "official", "github": REPO, "publisher": PUBLISHER}
    renamed = {"id": "renamed", "github": REPO.upper(), "publisher": PUBLISHER}
    fork = {"id": "official", "github": "someone/skeleton", "publisher": PUBLISHER}
    assert source_cache(agent, first) == source_cache(agent, renamed)
    assert source_cache(agent, first) != source_cache(agent, fork)
    assert source_cache(agent, {"id": "official", "url": "https://x.example/i.json"}) != source_cache(agent, first)


def test_public_source_never_sends_the_token(agent, published, network):
    serve_release(network, published)
    agent.config["catalogue_sources"] = [{"id": "official", "github": REPO, "publisher": PUBLISHER}]
    agent.check_updates()
    assert network.requests and all(request["authorization"] is None for request in network.requests)


def test_errors_never_carry_the_signed_url_or_token(agent, published, network):
    serve_release(network, published)
    network.responses[SIGNED + "-index"] = OSError(f"failed {SIGNED} {TOKEN}")
    result = agent.check_updates()
    assert result["sources"][0]["error_code"] == "catalogue_unavailable"
    status = (agent.store.root / "catalogue-status.json").read_text()
    assert "secret-signature" not in status and TOKEN not in status


def test_fetch_of_an_uploaded_artifact_skips_the_network(agent, published, network):
    import os

    serve_release(network, published)
    agent.check_updates()
    first = agent.fetch_release("django-skeleton", "0.6.0", "official")
    network.requests.clear()
    assert agent.fetch_release("django-skeleton", "0.6.0", "official") == first
    assert network.requests == []
    stored = agent.store.path("uploads", first["digest"])
    os.chmod(stored, 0o600)
    stored.write_bytes(stored.read_bytes() + b"tampered")
    with pytest.raises(AgentError) as raised:
        agent.fetch_release("django-skeleton", "0.6.0", "official")
    assert raised.value.code in ("catalogue_artifact_hash_conflict", "stored_artifact_corrupted")
