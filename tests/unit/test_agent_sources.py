"""Release sources registered from the platform page (#499): validated and kept by the agent."""

import json
import stat

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import sources  # noqa: E402
from deployment_agent.catalogue import catalogue_sources, source_cache  # noqa: E402
from deployment_agent.github import REPOSITORY  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.releases import GITHUB_REPOSITORY  # noqa: E402
from tests.unit.agent_helpers import bare_service  # noqa: E402

pytestmark = pytest.mark.posix
ACTOR = "11111111-2222-4333-8444-555555555555"
FORK = {"id": "fork", "github": "acme/skeleton", "publisher": "acme"}


@pytest.fixture
def service(tmp_path):
    service = bare_service(tmp_path)
    service.config.update(
        approved_publishers={"org.example": "/k.pem", "acme": "/acme.pem"},
        catalogue_sources=[
            {"id": "official", "url": "https://releases.internal.example/index.json"},
            {"id": "local", "path": "/srv/private/catalog/index.json"},
        ],
    )
    return service


def code_of(call):
    with pytest.raises(AgentError) as raised:
        call()
    return raised.value.code, raised.value.status


def test_set_source_writes_a_private_file_and_joins_the_catalogue(service):
    listing = sources.set_source(service, FORK, actor_id=ACTOR)
    path = service.store.root / "sources.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    managed = [row for row in listing["sources"] if row["origin"] == "managed"]
    assert managed == [
        {
            "id": "fork",
            "github": "acme/skeleton",
            "publisher": "acme",
            "channels": ["stable"],
            "private": False,
            "type": "github",
            "origin": "managed",
        }
    ]
    assert [source["id"] for source in catalogue_sources(service)] == ["official", "local", "fork"]
    assert catalogue_sources(service)[0] == {"id": "official", "url": "https://releases.internal.example/index.json"}


def test_listing_never_reveals_keys_paths_or_urls(service):
    sources.set_source(service, FORK, actor_id=ACTOR)
    listing = sources.listing(service)
    text = json.dumps(listing)
    assert "internal.example" not in text and "/srv/private" not in text and ".pem" not in text
    assert listing["publishers"] == ["acme", "org.example"] and listing["limit"] == 16
    assert listing["private_available"] is False
    assert [row["type"] for row in listing["sources"]] == ["url", "path", "github"]


@pytest.mark.parametrize(
    "payload,expected",
    [
        ({**FORK, "id": "Fork"}, ("invalid_source", 400)),
        ({**FORK, "id": "official"}, ("source_id_reserved", 409)),
        ({**FORK, "github": "https://github.com/acme/skeleton"}, ("invalid_source", 400)),
        ({**FORK, "github": "acme/skeleton/extra"}, ("invalid_source", 400)),
        ({**FORK, "github": "acme/../x"}, ("invalid_source", 400)),
        ({**FORK, "publisher": "evil"}, ("publisher_not_approved", 409)),
        ({**FORK, "private": True}, ("github_token_missing", 409)),
        ({**FORK, "private": "yes"}, ("invalid_source", 400)),
        ({**FORK, "channels": []}, ("invalid_source", 400)),
        ({**FORK, "channels": ["stable", "stable"]}, ("invalid_source", 400)),
        ({**FORK, "url": "https://evil.example"}, ("invalid_source", 400)),
    ],
)
def test_set_source_refusals(service, payload, expected):
    assert code_of(lambda: sources.set_source(service, payload, actor_id=ACTOR)) == expected
    assert not (service.store.root / "sources.json").exists()


def test_private_source_needs_the_token_file(service):
    service.config["github"] = {"token_file": "/srv/skeleton/private/github-token"}
    listing = sources.set_source(service, {**FORK, "private": True}, actor_id=ACTOR)
    assert listing["private_available"] is True and listing["sources"][-1]["private"] is True


def test_limit_replacement_and_removal(service):
    for number in range(sources.MAX_MANAGED_SOURCES):
        sources.set_source(service, {**FORK, "id": f"fork-{number}"}, actor_id=ACTOR)
    assert code_of(lambda: sources.set_source(service, {**FORK, "id": "one-more"}, actor_id=ACTOR)) == (
        "too_many_sources",
        409,
    )
    replaced = sources.set_source(service, {**FORK, "id": "fork-0", "github": "acme/other"}, actor_id=ACTOR)
    assert [row["github"] for row in replaced["sources"] if row["id"] == "fork-0"] == ["acme/other"]
    assert code_of(lambda: sources.remove_source(service, "official", actor_id=ACTOR)) == ("source_id_reserved", 409)
    assert code_of(lambda: sources.remove_source(service, "missing", actor_id=ACTOR)) == ("source_not_found", 404)
    after = sources.remove_source(service, "fork-1", actor_id=ACTOR)
    assert "fork-1" not in {row["id"] for row in after["sources"]}


def test_history_records_actor_and_date_and_stays_bounded(service, monkeypatch):
    monkeypatch.setattr(sources, "MAX_HISTORY", 3)
    for number in range(5):
        sources.set_source(service, {**FORK, "id": f"fork-{number}"}, actor_id=ACTOR)
    history = json.loads((service.store.root / "sources.json").read_text())["history"]
    assert len(history) == 3 and [row["id"] for row in history] == ["fork-2", "fork-3", "fork-4"]
    assert all(row["actor_id"] == ACTOR and row["at"] and row["action"] == "set" for row in history)
    sources.remove_source(service, "fork-4", actor_id=ACTOR)
    assert json.loads((service.store.root / "sources.json").read_text())["history"][-1]["action"] == "remove"


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        json.dumps({"version": 1, "sources": [{**FORK, "extra": 1}], "history": []}),
        json.dumps({"version": 1, "sources": [FORK, FORK], "history": []}),
        json.dumps({"version": 2, "sources": [], "history": []}),
        json.dumps({"version": 1, "sources": [{**FORK, "id": "official"}], "history": []}),
    ],
)
def test_invalid_file_fails_closed(service, content):
    (service.store.root / "sources.json").write_text(content)
    assert code_of(lambda: sources.managed_sources(service))[0] == "invalid_managed_sources"
    assert code_of(lambda: catalogue_sources(service))[0] == "invalid_managed_sources"


def test_publisher_removed_from_the_server_fails_closed(service):
    sources.set_source(service, FORK, actor_id=ACTOR)
    del service.config["approved_publishers"]["acme"]
    assert code_of(lambda: sources.managed_sources(service))[0] == "invalid_managed_sources"


def test_removing_and_registering_again_keeps_the_index_cache(service):
    sources.set_source(service, FORK, actor_id=ACTOR)
    cache = source_cache(service, {**FORK, "channels": ["stable"]})
    cache.parent.mkdir(exist_ok=True)
    cache.write_text(json.dumps({"generation": 7}))
    sources.remove_source(service, "fork", actor_id=ACTOR)
    assert cache.exists()
    sources.set_source(service, {**FORK, "id": "again"}, actor_id=ACTOR)
    assert source_cache(service, {**FORK, "id": "again"}) == cache


@pytest.mark.parametrize(
    "value",
    [
        "acme/skeleton",
        "a/b",
        "Acme-1/x.y_z",
        "acme/skeleton/extra",
        "-acme/x",
        "acme-/x",
        "acme/",
        "https://github.com/a/b",
        "a" * 40 + "/x",
        "acme/sk eleton",
    ],
)
def test_core_and_agent_repository_patterns_agree(value):
    assert bool(REPOSITORY.fullmatch(value)) == bool(GITHUB_REPOSITORY.fullmatch(value))
