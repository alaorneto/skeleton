"""Pages made of structured blocks (#619): the document format and its validation, without I/O.

A configurable page (the public home of the platform, the public page and the internal home of an
organization) is a versioned JSON document of plain-text blocks. Nothing here is HTML or Markdown:
the templates in ``skeleton/web/templates/skeleton/web/pages/`` show every value with autoescape.
``validate_page`` is the only door: unknown keys, types, schemes and control characters are refused
with a message per block and field, and what it returns is the normalized document to store.

Pure on purpose (rule R1 of ``tests/unit/test_core_layering.py``): no other ``skeleton.*`` package,
no database. The editor (``skeleton/web/page_editor.py``) reads ``FIELDS`` to build its forms.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from django.core.exceptions import ValidationError
from django.core.validators import URLValidator, validate_email
from django.utils.translation import gettext, gettext_lazy

__all__ = [
    "BLOCK_LABELS",
    "BLOCK_TYPES",
    "DEFAULT_ORGANIZATION_HOME",
    "EMPTY_PAGE",
    "FIELDS",
    "MAX_BLOCKS",
    "MAX_BYTES",
    "ORGANIZATION_HOME",
    "ORGANIZATION_PUBLIC",
    "PAGE_KINDS",
    "PLATFORM_HOME",
    "BlockField",
    "PageState",
    "empty_page",
    "validate_page",
]

PLATFORM_HOME = "platform_home"
ORGANIZATION_PUBLIC = "organization_public"
ORGANIZATION_HOME = "organization_home"
PAGE_KINDS = (PLATFORM_HOME, ORGANIZATION_PUBLIC, ORGANIZATION_HOME)

VERSION = 1
MAX_BLOCKS = 20
MAX_BYTES = 32_768
MAX_LINKS = 10
MAX_URL = 500


@dataclass(frozen=True)
class BlockField:
    """One field of a block type, as the editor shows it and the validation reads it.

    ``kind``: ``line`` (one line of text), ``text`` (several lines), ``bool``, ``email``, ``phone``,
    ``links`` (the list of ``{label, url}`` items) or ``widget`` (a registered widget name).
    """

    name: str
    kind: str
    label: Any
    max_length: int = 0
    required: bool = False
    help_text: Any = ""


def _heading() -> BlockField:
    return BlockField("heading", "line", gettext_lazy("Título da seção"), 120)


FIELDS: dict[str, tuple[BlockField, ...]] = {
    "hero": (
        BlockField("title", "line", gettext_lazy("Título"), 120, required=True),
        BlockField("subtitle", "line", gettext_lazy("Subtítulo"), 300),
        BlockField(
            "login_button",
            "bool",
            gettext_lazy("Mostrar o botão “Entrar”"),
            help_text=gettext_lazy("Leva à tela de login."),
        ),
    ),
    "text": (
        _heading(),
        BlockField(
            "body",
            "text",
            gettext_lazy("Texto"),
            4000,
            required=True,
            help_text=gettext_lazy("Texto simples. Separe os parágrafos com uma linha em branco."),
        ),
    ),
    "contact": (
        BlockField("email", "email", gettext_lazy("E-mail"), 254),
        BlockField(
            "phone",
            "phone",
            gettext_lazy("Telefone"),
            40,
            help_text=gettext_lazy("Números, espaço e os sinais + ( ) - ."),
        ),
        BlockField("address", "text", gettext_lazy("Endereço"), 300),
        BlockField("hours", "text", gettext_lazy("Horário de atendimento"), 300),
    ),
    "links": (
        _heading(),
        BlockField(
            "items",
            "links",
            gettext_lazy("Links"),
            required=True,
            help_text=gettext_lazy(
                "Um link por linha, no formato “Rótulo | endereço”. O endereço começa com https:// "
                "ou é um caminho deste site começando com /. Até 10 links."
            ),
        ),
    ),
    "apps": (_heading(),),
    "tasks": (_heading(),),
    "notifications": (_heading(),),
    "widget": (BlockField("name", "widget", gettext_lazy("Quadro"), required=True),),
}

BLOCK_LABELS: dict[str, Any] = {
    "hero": gettext_lazy("Destaque"),
    "text": gettext_lazy("Texto"),
    "contact": gettext_lazy("Contato"),
    "links": gettext_lazy("Links"),
    "apps": gettext_lazy("Aplicativos"),
    "tasks": gettext_lazy("Minhas tarefas"),
    "notifications": gettext_lazy("Notificações"),
    "widget": gettext_lazy("Quadro de aplicativo"),
}

_PUBLIC = ("hero", "text", "contact", "links")
BLOCK_TYPES: dict[str, tuple[str, ...]] = {
    PLATFORM_HOME: _PUBLIC,
    ORGANIZATION_PUBLIC: _PUBLIC,
    ORGANIZATION_HOME: ("text", "contact", "links", "apps", "tasks", "notifications", "widget"),
}

# A new page: no block, unpublished. Copy it (``empty_page()``) before changing anything.
EMPTY_PAGE: dict[str, Any] = {"version": VERSION, "blocks": []}
# What the internal home shows while no page is published (#624).
DEFAULT_ORGANIZATION_HOME: dict[str, Any] = {
    "version": VERSION,
    "blocks": [{"type": name, "heading": ""} for name in ("apps", "tasks", "notifications")],
}

_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_CONTROL_MULTILINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f-\x9f]")
_PHONE = re.compile(r"[0-9+().\- ]*")
_HTTPS = URLValidator(schemes=["https"])


@dataclass(frozen=True)
class PageState:
    """A stored page: its normalized document, whether it is published and the revision for CAS."""

    document: dict[str, Any] = field(default_factory=lambda: empty_page())
    published: bool = False
    revision: int = 0

    @property
    def blocks(self) -> list[dict[str, Any]]:
        blocks = self.document.get("blocks") if isinstance(self.document, dict) else None
        return list(blocks) if isinstance(blocks, list) else []


def empty_page() -> dict[str, Any]:
    return {"version": VERSION, "blocks": []}


class _Errors:
    def __init__(self) -> None:
        self.items: dict[str, list[str]] = {}

    def add(self, key: str, message: str) -> None:
        self.items.setdefault(key, []).append(message)

    def raise_if_any(self) -> None:
        if self.items:
            raise ValidationError(self.items)


def _size(document: Any) -> int | None:
    try:
        return len(json.dumps(document, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        return None


def _text(errors: _Errors, key: str, value: Any, spec: BlockField) -> str:
    if value is None:
        value = ""
    if not isinstance(value, str):
        errors.add(key, gettext("Use um texto."))
        return ""
    value = value.strip()
    pattern = _CONTROL_MULTILINE if spec.kind == "text" else _CONTROL
    if pattern.search(value):
        if spec.kind == "text":
            errors.add(key, gettext("Use só texto simples, sem caracteres de controle."))
        else:
            errors.add(key, gettext("Use uma única linha de texto simples, sem caracteres de controle."))
        return value
    if spec.max_length and len(value) > spec.max_length:
        errors.add(key, gettext("Use até %(limit)d caracteres.") % {"limit": spec.max_length})
    elif spec.required and not value:
        errors.add(key, gettext("Preencha este campo."))
    return value


def _url(errors: _Errors, key: str, value: Any) -> str:
    if not isinstance(value, str):
        errors.add(key, gettext("Use um texto."))
        return ""
    value = value.strip()
    if not value:
        errors.add(key, gettext("Informe o endereço do link."))
        return value
    if len(value) > MAX_URL:
        errors.add(key, gettext("Use até %(limit)d caracteres.") % {"limit": MAX_URL})
        return value
    invalid = gettext("Use um endereço https:// ou um caminho deste site começando com /.")
    if _CONTROL.search(value) or "\\" in value or any(char.isspace() for char in value):
        errors.add(key, invalid)
        return value
    if value.startswith("/"):
        if "//" in value:  # "//host" is another site; no double slash anywhere in a local path.
            errors.add(key, invalid)
        return value
    if not value.lower().startswith("https://"):
        errors.add(key, invalid)
        return value
    try:
        _HTTPS(value)
    except ValidationError:
        errors.add(key, invalid)
    return value


def _links(errors: _Errors, key: str, value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list):
        errors.add(key, gettext("Informe de 1 a %(limit)d links.") % {"limit": MAX_LINKS})
        return []
    if not 1 <= len(value) <= MAX_LINKS:
        errors.add(key, gettext("Informe de 1 a %(limit)d links.") % {"limit": MAX_LINKS})
    label_spec = BlockField("label", "line", "", 60, required=True)
    items = []
    for position, item in enumerate(value[:MAX_LINKS]):
        item_key = f"{key}.{position}"
        if not isinstance(item, dict):
            errors.add(item_key, gettext("Link inválido."))
            continue
        unknown = set(item) - {"label", "url"}
        for name in sorted(unknown, key=str):
            errors.add(f"{item_key}.{name}", gettext("Campo não permitido."))
        label = _text(errors, f"{item_key}.label", item.get("label", ""), label_spec)
        url = _url(errors, f"{item_key}.url", item.get("url", ""))
        items.append({"label": label, "url": url})
    return items


def _block(errors: _Errors, key: str, block: Any, kind: str, known_widgets: frozenset[str]) -> dict[str, Any] | None:
    if not isinstance(block, dict):
        errors.add(key, gettext("Bloco inválido."))
        return None
    block_type = block.get("type")
    if not isinstance(block_type, str) or block_type not in FIELDS:
        errors.add(f"{key}.type", gettext("Tipo de bloco desconhecido."))
        return None
    if block_type not in BLOCK_TYPES[kind]:
        errors.add(f"{key}.type", gettext("Este tipo de bloco não vale nesta página."))
        return None
    specs = FIELDS[block_type]
    allowed = {"type", *(spec.name for spec in specs)}
    for name in sorted(set(block) - allowed, key=str):
        errors.add(f"{key}.{name}", gettext("Campo não permitido."))
    result: dict[str, Any] = {"type": block_type}
    for spec in specs:
        field_key = f"{key}.{spec.name}"
        value = block.get(spec.name)
        if spec.kind == "bool":
            if value is None:
                value = True
            if not isinstance(value, bool):
                errors.add(field_key, gettext("Use verdadeiro ou falso."))
                value = True
            result[spec.name] = value
        elif spec.kind == "links":
            result[spec.name] = _links(errors, field_key, value)
        elif spec.kind == "widget":
            name = value if isinstance(value, str) else ""
            if name not in known_widgets:
                errors.add(field_key, gettext("Escolha um quadro da lista."))
            result[spec.name] = name
        else:
            text = _text(errors, field_key, value, spec)
            if text and spec.kind == "email" and field_key not in errors.items:
                try:
                    validate_email(text)
                except ValidationError:
                    errors.add(field_key, gettext("Informe um e-mail válido."))
            if text and spec.kind == "phone" and field_key not in errors.items and not _PHONE.fullmatch(text):
                errors.add(field_key, gettext("Use só números, espaço e os sinais + ( ) - ."))
            result[spec.name] = text
    if block_type == "contact" and not any(result[spec.name] for spec in specs):
        errors.add(key, gettext("Preencha ao menos um meio de contato."))
    return result


def validate_page(
    document: Any, *, kind: str, known_widgets: frozenset[str] = frozenset(), allow_empty: bool = False
) -> dict[str, Any]:
    """The normalized ``document`` of a page of ``kind``, or ``ValidationError`` by block and field.

    Error keys: ``document``, ``version``, ``blocks``, ``blocks.<i>``, ``blocks.<i>.<field>`` and
    ``blocks.<i>.items.<j>.<field>`` (0-based). ``allow_empty`` accepts a document without blocks
    (a page that is not published); a published page needs from 1 to ``MAX_BLOCKS`` blocks.
    """
    if kind not in PAGE_KINDS:
        raise ValueError(f"Unknown page kind: {kind!r}.")
    errors = _Errors()
    if not isinstance(document, dict):
        raise ValidationError({"document": [gettext("Documento de página inválido.")]})
    size = _size(document)
    if size is None:
        raise ValidationError({"document": [gettext("Documento de página inválido.")]})
    if size > MAX_BYTES:
        raise ValidationError(
            {"document": [gettext("A página passou do limite de %(limit)d KiB.") % {"limit": MAX_BYTES // 1024}]}
        )
    for name in sorted(set(document) - {"version", "blocks"}, key=str):
        errors.add(str(name), gettext("Campo não permitido."))
    version = document.get("version")
    if type(version) is not int or version != VERSION:
        errors.add("version", gettext("Versão de documento não suportada."))
    blocks = document.get("blocks")
    if not isinstance(blocks, list):
        errors.add("blocks", gettext("Lista de blocos inválida."))
        blocks = []
    minimum = 0 if allow_empty else 1
    if not minimum <= len(blocks) <= MAX_BLOCKS:
        if blocks:
            errors.add("blocks", gettext("Use até %(limit)d blocos.") % {"limit": MAX_BLOCKS})
        else:
            errors.add("blocks", gettext("A página publicada precisa de ao menos um bloco."))
    normalized = [
        _block(errors, f"blocks.{index}", block, kind, frozenset(known_widgets))
        for index, block in enumerate(blocks[:MAX_BLOCKS])
    ]
    errors.raise_if_any()
    result = {"version": VERSION, "blocks": [block for block in normalized if block is not None]}
    if (_size(result) or 0) > MAX_BYTES:  # Defaults filled in may grow a document at the limit.
        raise ValidationError(
            {"document": [gettext("A página passou do limite de %(limit)d KiB.") % {"limit": MAX_BYTES // 1024}]}
        )
    return result
