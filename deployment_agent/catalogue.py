"""Signed release catalogues: suggestions, verified downloads and fetches (#317)."""

import hashlib
import json
import platform
from email.message import Message
from pathlib import Path
from typing import IO, TYPE_CHECKING
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from skeleton.system.dependencies import catalog_candidates
from skeleton.system.packages import PackageError, validate_catalog

from .storage import (
    AgentError,
    atomic_json,
    digest,
    load_json,
    now,
)
from .types import JSON

if TYPE_CHECKING:
    from .service import DeploymentService


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, request: Request, response: IO[bytes], code: int, message: str, headers: Message, newurl: str
    ) -> Request | None:
        raise AgentError("catalogue_redirect_rejected", 409)


def catalogue_cache(service: "DeploymentService", source_id: str) -> Path:
    """The verified copy of one catalogue source, named by the hash of its id."""
    return service.store.root / "catalogues" / f"{hashlib.sha256(source_id.encode()).hexdigest()}.json"


def catalogue_suggestions(
    service: "DeploymentService", dependency: str, required: str, target: JSON, *, limit: int = 3
) -> list[JSON]:
    """Compatible releases from already verified catalogue caches; never downloads anything."""
    suggestions = []
    for source in service.catalogue_sources():
        cache = catalogue_cache(service, source["id"])
        if not cache.exists():
            continue
        try:
            saved = load_json(cache)
            body = validate_catalog(
                saved["envelope"],
                service.config["approved_publishers"],
                minimum_generation=saved["generation"],
                channels=source.get("channels", ["stable"]),
            )
        except (PackageError, KeyError, OSError, ValueError):
            continue
        for entry in catalog_candidates(body["entries"], dependency, required, target, limit=limit):
            suggestions.append(
                {
                    "source_id": source["id"],
                    "component_id": entry["component_id"],
                    "version": entry["version"],
                    "digest": entry["digest"],
                    # None marks a v1 catalogue: ranges unknown until the artifact is verified.
                    "dependencies": entry.get("dependencies"),
                }
            )
    suggestions.sort(key=lambda item: Version(item["version"]), reverse=True)
    return suggestions[:limit]


def catalogue_sources(service: "DeploymentService") -> list[JSON]:
    sources = service.config.get("catalogue_sources", [])
    if not isinstance(sources, list) or any(
        not isinstance(source, dict) or not isinstance(source.get("id"), str) for source in sources
    ):
        raise AgentError("invalid_catalogue_sources")
    return sources


def fetch_bytes(
    service: "DeploymentService",
    location: str,
    maximum: int,
    *,
    origin: tuple[str, str | None, int | None] | None = None,
) -> bytes:
    parts = urlsplit(location)
    if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
        raise AgentError("unapproved_catalogue_url")
    if origin is not None and (parts.scheme, parts.hostname, parts.port) != origin:
        raise AgentError("catalogue_artifact_off_origin")
    try:
        with build_opener(NoRedirect()).open(
            Request(location, headers={"Accept": "application/json, application/octet-stream"}),  # noqa: S310 - fetch_bytes accepts https only, checked above
            timeout=service.config.get("catalogue_timeout", 20),
        ) as response:
            content = response.read(maximum + 1)
            if len(content) > maximum:
                raise AgentError("catalogue_download_too_large", 413)
            return content
    except AgentError:
        raise
    except Exception:  # noqa: BLE001 - any transport failure is catalogue_unavailable
        raise AgentError("catalogue_unavailable", 409) from None


def check_updates(service: "DeploymentService") -> JSON:
    result: JSON = {"checked_at": now(), "sources": [], "releases": []}
    inventory = service.state()["inventory"]
    installed = {
        entry["manifest"]["app_id"]: entry["manifest"]["version"]
        for entry in inventory["apps"]
        if entry["state"] != "schema_retained"
    }
    installed["django-skeleton"] = inventory["core"]["version"]
    for source in service.catalogue_sources():
        entry = {"id": source["id"], "state": "available", "error_code": None}
        try:
            cache = catalogue_cache(service, source["id"])
            previous = load_json(cache) if cache.exists() else {"generation": 0}
            if source.get("url"):
                content = service.fetch_bytes(source["url"], 4 * 1024 * 1024)
            elif source.get("path") and Path(source["path"]).is_absolute():
                if Path(source["path"]).stat().st_size > 4 * 1024 * 1024:
                    raise AgentError("catalogue_download_too_large")
                content = Path(source["path"]).read_bytes()
            else:
                raise AgentError("invalid_catalogue_source")
            envelope = json.loads(content)
            body = validate_catalog(
                envelope,
                service.config["approved_publishers"],
                minimum_generation=previous["generation"],
                channels=source.get("channels", ["stable"]),
            )
            if (
                previous.get("generation") == body["generation"]
                and previous.get("digest")
                and previous["digest"] != digest(envelope)
            ):
                raise AgentError("catalogue_generation_content_changed")
            atomic_json(
                cache,
                {
                    "generation": body["generation"],
                    "digest": digest(envelope),
                    "envelope": envelope,
                    "checked_at": result["checked_at"],
                },
            )
            # Prefer the target runtime recorded by the active release over the agent's own.
            runtime = inventory.get("runtime") or {}
            python_version = runtime.get("python") or platform.python_version()
            django_version = runtime.get("django") or service.config.get("django_version", "6.1.3")
            for release in body["entries"]:
                compatible = all(
                    Version(value) in SpecifierSet(release["compatibility"][key])
                    for key, value in (
                        ("skeleton", inventory["core"]["version"]),
                        ("python", python_version),
                        ("django", django_version),
                    )
                )
                current = installed.get(release["component_id"])
                state = (
                    "incompatible"
                    if not compatible
                    else "current"
                    if current and Version(release["version"]) <= Version(current)
                    else "available"
                )
                result["releases"].append(
                    {
                        "source_id": source["id"],
                        "component_id": release["component_id"],
                        "version": release["version"],
                        "state": state,
                        "notes": release["notes"],
                        "channel": release["channel"],
                        "digest": release["digest"],
                        "compatibility": release["compatibility"],
                    }
                )
        except Exception as error:  # noqa: BLE001 - one unverifiable source never hides the others
            entry.update(
                state="unavailable",
                error_code=error.code if isinstance(error, AgentError) else "catalogue_verification_failed",
            )
        result["sources"].append(entry)
    atomic_json(service.store.root / "catalogue-status.json", result)
    return result


def fetch_release(service: "DeploymentService", component_id: str, version: str, source_id: str) -> JSON:
    sources = [source for source in service.catalogue_sources() if source["id"] == source_id]
    if len(sources) != 1 or not isinstance(component_id, str) or not isinstance(version, str):
        raise AgentError("invalid_catalogue_selection")
    source = sources[0]
    cache = catalogue_cache(service, source_id)
    if not cache.exists():
        service.check_updates()
    saved = load_json(cache)
    try:
        body = validate_catalog(
            saved["envelope"],
            service.config["approved_publishers"],
            minimum_generation=saved["generation"],
            channels=source.get("channels", ["stable"]),
        )
    except PackageError:
        raise AgentError("catalogue_verification_failed", 409) from None
    entries = [
        entry for entry in body["entries"] if entry["component_id"] == component_id and entry["version"] == version
    ]
    if len(entries) != 1:
        raise AgentError("catalogue_release_not_unique", 409)
    entry = entries[0]
    if source.get("url"):
        parts = urlsplit(source["url"])
        content = service.fetch_bytes(
            urljoin(source["url"], entry["artifact"]),
            service.config.get("max_upload_bytes", 128 * 1024 * 1024),
            origin=(parts.scheme, parts.hostname, parts.port),
        )
    else:
        base = Path(source["path"]).parent.resolve()
        target = (base / entry["artifact"]).resolve()
        if (
            target == base
            or base not in target.parents
            or not target.is_file()
            or target.stat().st_size > service.config.get("max_upload_bytes", 128 * 1024 * 1024)
        ):
            raise AgentError("catalogue_artifact_path_rejected")
        content = target.read_bytes()
    if hashlib.sha256(content).hexdigest() != entry["digest"]:
        raise AgentError("catalogue_artifact_hash_conflict", 409)
    result = service.upload(content)
    manifest = result["manifest"]
    if (
        manifest.get("app_id", manifest.get("component_id")) != component_id
        or manifest["version"] != version
        or manifest["publisher"] != entry["publisher"]
    ):
        raise AgentError("catalogue_artifact_identity_conflict", 409)
    with ZipFile(service.store.path("uploads", result["digest"])) as archive:
        if archive.read("manifest.sig").decode("ascii") != entry["signature"]:
            raise AgentError("catalogue_artifact_signature_conflict", 409)
    return result
