"""Detect a file type from its first bytes and require the extension to agree.

A small signature table in code instead of a dependency: only the types an
``AttachmentPolicy`` can allow are recognized, and anything else is refused.
"""

from __future__ import annotations

from pathlib import PurePath

HEAD_BYTES = 512
EXTENSIONS = {
    "application/pdf": {".pdf"},
    "image/png": {".png"},
    "image/jpeg": {".jpg", ".jpeg"},
    "image/gif": {".gif"},
    "image/webp": {".webp"},
    "text/plain": {".txt"},
    "text/csv": {".csv"},
    "application/zip": {".zip"},
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": {".docx"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {".xlsx"},
}
ZIP_FAMILY = {
    ".zip": "application/zip",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}
KNOWN_TYPES = frozenset(EXTENSIONS)


def _text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        head.decode("utf-8")
    except UnicodeDecodeError as error:
        # A multi-byte character cut at the end of the sample is still text.
        return error.start >= len(head) - 3
    return True


def detect(head: bytes, filename: str) -> str | None:
    """The MIME type proven by the content and the extension, or None."""
    extension = PurePath(filename).suffix.lower()
    if head.startswith(b"%PDF-"):
        kind = "application/pdf"
    elif head.startswith(b"\x89PNG\r\n\x1a\n"):
        kind = "image/png"
    elif head.startswith(b"\xff\xd8\xff"):
        kind = "image/jpeg"
    elif head.startswith((b"GIF87a", b"GIF89a")):
        kind = "image/gif"
    elif head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        kind = "image/webp"
    elif head.startswith(b"PK\x03\x04"):
        kind = ZIP_FAMILY.get(extension, "application/zip")
    elif _text(head) and extension in {".txt", ".csv"}:
        kind = "text/csv" if extension == ".csv" else "text/plain"
    else:
        return None
    return kind if extension in EXTENSIONS[kind] else None
