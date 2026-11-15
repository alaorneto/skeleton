"""Release sources registered from the platform page (#499).

The page chooses only *where to look*: a GitHub ``owner/name`` on the API host fixed in the agent
file, and one of the publishers the operator approved there. *Whom to trust* stays in the agent
file (``approved_publishers``); keys, tokens, URLs and paths are never registered here. The list
lives in ``sources.json`` in the agent's private storage, written atomically under the deployment
lock, with a bounded history of who changed it and when.
"""

import json
import re
from typing import TYPE_CHECKING, Any

from .github import REPOSITORY
from .storage import AgentError, atomic_json, deployment_lock, identifier, now
from .types import JSON

if TYPE_CHECKING:
    from .service import DeploymentService

SOURCE_ID = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
CHANNEL = re.compile(r"[a-z][a-z0-9-]{0,31}\Z")
MAX_MANAGED_SOURCES = 16
MAX_HISTORY = 100
FIELDS = ("id", "github", "publisher", "channels", "private")
MAX_FILE_BYTES = 1024 * 1024


def _path(service: "DeploymentService") -> Any:
    return service.store.root / "sources.json"


def _config_sources(service: "DeploymentService") -> list[JSON]:
    sources = service.config.get("catalogue_sources", [])
    return sources if isinstance(sources, list) else []


def _config_ids(service: "DeploymentService") -> set[str]:
    return {str(source.get("id")) for source in _config_sources(service) if isinstance(source, dict)}


def _validated(service: "DeploymentService", payload: Any, *, error: str | None = None) -> JSON:
    """One registered source, in the order of the rules; ``error`` replaces every code (file reads)."""

    def fail(code: str, status: int = 400) -> AgentError:
        return AgentError(error or code, 409 if error else status)

    if not isinstance(payload, dict) or set(payload) - set(FIELDS) or not {"id", "github", "publisher"} <= set(payload):
        raise fail("invalid_source")
    if not isinstance(payload["id"], str) or not SOURCE_ID.fullmatch(payload["id"]):
        raise fail("invalid_source")
    if payload["id"] in _config_ids(service):
        raise fail("source_id_reserved", 409)
    if not isinstance(payload["github"], str) or not REPOSITORY.fullmatch(payload["github"]):
        raise fail("invalid_source")
    if not isinstance(payload["publisher"], str) or payload["publisher"] not in (
        service.config.get("approved_publishers") or {}
    ):
        raise fail("publisher_not_approved", 409)
    channels = payload.get("channels", ["stable"])
    if (
        not isinstance(channels, list)
        or not 1 <= len(channels) <= 4
        or len(set(channels)) != len(channels)
        or not all(isinstance(channel, str) and CHANNEL.fullmatch(channel) for channel in channels)
    ):
        raise fail("invalid_source")
    private = payload.get("private", False)
    if not isinstance(private, bool):
        raise fail("invalid_source")
    if private and not (service.config.get("github") or {}).get("token_file"):
        raise fail("github_token_missing", 409)
    return {
        "id": payload["id"],
        "github": payload["github"],
        "publisher": payload["publisher"],
        "channels": list(channels),
        "private": private,
    }


def _document(service: "DeploymentService") -> JSON:
    path = _path(service)
    if not path.exists():
        return {"version": 1, "sources": [], "history": []}
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            raise ValueError
        document = json.loads(path.read_bytes())
    except (OSError, ValueError):
        raise AgentError("invalid_managed_sources", 409) from None
    if (
        not isinstance(document, dict)
        or set(document) != {"version", "sources", "history"}
        or document["version"] != 1
        or not isinstance(document["sources"], list)
        or not isinstance(document["history"], list)
        or len(document["sources"]) > MAX_MANAGED_SOURCES
    ):
        raise AgentError("invalid_managed_sources", 409)
    return document


def managed_sources(service: "DeploymentService") -> list[JSON]:
    """The registered sources, each validated again; any problem fails closed."""
    sources = [_validated(service, source, error="invalid_managed_sources") for source in _document(service)["sources"]]
    if len({source["id"] for source in sources}) != len(sources):
        raise AgentError("invalid_managed_sources", 409)
    return sources


def listing(service: "DeploymentService") -> JSON:
    """Sources for the page: identities only, never keys, tokens, paths or URLs."""
    rows = []
    for source in _config_sources(service):
        kind = "github" if source.get("github") else "url" if source.get("url") else "path"
        rows.append(
            {
                "id": source.get("id"),
                "type": kind,
                "github": source.get("github") if kind == "github" else None,
                "publisher": source.get("publisher") if kind == "github" else None,
                "channels": list(source.get("channels", ["stable"])),
                "private": bool(source.get("private")) if kind == "github" else False,
                "origin": "config",
            }
        )
    for source in managed_sources(service):
        rows.append({**source, "type": "github", "origin": "managed"})
    return {
        "sources": rows,
        "publishers": sorted(service.config.get("approved_publishers") or {}),
        "private_available": bool((service.config.get("github") or {}).get("token_file")),
        "limit": MAX_MANAGED_SOURCES,
    }


def _write(service: "DeploymentService", sources: list[JSON], history: list[JSON], entry: JSON) -> None:
    history = [*history, entry][-MAX_HISTORY:]
    atomic_json(_path(service), {"version": 1, "sources": sources, "history": history}, 0o600)


def _actor(actor_id: Any) -> str | None:
    return identifier(actor_id) if actor_id is not None else None


def set_source(service: "DeploymentService", payload: Any, *, actor_id: Any) -> JSON:
    actor = _actor(actor_id)
    with deployment_lock(service.store.root):
        source = _validated(service, payload)
        document = _document(service)
        sources = managed_sources(service)
        if source["id"] not in {item["id"] for item in sources} and len(sources) >= MAX_MANAGED_SOURCES:
            raise AgentError("too_many_sources", 409)
        replaced = [source if item["id"] == source["id"] else item for item in sources]
        if source not in replaced:
            replaced.append(source)
        entry = {
            "at": now(),
            "actor_id": actor,
            "action": "set",
            **{key: source[key] for key in ("id", "github", "publisher")},
        }
        _write(service, replaced, document["history"], entry)
    return listing(service)


def remove_source(service: "DeploymentService", source_id: Any, *, actor_id: Any) -> JSON:
    """Remove a registered source; its index cache stays, so its generation floor survives."""
    actor = _actor(actor_id)
    if not isinstance(source_id, str) or not SOURCE_ID.fullmatch(source_id):
        raise AgentError("invalid_source")
    with deployment_lock(service.store.root):
        if source_id in _config_ids(service):
            raise AgentError("source_id_reserved", 409)
        document = _document(service)
        sources = managed_sources(service)
        removed = [item for item in sources if item["id"] == source_id]
        if not removed:
            raise AgentError("source_not_found", 404)
        entry = {
            "at": now(),
            "actor_id": actor,
            "action": "remove",
            **{key: removed[0][key] for key in ("id", "github", "publisher")},
        }
        _write(service, [item for item in sources if item["id"] != source_id], document["history"], entry)
    return listing(service)
