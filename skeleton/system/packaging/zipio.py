"""Bounded, corruption-tolerant reads of zip archives and the no-follow artifact snapshot."""

import os
import stat
import zipfile
import zlib
from pathlib import PurePosixPath

from . import limits
from .errors import PackageError, _fail
from .primitives import _path

READ_CHUNK_BYTES = 65536


def _zip_members(archive, maximum=None):
    members = archive.infolist()
    if len(members) > (limits.MAX_MEMBERS if maximum is None else maximum):  # Read at call time.
        _fail("Archive contains too many members.")
    seen = set()
    files = {}
    directories = set()
    total = 0
    for info in members:
        raw = info.filename
        if info.orig_filename != raw:
            _fail("Truncated/NUL archive filename.")
        name = _path(raw[:-1] if raw.endswith("/") else raw)
        folded = name.casefold()
        if folded in seen:
            _fail("Duplicate or normalized archive collision.")
        seen.add(folded)
        mode = info.external_attr >> 16
        if (
            stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)
            or info.flag_bits & 1
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
        ):
            _fail("Links, special files, encryption and unsupported compression are forbidden.")
        if info.is_dir():
            if info.file_size or stat.S_IFMT(mode) == stat.S_IFREG:
                _fail("Invalid directory archive member.")
            directories.add(name)
            continue
        if (
            stat.S_IFMT(mode) == stat.S_IFDIR
            or info.file_size > limits.MAX_FILE_BYTES
            or info.file_size > max(1, info.compress_size) * limits.MAX_EXPANSION_RATIO
        ):
            _fail("Archive expansion limit exceeded.")
        total += info.file_size
        if total > limits.MAX_TOTAL_BYTES:
            _fail("Archive uncompressed size limit exceeded.")
        files[name] = info
    file_names = {p.casefold() for p in files}
    for path in files:
        if any(str(parent).casefold() in file_names for parent in PurePosixPath(path).parents if str(parent) != "."):
            _fail("File/directory archive collision.")
    for directory in directories:
        if not any(p.startswith(directory + "/") for p in files):
            _fail("Unowned empty archive directory.")
    return files


def read_bounded(archive, info, limit, *, max_expansion_ratio=None):
    """Stream a zip member in bounded chunks, never trusting ``info.file_size`` (#891)."""
    if info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
        _fail("Links, special files, encryption and unsupported compression are forbidden.")
    if limit < 0 or info.file_size > limit:
        _fail("Release metadata/member size limit exceeded.")
    ratio = limits.MAX_EXPANSION_RATIO if max_expansion_ratio is None else max_expansion_ratio
    ratio_cap = max(1, info.compress_size) * ratio
    effective_cap = min(limit, ratio_cap)
    out = bytearray()
    try:
        with archive.open(info) as handle:
            while True:
                remaining = effective_cap + 1 - len(out)
                if remaining <= 0:
                    break
                chunk = handle.read(min(READ_CHUNK_BYTES, remaining))
                if not chunk:
                    break
                out += chunk
                if len(out) > limit:
                    _fail("Release metadata/member size limit exceeded.")
                if len(out) > ratio_cap:
                    _fail("Archive expansion limit exceeded.")
    except PackageError:
        raise
    except (zlib.error, EOFError, zipfile.BadZipFile, NotImplementedError, RuntimeError) as exc:
        raise PackageError("Corrupt archive member.") from exc
    if len(out) > limit:
        _fail("Release metadata/member size limit exceeded.")
    if len(out) > ratio_cap:
        _fail("Archive expansion limit exceeded.")
    if len(out) != info.file_size:
        _fail("Archive member size mismatch.")
    return bytes(out)


def _read_member(archive, info, maximum=None, *, max_expansion_ratio=None):
    """One member, streamed once within its real byte and expansion limits (#255, #891)."""
    maximum = limits.MAX_FILE_BYTES if maximum is None else maximum  # Read at call time.
    return read_bounded(archive, info, maximum, max_expansion_ratio=max_expansion_ratio)


def _protected_snapshot(path, maximum=None):
    """Read once from a regular no-follow descriptor into immutable bytes."""
    maximum = limits.MAX_ARCHIVE_BYTES if maximum is None else maximum
    if not hasattr(os, "O_NOFOLLOW"):
        # Never weakened to a plain open: the no-follow read is the guarantee (#255).
        _fail("Artifact verification requires a POSIX filesystem (os.O_NOFOLLOW is unavailable).")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, "rb") as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                _fail("Compressed archive size limit exceeded or file is not regular.")
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if len(data) > maximum:
                _fail("Compressed archive size limit exceeded.")
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ) or len(data) != before.st_size:
                _fail("Package changed while reading its snapshot.")
            return data
    except OSError as exc:
        raise PackageError("Package file is unavailable.") from exc
