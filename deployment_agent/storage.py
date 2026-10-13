"""Durable filesystem journal; deliberately independent of the application DB."""

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID, uuid4


class AgentError(ValueError):
    def __init__(self, code, status=400):
        self.code = code
        self.status = status
        super().__init__(code)


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def file_hash(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    try:
        return str(UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        raise AgentError("invalid_uuid") from None


def artifact_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value):
        raise AgentError("invalid_digest")
    return value


def atomic_json(path, value, mode=0o600):
    atomic_bytes(path, canonical(value) + b"\n", mode)


def atomic_bytes(path, data, mode=0o600):
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


def load_json(path):
    try:
        return json.loads(Path(path).read_bytes())
    except (OSError, ValueError):
        raise AgentError("invalid_state", 409) from None


def inventory_files(root):
    """Include links as resources without following them; code links are refused elsewhere."""
    root = Path(root)
    result = {}
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
def deployment_lock(root):
    path = Path(root) / "deployment.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("a+b") as stream:
        os.chmod(path, 0o600)
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise AgentError("deployment_busy", 409) from None
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def private_file(path):
    path = Path(path)
    metadata = path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o027 or path.is_symlink():
        raise AgentError("insecure_private_file")
    return path


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.root.stat().st_mode & 0o007:
            raise AgentError("insecure_storage_root")
        for name in ("uploads", "plans", "operations", "backups", "catalogues"):
            (self.root / name).mkdir(exist_ok=True, mode=0o700)

    def path(self, collection, key):
        if collection == "uploads":
            return self.root / collection / f"{artifact_id(key)}.skapp"
        return self.root / collection / f"{identifier(key)}.json"

    def put(self, collection, key, value):
        atomic_json(self.path(collection, key), value)

    def get(self, collection, key):
        path = self.path(collection, key)
        if not path.exists():
            raise AgentError("not_found", 404)
        return load_json(path)

    def operations(self):
        return sorted((load_json(path) for path in (self.root / "operations").glob("*.json")), key=lambda row: row["created_at"], reverse=True)
