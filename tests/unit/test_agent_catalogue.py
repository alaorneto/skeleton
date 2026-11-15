"""Catalogue downloads of the agent: HTTPS, same origin, no redirects, bounded size (#247).

No network: ``build_opener`` is replaced by a fake whose responses the test chooses.
"""

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import catalogue as catalogue_module  # noqa: E402
from deployment_agent.service import NoRedirect  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.packages import PackageError  # noqa: E402
from tests.unit.agent_helpers import bare_service, journal_operation  # noqa: E402

pytestmark = pytest.mark.posix
CATALOG_URL = "https://releases.example.test/catalog/index.json"


class FakeResponse:
    def __init__(self, content):
        self.content = content

    def read(self, size):
        return self.content[:size]

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture
def network(monkeypatch):
    """Requests the agent would send; ``responses`` maps URL to bytes or an exception."""
    state = SimpleNamespace(requests=[], responses={}, handlers=[])

    class Opener:
        def open(self, request, timeout):
            state.requests.append((request.full_url, timeout))
            answer = state.responses.get(request.full_url, OSError("unreachable"))
            if isinstance(answer, BaseException):
                raise answer
            return FakeResponse(answer)

    def build_opener(*handlers):
        state.handlers.extend(handlers)
        return Opener()

    monkeypatch.setattr(catalogue_module, "build_opener", build_opener)
    return state


@pytest.fixture
def service(tmp_path):
    return bare_service(tmp_path)


@pytest.mark.parametrize(
    "url",
    [
        "http://releases.example.test/index.json",
        "ftp://releases.example.test/index.json",
        "file:///etc/passwd",
        "https://user:secret@releases.example.test/index.json",
        "https://user@releases.example.test/index.json",
        "https://releases.example.test/index.json#fragment",
        "https:///index.json",
        "releases.example.test/index.json",
    ],
)
def test_fetch_refuses_http_credentials_and_fragment(service, network, url):
    with pytest.raises(AgentError, match="unapproved_catalogue_url"):
        service.fetch_bytes(url, 1024)
    assert network.requests == []


def test_fetch_uses_https_without_redirects(service, network):
    network.responses[CATALOG_URL] = b"{}"
    assert service.fetch_bytes(CATALOG_URL, 1024) == b"{}"
    assert network.requests == [(CATALOG_URL, 20)]
    assert len(network.handlers) == 1 and isinstance(network.handlers[0], NoRedirect)


@pytest.mark.parametrize(
    "url",
    [
        "https://cdn.example.test/notes.skapp",
        "https://releases.example.test:8443/notes.skapp",
    ],
)
def test_fetch_refuses_artifact_on_another_origin(service, network, url):
    origin = ("https", "releases.example.test", None)
    with pytest.raises(AgentError, match="catalogue_artifact_off_origin"):
        service.fetch_bytes(url, 1024, origin=origin)
    assert network.requests == []
    network.responses["https://releases.example.test/notes.skapp"] = b"ok"
    assert service.fetch_bytes("https://releases.example.test/notes.skapp", 1024, origin=origin) == b"ok"


def test_redirect_is_refused():
    with pytest.raises(AgentError) as raised:
        NoRedirect().redirect_request(None, None, 302, "Found", {}, "https://elsewhere.example.test/")
    assert (raised.value.code, raised.value.status) == ("catalogue_redirect_rejected", 409)


def test_redirect_raised_during_download_is_not_masked(service, network):
    network.responses[CATALOG_URL] = AgentError("catalogue_redirect_rejected", 409)
    with pytest.raises(AgentError, match="catalogue_redirect_rejected"):
        service.fetch_bytes(CATALOG_URL, 1024)


def test_download_over_limit_is_refused(service, network):
    network.responses[CATALOG_URL] = b"x" * 1025
    with pytest.raises(AgentError) as raised:
        service.fetch_bytes(CATALOG_URL, 1024)
    assert (raised.value.code, raised.value.status) == ("catalogue_download_too_large", 413)
    network.responses[CATALOG_URL] = b"x" * 1024
    assert len(service.fetch_bytes(CATALOG_URL, 1024)) == 1024


@pytest.mark.parametrize("error", [OSError("refused"), TimeoutError(), ValueError("bad"), RuntimeError("x")])
def test_network_failure_becomes_catalogue_unavailable(service, network, error):
    network.responses[CATALOG_URL] = error
    with pytest.raises(AgentError) as raised:
        service.fetch_bytes(CATALOG_URL, 1024)
    assert (raised.value.code, raised.value.status) == ("catalogue_unavailable", 409)


def body(*entries):
    return {"generation": 1, "entries": list(entries)}


def entry(**values):
    return {
        "component_id": "org.example.notes",
        "version": "1.0.0",
        "compatibility": {"skeleton": ">=0.1", "python": ">=3", "django": ">=5"},
        "notes": "",
        "channel": "stable",
        "digest": "d" * 64,
        "publisher": "org.example",
        "artifact": "notes-1.0.0.skapp",
        **values,
    }


@pytest.fixture
def catalogue(monkeypatch):
    """validate_catalog accepts envelopes marked ``signed`` and refuses the rest."""

    def validate_catalog(envelope, publishers, *, minimum_generation, channels):
        if not envelope.get("signed"):
            raise PackageError("unsigned catalogue")
        return envelope["body"]

    monkeypatch.setattr(catalogue_module, "validate_catalog", validate_catalog)


@pytest.mark.parametrize("source", [{"id": "s"}, {"id": "s", "path": "relative/index.json"}, {"id": "s", "path": ""}])
def test_catalogue_source_must_have_url_or_absolute_path(service, network, catalogue, source):
    service.config["catalogue_sources"] = [source]
    result = service.check_updates()
    assert result["sources"] == [{"id": "s", "state": "unavailable", "error_code": "invalid_catalogue_source"}]
    assert result["releases"] == [] and network.requests == []


def test_catalogue_sources_must_be_a_list_of_identified_objects(service):
    for value in ({"id": "s"}, [{"path": "/x"}], ["s"]):
        service.config["catalogue_sources"] = value
        with pytest.raises(AgentError, match="invalid_catalogue_sources"):
            service.catalogue_sources()


def write_catalogue(tmp_path, name, envelope):
    folder = tmp_path / name
    folder.mkdir()
    (folder / "index.json").write_text(json.dumps(envelope))
    return folder


def test_local_artifact_path_cannot_escape_catalogue_directory(service, tmp_path, catalogue):
    outside = tmp_path / "outside.skapp"
    outside.write_bytes(b"outside")
    digest = hashlib.sha256(b"outside").hexdigest()
    for artifact in ("../outside.skapp", str(outside), ".", "missing.skapp", "sub/../../outside.skapp"):
        envelope = {"signed": True, "body": body(entry(artifact=artifact, digest=digest))}
        folder = write_catalogue(tmp_path, f"c{abs(hash(artifact))}", envelope)
        service.config["catalogue_sources"] = [{"id": "local", "path": str(folder / "index.json")}]
        service.store.root.joinpath("catalogues").mkdir(exist_ok=True)
        for cached in service.store.root.joinpath("catalogues").glob("*.json"):
            cached.unlink()
        service.upload = lambda content: pytest.fail("An artifact outside the catalogue was read.")
        with pytest.raises(AgentError, match="catalogue_artifact_path_rejected"):
            service.fetch_release("org.example.notes", "1.0.0", "local")


def test_local_artifact_with_wrong_hash_is_refused(service, tmp_path, catalogue):
    envelope = {"signed": True, "body": body(entry(digest="0" * 64))}
    folder = write_catalogue(tmp_path, "catalogue", envelope)
    (folder / "notes-1.0.0.skapp").write_bytes(b"package")
    service.config["catalogue_sources"] = [{"id": "local", "path": str(folder / "index.json")}]
    service.upload = lambda content: pytest.fail("A package with the wrong hash was uploaded.")
    with pytest.raises(AgentError, match="catalogue_artifact_hash_conflict"):
        service.fetch_release("org.example.notes", "1.0.0", "local")


def test_unverifiable_catalogue_marks_source_unavailable_and_keeps_other_sources(service, tmp_path, catalogue, network):
    good = write_catalogue(tmp_path, "good", {"signed": True, "body": body(entry())})
    bad = write_catalogue(tmp_path, "bad", {"signed": False, "body": body(entry(component_id="org.evil.app"))})
    network.responses[CATALOG_URL] = b"not json"
    service.config["catalogue_sources"] = [
        {"id": "bad", "path": str(bad / "index.json")},
        {"id": "remote", "url": CATALOG_URL},
        {"id": "good", "path": str(good / "index.json")},
    ]
    result = service.check_updates()
    assert result["sources"] == [
        {"id": "bad", "state": "unavailable", "error_code": "catalogue_verification_failed"},
        {"id": "remote", "state": "unavailable", "error_code": "catalogue_verification_failed"},
        {"id": "good", "state": "available", "error_code": None},
    ]
    assert [(row["source_id"], row["component_id"], row["state"]) for row in result["releases"]] == [
        ("good", "org.example.notes", "available")
    ]


def core_entry(version, *, from_versions=None, django=">=6.1,<6.2", **values):
    return entry(
        component_id="django-skeleton",
        version=version,
        compatibility={"skeleton": ">=0.1", "python": ">=3", "django": django},
        migrations={"leaves": [], "from_versions": from_versions or ">=0.5.0"},
        artifact=f"skeleton-release-{version}.skapp",
        **values,
    )


@pytest.fixture
def expiring(monkeypatch):
    """Like ``catalogue``, plus envelopes that verify only while ``state.now_valid`` (an expiry)."""
    state = SimpleNamespace(now_valid=True)

    def validate_catalog(envelope, publishers, *, minimum_generation, channels):
        if not envelope.get("signed") or (envelope.get("expires_soon") and not state.now_valid):
            raise PackageError("Release catalog is expired.")
        return envelope["body"]

    monkeypatch.setattr(catalogue_module, "validate_catalog", validate_catalog)
    return state


def core_path(installed="0.6.0", latest=None, steps=(), blocked=None):
    return {
        "component_id": "django-skeleton",
        "kind": "core",
        "installed": installed,
        "latest": latest,
        "steps": list(steps),
        "blocked": blocked,
    }


def test_refresh_then_evaluate_and_check_updates_adds_paths(service, tmp_path, expiring, network):
    releases = body(core_entry("0.6.3"), core_entry("0.7.2", from_versions=">=0.6.3"))
    folder = write_catalogue(tmp_path, "core", {"signed": True, "body": releases})
    service.config["catalogue_sources"] = [{"id": "official", "path": str(folder / "index.json")}]
    refreshed = catalogue_module.refresh_sources(service)
    assert set(refreshed) == {"checked_at", "sources"}
    assert refreshed["sources"] == [{"id": "official", "state": "available", "error_code": None}]
    result = service.check_updates()
    assert set(result) == {"checked_at", "sources", "releases", "paths"}
    assert [(row["version"], row["state"]) for row in result["releases"]] == [
        ("0.6.3", "available"),
        ("0.7.2", "available"),
    ]
    steps = [{"version": "0.6.3", "source_id": "official"}, {"version": "0.7.2", "source_id": "official"}]
    assert result["paths"] == [core_path(latest="0.7.2", steps=steps)]
    assert catalogue_module.evaluate(service) == {"releases": result["releases"], "paths": result["paths"]}
    assert network.requests == []


def test_update_status_never_reaches_the_network_and_follows_the_inventory(service, expiring, network):
    assert service.update_status() == {"checked_at": None, "sources": [], "releases": [], "paths": [core_path()]}
    network.responses[CATALOG_URL] = json.dumps({"signed": True, "body": body(core_entry("0.6.3"))}).encode()
    service.config["catalogue_sources"] = [{"id": "remote", "url": CATALOG_URL}]
    checked = service.check_updates()
    assert len(network.requests) == 1
    status = service.update_status()
    assert status["checked_at"] == checked["checked_at"] and status["sources"] == checked["sources"]
    assert status["paths"][0]["steps"] == [{"version": "0.6.3", "source_id": "remote"}]
    state = service.state()
    state["inventory"]["core"]["version"] = "0.6.3"
    catalogue_module.atomic_json(service.store.root / "active.json", state)
    assert service.update_status()["paths"] == [core_path(installed="0.6.3")]
    assert len(network.requests) == 1


def test_update_status_without_deployment_is_empty(tmp_path, expiring):
    service = bare_service(tmp_path)
    (service.store.root / "active.json").unlink()
    assert service.update_status() == {"checked_at": None, "sources": [], "releases": [], "paths": []}


def test_core_entry_ignores_the_installed_django_but_app_entry_does_not(service, tmp_path, expiring):
    newer_django = {"skeleton": ">=0.1", "python": ">=3", "django": ">=7.0,<7.1"}
    envelope = {
        "signed": True,
        "body": body(
            core_entry("0.7.0", django=">=7.0,<7.1", from_versions=">=0.6.0"), entry(compatibility=newer_django)
        ),
    }
    folder = write_catalogue(tmp_path, "mixed", envelope)
    service.config["catalogue_sources"] = [{"id": "local", "path": str(folder / "index.json")}]
    result = service.check_updates()
    assert [(row["component_id"], row["state"]) for row in result["releases"]] == [
        ("django-skeleton", "available"),
        ("org.example.notes", "incompatible"),
    ]


def test_expired_cache_offers_nothing_and_keeps_other_sources(service, tmp_path, expiring):
    soon = write_catalogue(tmp_path, "soon", {"signed": True, "expires_soon": True, "body": body(core_entry("0.6.3"))})
    other = write_catalogue(tmp_path, "other", {"signed": True, "body": body(core_entry("0.6.5"))})
    service.config["catalogue_sources"] = [
        {"id": "soon", "path": str(soon / "index.json")},
        {"id": "other", "path": str(other / "index.json")},
    ]
    service.check_updates()
    expiring.now_valid = False
    status = service.update_status()
    assert [row["source_id"] for row in status["releases"]] == ["other"]
    assert status["paths"][0]["steps"] == [{"version": "0.6.5", "source_id": "other"}]


def test_tampered_cache_is_verified_again(service, tmp_path, expiring):
    folder = write_catalogue(tmp_path, "c", {"signed": True, "body": body(core_entry("0.6.3"))})
    service.config["catalogue_sources"] = [{"id": "local", "path": str(folder / "index.json")}]
    service.check_updates()
    cache = catalogue_module.catalogue_cache(service, "local")
    saved = json.loads(cache.read_text())
    saved["envelope"]["signed"] = False
    cache.write_text(json.dumps(saved))
    assert service.update_status()["releases"] == []


def test_malformed_from_versions_is_listed_but_never_a_step(service, tmp_path, expiring):
    bad = core_entry("0.6.3")
    bad["migrations"]["from_versions"] = "not a range"
    folder = write_catalogue(tmp_path, "c", {"signed": True, "body": body(bad, core_entry("0.6.2"))})
    service.config["catalogue_sources"] = [{"id": "local", "path": str(folder / "index.json")}]
    result = service.check_updates()
    assert [row["version"] for row in result["releases"]] == ["0.6.3", "0.6.2"]
    assert result["paths"][0]["steps"] == [{"version": "0.6.2", "source_id": "local"}]


def test_app_paths_cover_installed_managed_apps(tmp_path, expiring):
    inventory = {
        "generation": 1,
        "core": {"version": "0.6.0"},
        "apps": [
            {"manifest": {"app_id": "org.example.notes", "version": "1.0.0"}, "state": "installed"},
            {"manifest": {"app_id": "org.example.local", "version": "1.0.0"}, "state": "project-local"},
        ],
    }
    service = bare_service(tmp_path, inventory=inventory)
    newer = entry(version="1.4.0", migrations={"leaves": [], "from_versions": ">=1.0.0"})
    folder = write_catalogue(tmp_path, "apps", {"signed": True, "body": body(newer)})
    service.config["catalogue_sources"] = [{"id": "apps", "path": str(folder / "index.json")}]
    paths = service.check_updates()["paths"]
    assert [(row["component_id"], row["kind"]) for row in paths] == [
        ("django-skeleton", "core"),
        ("org.example.notes", "app"),
    ]
    assert paths[1]["steps"] == [{"version": "1.4.0", "source_id": "apps"}]


def test_same_version_from_two_sources_keeps_the_first(service, tmp_path, expiring):
    first = write_catalogue(tmp_path, "first", {"signed": True, "body": body(core_entry("0.6.3"))})
    second = write_catalogue(tmp_path, "second", {"signed": True, "body": body(core_entry("0.6.3"))})
    service.config["catalogue_sources"] = [
        {"id": "first", "path": str(first / "index.json")},
        {"id": "second", "path": str(second / "index.json")},
    ]
    assert service.check_updates()["paths"][0]["steps"] == [{"version": "0.6.3", "source_id": "first"}]


def test_scheduled_check_skips_without_touching_the_network(service, tmp_path, network):
    assert catalogue_module.scheduled_check(service) == "skipped"  # No sources.
    service.config["catalogue_sources"] = [{"id": "remote", "url": CATALOG_URL}]
    Path(service.config["maintenance_marker"]).write_text("{}")
    assert catalogue_module.scheduled_check(service) == "skipped"
    Path(service.config["maintenance_marker"]).unlink()
    journal_operation(service, tmp_path, state="running")
    assert catalogue_module.scheduled_check(service) == "skipped"
    (tmp_path / "fresh").mkdir()
    fresh = bare_service(tmp_path / "fresh")
    fresh.config["catalogue_sources"] = [{"id": "remote", "url": CATALOG_URL}]
    (fresh.store.root / "active.json").unlink()
    assert catalogue_module.scheduled_check(fresh) == "skipped"
    assert network.requests == []


def test_scheduled_check_refreshes_and_never_raises(service, expiring, network, monkeypatch):
    network.responses[CATALOG_URL] = json.dumps({"signed": True, "body": body(core_entry("0.6.3"))}).encode()
    service.config["catalogue_sources"] = [{"id": "remote", "url": CATALOG_URL}]
    assert catalogue_module.scheduled_check(service) == "checked"
    assert service.update_status()["checked_at"] is not None
    assert service.update_status()["paths"][0]["steps"] == [{"version": "0.6.3", "source_id": "remote"}]

    def broken(service):
        raise RuntimeError("boom")

    monkeypatch.setattr(catalogue_module, "refresh_sources", broken)
    assert catalogue_module.scheduled_check(service) == "failed"
