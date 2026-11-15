"""The organization icon (#485): a PNG of exactly 512 x 512 up to 256 KB, proven by the content."""

import struct

import pytest
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile

from skeleton.tenancy.branding import ICON_MAX_BYTES, _checked_icon


def png(width=512, height=512, size=200):
    head = b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + b"IHDR" + struct.pack(">II", width, height)
    return head + b"\x00" * max(0, size - len(head))


def message(upload):
    with pytest.raises(ValidationError) as error:
        _checked_icon(upload)
    return error.value.message_dict["icon"][0]


def test_a_square_png_of_512_is_accepted():
    _checked_icon(SimpleUploadedFile("icon.png", png()))


@pytest.mark.parametrize(
    ("name", "content", "expected"),
    [
        ("icon.png", png(256, 256), "512 × 512"),
        ("icon.png", png(512, 400), "512 × 512"),
        ("icon.jpg", b"\xff\xd8\xff" + b"\x00" * 100, "Use uma imagem PNG."),
        ("icon.png", b"\xff\xd8\xff" + b"\x00" * 100, "Use uma imagem PNG."),  # JPEG named .png
        ("icon.png", b"GIF89a" + b"\x00" * 100, "Use uma imagem PNG."),
        ("icon.png", png(size=300 * 1024), "256 KB"),
        ("icon.png", b"\x89PNG\r\n\x1a\n" + b"\x00" * 8 + b"XXXX" + b"\x00" * 50, "512 × 512"),
    ],
)
def test_refused_icons(name, content, expected):
    assert expected in message(SimpleUploadedFile(name, content))


def test_size_limit_is_256_kb():
    assert ICON_MAX_BYTES == 256 * 1024
    _checked_icon(SimpleUploadedFile("icon.png", png(size=ICON_MAX_BYTES)))
