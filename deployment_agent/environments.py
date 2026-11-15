"""The publisher environment (#495): the lock and wheels a release package brings for this target.

``environments/<core digest>.json`` records which uploaded release package carries the
dependencies of a core package. The bytes are never kept extracted: every use verifies the
signed release package in ``uploads/`` again and reads the members from that verified snapshot.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from skeleton.system.packages import PackageError, match_release_target, read_release_member, validate_release

from .storage import AgentError, atomic_bytes, atomic_json, load_json, now
from .types import JSON

if TYPE_CHECKING:
    from skeleton.system.packages import VerifiedRelease

    from .config import Config
    from .storage import Store


def _record_path(store: "Store", core_digest: str) -> Path:
    return store.root / "environments" / f"{core_digest}.json"


def check(store: "Store", verified: "VerifiedRelease") -> bool:
    """True when already recorded; a conflict when the same core came with another release package."""
    path = _record_path(store, verified.core_digest)
    if not path.exists():
        return False
    if load_json(path).get("release_digest") != verified.digest:
        # The same core never gets another set of dependencies: that is another version.
        raise AgentError("release_environment_conflict", 409)
    return True


def record(store: "Store", verified: "VerifiedRelease", target_id: str) -> None:
    """Remember that ``verified`` carries the dependencies of its core for ``target_id``."""
    if check(store, verified):
        return
    path = _record_path(store, verified.core_digest)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_json(
        path,
        {
            "core_digest": verified.core_digest,
            "release_digest": verified.digest,
            "version": verified.manifest["version"],
            "publisher": verified.manifest["publisher"],
            "target": target_id,
            "recorded_at": now(),
        },
        0o600,
    )


@dataclass(frozen=True)
class PublisherEnvironment:
    release_digest: str
    target: JSON
    _verified: Any = field(repr=False, compare=False)

    def lock_text(self) -> str:
        """The target's lock, read from the verified snapshot."""
        try:
            return read_release_member(self._verified, self.target["lock"]).decode("utf-8")
        except (PackageError, UnicodeError):
            raise AgentError("package_verification_failed", 409) from None

    def materialize(self, directory: Path) -> Path:
        """Write the target's ``requirements.lock`` and ``wheels/`` into a new directory."""
        directory = Path(directory)
        directory.mkdir(parents=True, exist_ok=False, mode=0o700)
        wheels = directory / "wheels"
        wheels.mkdir(mode=0o700)
        lock = directory / "requirements.lock"
        atomic_bytes(lock, read_release_member(self._verified, self.target["lock"]), 0o600)
        for member in self.target["wheels"]:
            # validate_release already refused unsafe names; only the file name is used anyway.
            atomic_bytes(wheels / Path(member).name, read_release_member(self._verified, member), 0o644)
        return lock


def publisher_environment(
    config: "Config | Mapping[str, Any]", store: "Store", core_digest: str, target_environment: Mapping[str, Any]
) -> PublisherEnvironment | None:
    """The verified publisher environment of a core package for this target, or None."""
    path = _record_path(store, core_digest)
    if not path.exists():
        return None
    saved = load_json(path)
    release_digest = saved.get("release_digest")
    try:
        verified = validate_release(store.path("uploads", str(release_digest)), config["approved_publishers"])
    except (PackageError, AgentError, OSError):
        raise AgentError("package_verification_failed", 409) from None
    if verified.core_digest != core_digest or verified.digest != release_digest:
        raise AgentError("package_verification_failed", 409)
    target = match_release_target(verified.manifest, target_environment)
    if target is None:
        return None
    return PublisherEnvironment(verified.digest, target, verified)
