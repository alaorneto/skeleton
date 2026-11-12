"""Comment bodies: plain text only, escaped on output, with automatic links.

No Markdown and no HTML: the body is stored as typed and every rendering escapes it first;
``linkify`` then turns ``http``/``https`` URLs of the escaped text into links that open
without referrer. Nothing the author typed can become markup.
"""

from __future__ import annotations

import re

from django.core.exceptions import ValidationError
from django.utils.html import escape
from django.utils.safestring import SafeString, mark_safe
from django.utils.translation import gettext

from .models import MAX_BODY

URL = re.compile(r"\bhttps?://[^\s<>\"']+", re.IGNORECASE)
TRAILING = ".,;:!?)]}"
FORBIDDEN = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def clean_body(value: object) -> str:
    if not isinstance(value, str):
        raise ValidationError({"body": gettext("Use um texto.")})
    body = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not body:
        raise ValidationError({"body": gettext("O comentário está vazio.")})
    if len(body) > MAX_BODY:
        raise ValidationError({"body": gettext("Use até %(count)d caracteres.") % {"count": MAX_BODY}})
    if FORBIDDEN.search(body):
        raise ValidationError({"body": gettext("O comentário tem caracteres de controle.")})
    return body


def linkify(body: str) -> SafeString:
    """Escaped HTML of a body with its URLs as links; the only markup is ours."""
    escaped = str(escape(body))

    def link(match: re.Match[str]) -> str:
        url = match.group(0)
        tail = ""
        while url and url[-1] in TRAILING:
            url, tail = url[:-1], url[-1] + tail
        # ``url`` is already escaped: it is safe inside the quoted attribute and as text.
        return f'<a href="{url}" rel="nofollow noopener noreferrer" target="_blank">{url}</a>{tail}'

    from .mentions import TOKEN

    def mention(match: re.Match[str]) -> str:
        # The label is the token's own (already escaped) text; the sysid stays out of the markup.
        return f'<span class="mention" data-mention="{match.group(2)}">@{match.group(1)}</span>'

    return mark_safe(URL.sub(link, TOKEN.sub(mention, escaped)))  # noqa: S308 - built from escaped text only
