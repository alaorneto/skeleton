"""Bounded, corruption-tolerant reads of zip archives and the no-follow artifact snapshot."""

import os
import stat
import zipfile
import zlib
from pathlib import PurePosixPath

from . import limits
from .errors import PackageError, _fail
from .primitives import _path


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


def _read_member(archive, info, maximum=None):
    """One member, read once, within its limit; a corrupt member is a PackageError (#255)."""
    maximum = limits.MAX_FILE_BYTES if maximum is None else maximum  # Read at call time.
    if info.file_size > maximum:
        _fail("Release metadata/member size limit exceeded.")
    try:
        data = archive.read(info)
    except (zlib.error, EOFError, zipfile.BadZipFile, NotImplementedError) as exc:
        raise PackageError("Corrupt archive member.") from exc
    if len(data) != info.file_size:
        _fail("Archive member size mismatch.")
    return data


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
