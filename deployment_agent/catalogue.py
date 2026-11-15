"""Signed release catalogues: suggestions, verified downloads and fetches (#317)."""

import hashlib
import json
import platform
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener
from zipfile import ZipFile

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from skeleton.system.dependencies import catalog_candidates
from skeleton.system.packages import PackageError, validate_catalog

from .github import INDEX_ASSET, MAX_INDEX_BYTES, GitHub, NoRedirect, artifact_reference, find_asset
from .paths import component_path
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


def catalogue_cache(service: "DeploymentService", source_id: str) -> Path:
    """The verified copy of one catalogue source, named by the hash of its id."""
    return service.store.root / "catalogues" / f"{hashlib.sha256(source_id.encode()).hexdigest()}.json"


def source_cache(service: "DeploymentService", source: JSON) -> Path:
    """The cache of a source. A GitHub source is keyed by repository and publisher, not by id (#498):
    the generation floor follows the repository, and recreating the source never resets it."""
    if source.get("github"):
        key = f"github\n{str(source['github']).lower()}\n{source.get('publisher')}"
        return service.store.root / "catalogues" / f"{hashlib.sha256(key.encode()).hexdigest()}.json"
    return catalogue_cache(service, source["id"])


def catalogue_suggestions(
    service: "DeploymentService", dependency: str, required: str, target: JSON, *, limit: int = 3
) -> list[JSON]:
    """Compatible releases from already verified catalogue caches; never downloads anything."""
    suggestions = []
    for source in service.catalogue_sources():
        cache = source_cache(service, source)
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


CORE_COMPONENT = "django-skeleton"


def _source_content(service: "DeploymentService", source: JSON) -> bytes:
    """The raw index of one source: from a GitHub release, downloaded (``url``) or read (``path``)."""
    if source.get("github"):
        github, private = GitHub(service.config), bool(source.get("private"))
        release = github.latest_release(source["github"], private=private)
        asset = find_asset(release, INDEX_ASSET, missing="source_index_missing")
        return github.download_asset(source["github"], asset, limit=MAX_INDEX_BYTES, private=private)
    if source.get("url"):
        return service.fetch_bytes(source["url"], 4 * 1024 * 1024)
    if source.get("path") and Path(source["path"]).is_absolute():
        if Path(source["path"]).stat().st_size > 4 * 1024 * 1024:
            raise AgentError("catalogue_download_too_large")
        return Path(source["path"]).read_bytes()
    raise AgentError("invalid_catalogue_source")


def refresh_sources(service: "DeploymentService") -> JSON:
    """Download or read each source's index, verify it and keep the verified copy (#493).

    Writes and returns ``{checked_at, sources}``; one unverifiable source never hides the others.
    """
    result: JSON = {"checked_at": now(), "sources": []}
    for source in service.catalogue_sources():
        entry = {"id": source["id"], "state": "available", "error_code": None}
        try:
            cache = source_cache(service, source)
            previous = load_json(cache) if cache.exists() else {"generation": 0}
            envelope = json.loads(_source_content(service, source))
            body = validate_catalog(
                envelope,
                service.config["approved_publishers"],
                minimum_generation=previous["generation"],
                channels=source.get("channels", ["stable"]),
            )
            if source.get("publisher") and body["publisher"] != source["publisher"]:
                raise AgentError("catalogue_publisher_mismatch")  # Fixed per source, whatever else is approved.
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
        except Exception as error:  # noqa: BLE001 - one unverifiable source never hides the others
            entry.update(
                state="unavailable",
                error_code=error.code if isinstance(error, AgentError) else "catalogue_verification_failed",
            )
        result["sources"].append(entry)
    atomic_json(service.store.root / "catalogue-status.json", result)
    return result


def cached_entries(service: "DeploymentService", source: JSON) -> list[JSON] | None:
    """Entries of a source's cached index, verified again; None when absent or no longer valid."""
    cache = source_cache(service, source)
    if not cache.exists():
        return None
    try:
        saved = load_json(cache)
        body = validate_catalog(
            saved["envelope"],
            service.config["approved_publishers"],
            minimum_generation=saved["generation"],
            channels=source.get("channels", ["stable"]),
        )
    except (PackageError, AgentError, KeyError, OSError, ValueError, TypeError):
        return None  # A tampered or expired cache offers nothing.
    if source.get("publisher") and body.get("publisher") != source["publisher"]:
        return None
    entries = body["entries"]
    return entries if isinstance(entries, list) else None


def _compatible(release: JSON, core: str, python_version: str, django_version: str) -> bool:
    checks = [("skeleton", core), ("python", python_version)]
    # A core release brings its own Django: the preview checks it against the target runtime.
    if release["component_id"] != CORE_COMPONENT:
        checks.append(("django", django_version))
    try:
        return all(Version(value) in SpecifierSet(release["compatibility"][key]) for key, value in checks)
    except (InvalidSpecifier, InvalidVersion, KeyError, TypeError):
        return False


def _from_versions(release: JSON) -> str | None:
    try:
        value = release["migrations"]["from_versions"]
        SpecifierSet(value)
    except (InvalidSpecifier, KeyError, TypeError):
        return None
    return value if isinstance(value, str) else None


def evaluate(service: "DeploymentService") -> JSON:
    """Releases and update paths from the verified caches, against what is installed; never online."""
    try:
        inventory = service.state()["inventory"]
    except AgentError as error:
        if error.code == "deployment_not_initialized":
            return {"releases": [], "paths": []}
        raise
    installed = {
        entry["manifest"]["app_id"]: entry["manifest"]["version"]
        for entry in inventory["apps"]
        if entry["state"] != "schema_retained"
    }
    core = inventory["core"]["version"]
    installed[CORE_COMPONENT] = core
    # Prefer the target runtime recorded by the active release over the agent's own.
    runtime = inventory.get("runtime") or {}
    python_version = runtime.get("python") or platform.python_version()
    django_version = runtime.get("django") or service.config.get("django_version", "6.1.3")
    releases: list[JSON] = []
    candidates: dict[str, dict[str, JSON]] = {}
    for source in service.catalogue_sources():
        for release in cached_entries(service, source) or []:
            compatible = _compatible(release, core, python_version, django_version)
            current = installed.get(release["component_id"])
            state = (
                "incompatible"
                if not compatible
                else "current"
                if current and Version(release["version"]) <= Version(current)
                else "available"
            )
            releases.append(
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
            from_versions = _from_versions(release)
            if from_versions is not None:  # Same version from two sources: the first source wins.
                candidates.setdefault(release["component_id"], {}).setdefault(
                    release["version"],
                    {
                        "version": release["version"],
                        "source_id": source["id"],
                        "from_versions": from_versions,
                        "compatible": state != "incompatible",
                    },
                )
    paths = [
        component_path(
            CORE_COMPONENT,
            "core",
            core,
            list(candidates.get(CORE_COMPONENT, {}).values()),
            enforce_lines=not service.config.get("allow_version_jumps", False),
        )
    ]
    for entry in inventory["apps"]:
        if entry["state"] != "installed" or entry.get("origin") == "project-local":
            continue
        app_id = entry["manifest"]["app_id"]
        paths.append(
            component_path(
                app_id,
                "app",
                entry["manifest"]["version"],
                list(candidates.get(app_id, {}).values()),
                enforce_lines=False,
            )
        )
    return {"releases": releases, "paths": paths}


def check_updates(service: "DeploymentService") -> JSON:
    """Refresh every source, then evaluate: ``{checked_at, sources, releases, paths}``."""
    return {**refresh_sources(service), **evaluate(service)}


def update_status(service: "DeploymentService") -> JSON:
    """The last refresh and a fresh evaluation of the cached indexes (#493); never online."""
    path = service.store.root / "catalogue-status.json"
    status = load_json(path) if path.exists() else {"checked_at": None, "sources": []}
    return {"checked_at": status.get("checked_at"), "sources": status.get("sources", []), **evaluate(service)}


def fetch_release(service: "DeploymentService", component_id: str, version: str, source_id: str) -> JSON:
    sources = [source for source in service.catalogue_sources() if source["id"] == source_id]
    if len(sources) != 1 or not isinstance(component_id, str) or not isinstance(version, str):
        raise AgentError("invalid_catalogue_selection")
    source = sources[0]
    cache = source_cache(service, source)
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
    if source.get("publisher") and entry["publisher"] != source["publisher"]:
        raise AgentError("catalogue_publisher_mismatch", 409)
    if source.get("github"):
        tag, name = artifact_reference(entry["artifact"])
        github, private = GitHub(service.config), bool(source.get("private"))
        asset = find_asset(
            github.release_by_tag(source["github"], tag, private=private), name, missing="github_asset_missing"
        )
        content = github.download_asset(
            source["github"], asset, limit=service.config.get("max_upload_bytes", 128 * 1024 * 1024), private=private
        )
    elif source.get("url"):
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
