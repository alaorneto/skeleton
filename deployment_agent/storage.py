"""Durable filesystem journal; deliberately independent of the application DB."""

import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import traceback
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path, PurePath
from typing import Any, TextIO
from uuid import UUID, uuid4

from .types import JSON, StrPath


class AgentError(ValueError):
    """A stable public code; details carry only identities, ranges and versions."""

    log: str | None = None  # The command log file of a failed subprocess (#244).

    def __init__(self, code: str, status: int = 400, details: Any = None) -> None:
        self.code = code
        self.status = status
        self.details = details
        super().__init__(code)


def canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path: StrPath) -> str:
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def now() -> str:
    return datetime.now(UTC).isoformat()


def identifier(value: object) -> str:
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise AgentError("invalid_uuid") from None


def artifact_id(value: object) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise AgentError("invalid_digest")
    return value


def atomic_json(path: StrPath, value: object, mode: int = 0o600) -> None:
    atomic_bytes(path, canonical(value) + b"\n", mode)


def atomic_bytes(path: StrPath, data: bytes, mode: int = 0o600) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def load_json(path: StrPath) -> Any:
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        raise AgentError("invalid_state", 409) from None


def inventory_files(root: StrPath) -> JSON:
    """Include links as resources without following them; code links are refused elsewhere."""
    root = Path(root)
    result: JSON = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        if path.is_symlink():
            result[relative] = {"sha256": hashlib.sha256(os.readlink(path).encode()).hexdigest(), "type": "symlink"}
        elif path.is_file():
            result[relative] = {"sha256": file_hash(path), "size": path.stat().st_size, "type": "file"}
        elif not path.is_dir():
            raise AgentError("unsupported_file_type", 409)
    return result


@contextmanager
def deployment_lock(root: StrPath, *, blocking: bool = False) -> Iterator[None]:
    path = Path(root) / "deployment.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        except BlockingIOError:
            raise AgentError("deployment_busy", 409) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def private_file(path: StrPath) -> Path:
    path = Path(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027 or path.is_symlink():
        raise AgentError("insecure_private_file")
    return path


def log_internal_error(error: BaseException, stream: TextIO | None = None) -> None:
    """Emit one JSON line locating the failure; never the message, request, token or subprocess output."""
    frames = traceback.extract_tb(error.__traceback__)[-25:]
    record = {
        "timestamp": datetime.now(UTC).isoformat(),
        "level": "ERROR",
        "event": "agent.internal_error",
        "error_class": type(error).__name__,
        "error_stack": [f"{PurePath(frame.filename).name}:{frame.lineno}:{frame.name}" for frame in frames],
    }
    stream = stream or sys.stderr
    stream.write(json.dumps(record, sort_keys=True) + "\n")
    stream.flush()


class Store:
    def __init__(self, root: StrPath) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o007:
            raise AgentError("insecure_storage_root")
        for name in ("uploads", "plans", "operations", "backups", "catalogues"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)

    def path(self, collection: str, key: str) -> Path:
        if collection == "uploads":
            return self.root / collection / f"{artifact_id(key)}.skapp"
        return self.root / collection / f"{identifier(key)}.json"

    def put(self, collection: str, key: str, value: object) -> None:
        atomic_json(self.path(collection, key), value)

    def get(self, collection: str, key: str) -> Any:
        path = self.path(collection, key)
        if not path.exists():
            raise AgentError("not_found", 404)
        return load_json(path)

    def plans(self) -> list[tuple[str, Any, float]]:
        """``(plan_id, record, mtime)`` of every stored preview plan."""
        result = []
        for path in sorted((self.root / "plans").glob("*.json")):
            result.append((path.stem, load_json(path), path.stat().st_mtime))
        return result

    def operations(self, limit: int | None = None) -> list[Any]:
        """Operations newest first; with limit, only the most recently written files are parsed (#281)."""
        paths = list((self.root / "operations").glob("*.json"))
        if limit is not None:
            paths = sorted(paths, key=lambda path: path.stat().st_mtime_ns, reverse=True)[:limit]
        return sorted((load_json(path) for path in paths), key=lambda row: row["created_at"], reverse=True)

    def operation_states(self) -> dict[str, str]:
        """``{operation_id: state}`` of every operation, from an index kept beside the journal.

        The index is derived: each entry records the file's mtime_ns, and any file whose mtime
        changed (or that the index lacks) is read again, so a lost concurrent update never hides a
        pending operation. Security decisions other than ensure_available keep reading the files.
        """
        index_path = self.root / "operations-index.json"
        try:
            index = load_json(index_path)
        except (OSError, ValueError, AgentError):
            index = {}
        result, fresh, changed = {}, {}, False
        for path in (self.root / "operations").glob("*.json"):
            stamp = path.stat().st_mtime_ns
            entry = index.get(path.stem) if isinstance(index, dict) else None
            if not isinstance(entry, list) or len(entry) != 2 or entry[1] != stamp:
                entry, changed = [load_json(path)["state"], stamp], True
            fresh[path.stem] = entry
            result[path.stem] = entry[0]
        if changed or set(fresh) != set(index if isinstance(index, dict) else {}):
            atomic_json(index_path, fresh)
        return result
