"""The text of a notification (#782): plain text with ``{field}`` placeholders, never a template.

A text is parsed by a closed grammar: ``{name}`` (lowercase, digits and underscores) is a
placeholder, ``{{`` and ``}}`` are literal braces, and any other brace is an error. There is no
attribute access, index, format spec or conversion (``str.format`` would allow ``{x.__class__}``),
no Django template and no HTML or Markdown: rendering only replaces placeholders by the values
the caller formatted, and the HTML form escapes every piece. Links (open, preferences,
unsubscribe) never come from the text: the core layout adds them. The only address a text may
carry is the value of ``{link}`` (#806), built by the core; in HTML only that exact address
becomes a link, never another one written in the text.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from django.core.exceptions import ValidationError
from django.utils.html import conditional_escape
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext

NAME = re.compile(r"[a-z][a-z0-9_]{0,39}")
TOKEN = re.compile(r"\{\{|\}\}|\{([^{}]*)\}|[{}]")
# The placeholders every type has (#806), filled by the core, never by the payload:
# ``organizacao``: the public name of the organization (empty for the platform's own notices);
# ``aviso``: the label of the type; ``plataforma``: the product name of the instance;
# ``data_atual``: the date the notification was created, in the recipient's time zone;
# ``link``: the address that opens the item of the notification, re-authorizing each click;
# ``destinatario`` and ``destinatario_primeiro_nome``: the display name and the first name of
# the person who receives it (only on that person's own row).
RECIPIENT = ("destinatario", "destinatario_primeiro_nome")
FIXED = ("organizacao", "aviso", "plataforma", "data_atual", "link", *RECIPIENT)
SAMPLE_LINK = "https://exemplo.test/notificacao"
TITLE_MAX = 150
BODY_MAX = 2000
BODY_LINES = 30


@dataclass(frozen=True)
class Segment:
    """A piece of literal text (``name`` empty) or a placeholder."""

    text: str = ""
    name: str = ""


def _error(message: str, code: str) -> ValidationError:
    return ValidationError(message, code=code)


def parse(text: str) -> tuple[Segment, ...]:
    """Split ``text`` into literal pieces and placeholders; a stray brace is ``unbalanced_brace``."""
    segments: list[Segment] = []
    literal: list[str] = []
    position = 0
    for match in TOKEN.finditer(text):
        literal.append(text[position : match.start()])
        piece = match.group(0)
        if piece == "{{":
            literal.append("{")
        elif piece == "}}":
            literal.append("}")
        elif match.group(1) is not None:
            name = match.group(1)
            if not NAME.fullmatch(name):
                raise _error(gettext("Marcador inválido: {%(name)s}.") % {"name": name}, "invalid_placeholder")
            if literal:
                segments.append(Segment(text="".join(literal)))
                literal = []
            segments.append(Segment(name=name))
        else:
            raise _error(gettext("Use {{ e }} para escrever chaves no texto."), "unbalanced_brace")
        position = match.end()
    literal.append(text[position:])
    if "".join(literal):
        segments.append(Segment(text="".join(literal)))
    return tuple(segments)


def placeholders(text: str) -> frozenset[str]:
    return frozenset(segment.name for segment in parse(text) if segment.name)


def _control(text: str, *, allow_newline: bool) -> bool:
    return any(ord(char) < 32 and not (allow_newline and char == "\n") or ord(char) == 127 for char in text)


def validate(text: str, *, allowed: frozenset[str] | set[str], kind: str) -> str:
    """Return ``text`` with normalized line breaks, or raise ``ValidationError`` with a ``code``.

    ``kind`` is ``title`` (1 to 150 characters, one line) or ``body`` (up to 2000 characters and
    30 lines). Every placeholder must be in ``allowed``.
    """
    if not isinstance(text, str):
        raise _error(gettext("Informe um texto."), "invalid")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if kind == "title":
        text = text.strip()
        if not text:
            raise _error(gettext("Informe o título."), "empty")
        if "\n" in text:
            raise _error(gettext("O título tem uma linha só."), "multiline")
        if len(text) > TITLE_MAX:
            raise _error(gettext("Use no máximo %(limit)d caracteres.") % {"limit": TITLE_MAX}, "too_long")
    else:
        text = text.strip("\n")
        if len(text) > BODY_MAX:
            raise _error(gettext("Use no máximo %(limit)d caracteres.") % {"limit": BODY_MAX}, "too_long")
        if text.count("\n") >= BODY_LINES:
            raise _error(gettext("Use no máximo %(limit)d linhas.") % {"limit": BODY_LINES}, "too_long")
    if _control(text, allow_newline=kind != "title"):
        raise _error(gettext("O texto tem caracteres de controle."), "control_char")
    unknown = sorted(placeholders(text) - set(allowed))
    if unknown:
        names = ", ".join("{" + name + "}" for name in unknown)
        raise _error(gettext("Marcadores que este aviso não tem: %(names)s.") % {"names": names}, "unknown_placeholder")
    return text


def render(text: str, values: Mapping[str, Any]) -> str:
    """Plain text with each placeholder replaced by ``str(value)``; a missing value is empty."""
    return "".join(
        segment.text if not segment.name else str(values.get(segment.name, "") or "") for segment in parse(text)
    )


def render_html(text: str, values: Mapping[str, Any] | None = None) -> SafeString:
    """Like :func:`render`, every piece escaped and each line break turned into ``<br>``."""
    escaped = conditional_escape(render(text, values or {}))
    return mark_safe(str(escaped).replace("\n", "<br>"))  # noqa: S308 - escaped above


def plain_html(text: str, *, link: str = "") -> SafeString:
    """Already rendered text as HTML: escaped, line breaks as ``<br>``; braces are never parsed again.

    ``link`` is the core's ``{link}`` address of this notification: each exact occurrence becomes
    an anchor; any other address in the text stays plain text.
    """
    escaped = str(conditional_escape(text or ""))
    if link and link.startswith(("https://", "http://", "/")):
        url = str(conditional_escape(link))
        escaped = escaped.replace(url, f'<a href="{url}">{url}</a>')
    return mark_safe(escaped.replace("\n", "<br>"))  # noqa: S308 - escaped above


def _date(raw: Any, *, with_time: bool) -> str:
    from django.utils import formats, timezone

    value = raw
    if isinstance(raw, str):
        try:
            value = datetime.fromisoformat(raw) if with_time or "T" in raw else date.fromisoformat(raw)
        except ValueError:
            return raw
    if isinstance(value, datetime):
        if timezone.is_aware(value):
            value = timezone.localtime(value)
        return formats.date_format(value, "DATETIME_FORMAT" if with_time else "DATE_FORMAT")
    if isinstance(value, date):
        return formats.date_format(value, "DATE_FORMAT")
    return str(raw)


def date_text(moment: date | datetime) -> str:
    """The date of ``moment`` in the active language and time zone."""
    return _date(moment, with_time=False)


def format_value(field: Any, raw: Any, *, values: Mapping[str, Any], people: Mapping[str, str] | None = None) -> str:
    """The text of one field in the active language and time zone; a bad value shows as itself."""
    if raw is None or raw == "":
        return ""
    kind = getattr(field, "kind", "text")
    try:
        if kind == "number":
            from django.utils.formats import number_format

            return str(number_format(raw, use_l10n=True, force_grouping=True))
        if kind in ("date", "datetime"):
            return _date(raw, with_time=kind == "datetime")
        if kind == "money":
            from django.utils.formats import number_format

            from skeleton.common.money import exponent, format_amount

            currency = str(values.get(field.currency_field) or "")
            places = exponent(currency)
            amount = Decimal(format_amount(int(raw), currency))
            return f"{number_format(amount, decimal_pos=places, use_l10n=True, force_grouping=True)} {currency}"
        if kind == "person":
            return (people or {}).get(str(raw)) or gettext("Pessoa indisponível")
    except (TypeError, ValueError, ArithmeticError, ValidationError):
        return str(raw)
    return str(raw)


def first_name(name: str) -> str:
    """The first word of a display name ("Maria Exemplo" → "Maria")."""
    parts = (name or "").split()
    return parts[0] if parts else ""


def product_name() -> str:
    """The product name of the instance, as the screens show it."""
    from django.conf import settings

    from skeleton.appearance.snapshot import current_appearance

    name = current_appearance().product_name
    return str(name or getattr(settings, "SKELETON_PRODUCT_NAME", "") or "Skeleton")


def fixed_samples(*, organization: str) -> dict[str, str]:
    """Example values of the fixed placeholders but ``aviso``, for the previews."""
    from django.utils import timezone

    person = gettext("Maria Exemplo")
    return {
        "organizacao": organization,
        "plataforma": product_name(),
        "data_atual": date_text(timezone.localdate()),
        "link": SAMPLE_LINK,
        "destinatario": person,
        "destinatario_primeiro_nome": first_name(person),
    }


def fixed_descriptions() -> tuple[tuple[str, str], ...]:
    """``(placeholder, description)`` of the fixed placeholders, in the order of the screens."""
    return (
        ("{destinatario}", gettext("nome de quem recebe")),
        ("{destinatario_primeiro_nome}", gettext("primeiro nome de quem recebe")),
        ("{organizacao}", gettext("nome da organização")),
        ("{plataforma}", gettext("nome da plataforma")),
        ("{aviso}", gettext("nome do aviso")),
        ("{data_atual}", gettext("data do envio")),
        ("{link}", gettext("endereço que abre o item do aviso")),
    )


def placeholder_list(kind: Any) -> list[dict[str, Any]]:
    """``[{placeholder, label}]`` of a type: its fields, then the fixed placeholders."""
    own = [{"placeholder": "{" + item.name + "}", "label": item.label} for item in kind.fields]
    return own + [{"placeholder": name, "label": label} for name, label in fixed_descriptions()]


def sample_values(fields: tuple[Any, ...]) -> dict[str, str]:
    """Example values for the preview of a text in the configuration screens."""
    from django.utils import timezone

    now = timezone.now()
    samples = {
        "text": gettext("Exemplo"),
        "number": "42",
        "date": _date(timezone.localdate(), with_time=False),
        "datetime": _date(now, with_time=True),
        "money": "1.234,56 BRL",
        "person": gettext("Maria Exemplo"),
    }
    return {field.name: samples.get(field.kind, gettext("Exemplo")) for field in fields}
