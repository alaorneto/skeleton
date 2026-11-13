"""Catalogue downloads of the agent: HTTPS, same origin, no redirects, bounded size (#247).

No network: ``build_opener`` is replaced by a fake whose responses the test chooses.
"""

import hashlib
import json
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import service as service_module  # noqa: E402
from deployment_agent.service import NoRedirect  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.packages import PackageError  # noqa: E402
from tests.unit.agent_helpers import bare_service  # noqa: E402

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

    monkeypatch.setattr(service_module, "build_opener", build_opener)
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

    monkeypatch.setattr(service_module, "validate_catalog", validate_catalog)


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
