"""The shared SSR editor of block pages (#619): add, edit, move, delete and publish, without JavaScript.

A concrete page (the platform home, the organization's public page and internal home) declares a
``PageEditor`` with its *store* (``load() -> PageState`` and ``save(document=, published=,
expected_revision=)``), its authorization (the decorators of the view) and its base template, and
routes the operations with ``editor_urls``. Every change is a POST with CSRF and the revision the
person read: an old revision answers 409 and writes nothing; a validation error answers 400 with the
``error-summary``. The store validates with ``skeleton.common.page_blocks.validate_page``; this
module only turns forms into documents and documents into screens.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponse, HttpResponseBadRequest, HttpResponseNotAllowed
from django.shortcuts import redirect, render
from django.urls import path, reverse
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.csrf import csrf_protect

from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.common.page_blocks import BLOCK_LABELS, BLOCK_TYPES, FIELDS, PageState

__all__ = ["OPERATIONS", "PageEditor", "PageStore", "dispatch", "editor_route_names", "editor_urls"]

OPERATIONS = ("index", "preview", "publish", "add", "edit", "move", "delete")
STALE = gettext_lazy("Esta página foi alterada por outra pessoa. Recarregue para ver a versão atual.")
CSRF = "csrfmiddlewaretoken"
SAFE = ("GET", "HEAD")


class PageStore(Protocol):
    def load(self) -> PageState: ...

    def save(self, *, document: dict[str, Any], published: bool, expected_revision: int) -> PageState: ...


@dataclass(frozen=True)
class PageEditor:
    """What one concrete page gives the editor; links are ``reverse(f"{url_name}-<op>", kwargs=url_kwargs)``."""

    kind: str
    store: PageStore
    url_name: str
    base_template: str
    title: Any
    preview: Callable[[Any, PageState], HttpResponse]
    url_kwargs: Mapping[str, Any] = field(default_factory=dict)
    eyebrow: Any = ""
    description: Any = ""
    widget_choices: tuple[tuple[str, Any], ...] = ()
    notice: Any = ""
    notice_link: tuple[Any, str] | None = None
    extra_context: Mapping[str, Any] = field(default_factory=dict)

    def url(self, operation: str, **kwargs: Any) -> str:
        name = self.url_name if operation == "index" else f"{self.url_name}-{operation}"
        return reverse(name, kwargs={**self.url_kwargs, **kwargs})


def editor_route_names(name: str, namespace: str = "") -> tuple[str, ...]:
    """The view names ``editor_urls(..., name, ...)`` creates, with ``namespace:`` when given."""
    prefix = f"{namespace}:" if namespace else ""
    return tuple(prefix + (name if operation == "index" else f"{name}-{operation}") for operation in OPERATIONS)


def editor_urls(prefix: str, name: str, view: Callable[..., HttpResponse]) -> list[Any]:
    """The routes of one editor under ``prefix`` (ending with ``/``); ``view(request, operation=, index=)``."""
    return [
        path(prefix, view, {"operation": "index"}, name=name),
        path(f"{prefix}preview/", view, {"operation": "preview"}, name=f"{name}-preview"),
        path(f"{prefix}publish/", view, {"operation": "publish"}, name=f"{name}-publish"),
        path(f"{prefix}blocks/new/", view, {"operation": "add"}, name=f"{name}-add"),
        path(f"{prefix}blocks/<int:index>/", view, {"operation": "edit"}, name=f"{name}-edit"),
        path(f"{prefix}blocks/<int:index>/move/", view, {"operation": "move"}, name=f"{name}-move"),
        path(f"{prefix}blocks/<int:index>/delete/", view, {"operation": "delete"}, name=f"{name}-delete"),
    ]


# --------------------------------------------------------------------------- forms


class _RevisionForm(forms.Form):
    revision = forms.IntegerField(widget=forms.HiddenInput, min_value=0)


def _links_text(items: Any) -> str:
    return "\n".join(f"{item.get('label', '')} | {item.get('url', '')}" for item in items or [])


def _parse_links(text: str) -> list[dict[str, str]]:
    items = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if not line.strip():
            continue
        label, _, url = line.partition("|")
        items.append({"label": label.strip(), "url": url.strip()})
    return items


def block_form_class(block_type: str, widget_choices: tuple[tuple[str, Any], ...] = ()) -> type[forms.Form]:
    """A Django form with one field per ``FIELDS[block_type]``; limits stay in ``validate_page``."""
    fields: dict[str, forms.Field] = {}
    for spec in FIELDS[block_type]:
        help_text = spec.help_text
        if spec.kind == "bool":
            fields[spec.name] = forms.BooleanField(label=spec.label, required=False, help_text=help_text)
        elif spec.kind == "widget":
            fields[spec.name] = forms.ChoiceField(
                label=spec.label, choices=[("", gettext_lazy("Escolha um quadro")), *widget_choices], required=False
            )
        elif spec.kind in ("text", "links"):
            fields[spec.name] = forms.CharField(
                label=spec.label,
                required=False,
                strip=False,
                help_text=help_text,
                widget=forms.Textarea(attrs={"rows": 6 if spec.kind == "text" else 4, "spellcheck": "true"}),
            )
        else:
            attrs: dict[str, Any] = {"maxlength": spec.max_length} if spec.max_length else {}
            if spec.kind == "email":
                attrs["autocomplete"] = "email"
            fields[spec.name] = forms.CharField(
                label=spec.label, required=False, strip=False, help_text=help_text, widget=forms.TextInput(attrs=attrs)
            )
        # The * mark of the form (#474); the limits and the requirement stay in validate_page.
        fields[spec.name].marked_required = spec.required  # type: ignore[attr-defined]
    fields["revision"] = forms.IntegerField(widget=forms.HiddenInput, min_value=0)
    return type(f"{block_type.title()}BlockForm", (forms.Form,), fields)


def _initial(block: Mapping[str, Any]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for spec in FIELDS[block["type"]]:
        value = block.get(spec.name)
        values[spec.name] = _links_text(value) if spec.kind == "links" else value
    return values


def _block_from(block_type: str, data: Mapping[str, Any]) -> dict[str, Any]:
    block: dict[str, Any] = {"type": block_type}
    for spec in FIELDS[block_type]:
        raw = data.get(spec.name)
        if spec.kind == "bool":
            block[spec.name] = bool(raw)
        elif spec.kind == "links":
            block[spec.name] = _parse_links(raw or "")
        elif spec.kind == "text":
            block[spec.name] = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
        else:
            block[spec.name] = raw or ""
    return block


def _place_errors(form: forms.Form, error: ValidationError, index: int | None) -> None:
    """Put the errors of ``validate_page`` on the fields of the block being edited, the rest on top."""
    details = error.message_dict if hasattr(error, "error_dict") else {"document": error.messages}
    prefix = f"blocks.{index}." if index is not None else None
    for key, items in details.items():
        target = None
        label = ""
        if prefix is not None and key.startswith(prefix):
            rest = key[len(prefix) :].split(".")
            if rest[0] in form.fields:
                target = rest[0]
                if rest[0] == "items" and len(rest) > 1 and rest[1].isdigit():
                    label = gettext("Linha %(number)d: ") % {"number": int(rest[1]) + 1}
        for message in items:
            form.add_error(target, label + message)


# --------------------------------------------------------------------------- screens


def summary(block: Mapping[str, Any], widget_labels: Mapping[str, Any]) -> str:
    """A short plain-text line that tells blocks apart in the editor list."""
    if block.get("type") == "widget":
        return str(widget_labels.get(block.get("name", ""), block.get("name", "")))
    for name in ("title", "heading", "body", "email", "phone", "address", "hours"):
        value = block.get(name)
        if isinstance(value, str) and value.strip():
            text = " ".join(value.split())
            return text if len(text) <= 80 else text[:79] + "…"
    if block.get("type") == "links":
        return ", ".join(item.get("label", "") for item in block.get("items", []))[:80]
    return ""


def _context(request: Any, editor: PageEditor, state: PageState, **extra: Any) -> dict[str, Any]:
    labels = dict(editor.widget_choices)
    blocks = state.blocks
    return {
        **editor.extra_context,
        "editor": editor,
        "base_template": editor.base_template,
        "state": state,
        "revision": state.revision,
        "rows": [
            {
                "index": index,
                "number": index + 1,
                "type": block.get("type"),
                "label": BLOCK_LABELS.get(str(block.get("type", "")), block.get("type")),
                "summary": summary(block, labels),
                "edit_url": editor.url("edit", index=index),
                "move_url": editor.url("move", index=index),
                "delete_url": editor.url("delete", index=index),
                "first": index == 0,
                "last": index == len(blocks) - 1,
            }
            for index, block in enumerate(blocks)
        ],
        "block_types": [{"type": name, "label": BLOCK_LABELS[name]} for name in BLOCK_TYPES[editor.kind]],
        "index_url": editor.url("index"),
        "add_url": editor.url("add"),
        "publish_url": editor.url("publish"),
        "preview_url": editor.url("preview"),
        **extra,
    }


def _index(request: Any, editor: PageEditor, state: PageState, *, error: Any = "", status: int = 200) -> HttpResponse:
    return render(
        request, "skeleton/web/pages/editor.html", _context(request, editor, state, error=error), status=status
    )


def _block_page(
    request: Any,
    editor: PageEditor,
    state: PageState,
    form: forms.Form,
    *,
    block_type: str,
    index: int | None,
    status: int = 200,
) -> HttpResponse:
    if index is None:
        action = editor.url("add") + f"?type={block_type}"
        title = gettext("Novo bloco: %(label)s") % {"label": BLOCK_LABELS[block_type]}
    else:
        action = editor.url("edit", index=index)
        title = gettext("Editar bloco %(number)d: %(label)s") % {"number": index + 1, "label": BLOCK_LABELS[block_type]}
    context = _context(
        request, editor, state, form=form, block_type=block_type, form_action=action, form_title=title, index=index
    )
    return render(request, "skeleton/web/pages/editor-block.html", context, status=status)


def _revision(request: Any) -> int | None:
    form = _RevisionForm(request.POST)
    return form.cleaned_data["revision"] if form.is_valid() else None


def _unknown(request: Any, allowed: set[str]) -> bool:
    return bool(set(request.POST) - allowed - {CSRF} or request.FILES)


def _save(request: Any, editor: PageEditor, document: dict[str, Any], published: bool, revision: int) -> PageState:
    return editor.store.save(document=document, published=published, expected_revision=revision)


def _stale(request: Any, editor: PageEditor) -> HttpResponse:
    return _index(request, editor, editor.store.load(), error=STALE, status=409)


def _validation_message(error: ValidationError) -> str:
    return " ".join(error.messages)


@csrf_protect
def dispatch(request: Any, editor: PageEditor, operation: str, *, index: int | None = None) -> HttpResponse:
    """Run one editor ``operation``; the caller has already authorized the person and the organization."""
    if operation not in OPERATIONS:
        raise Http404
    if operation in ("index", "preview"):
        if request.method not in SAFE:
            return HttpResponseNotAllowed(SAFE)
        state = editor.store.load()
        return _index(request, editor, state) if operation == "index" else editor.preview(request, state)
    if operation == "add":
        return _add(request, editor)
    if operation == "edit":
        return _edit(request, editor, index)
    if operation == "delete":
        return _delete(request, editor, index)
    if request.method != "POST":
        return HttpResponseNotAllowed(["POST"])
    if operation == "move":
        return _move(request, editor, index)
    return _publish(request, editor)


def _add(request: Any, editor: PageEditor) -> HttpResponse:
    block_type = request.GET.get("type", "")  # In the query string for GET and POST (the form action).
    if block_type not in BLOCK_TYPES[editor.kind]:
        raise Http404
    state = editor.store.load()
    form_class = block_form_class(block_type, editor.widget_choices)
    if request.method in SAFE:
        defaults = {spec.name: True for spec in FIELDS[block_type] if spec.kind == "bool"}
        form = form_class(initial={**defaults, "revision": state.revision})
        return _block_page(request, editor, state, form, block_type=block_type, index=None)
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    if _unknown(request, {*form_class.base_fields}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    form = form_class(request.POST)
    if not form.is_valid():
        return _block_page(request, editor, state, form, block_type=block_type, index=None, status=400)
    revision = form.cleaned_data["revision"]
    if revision != state.revision:
        return _stale(request, editor)
    block = _block_from(block_type, form.cleaned_data)
    document = {"version": state.document.get("version", 1), "blocks": [*state.blocks, block]}
    try:
        _save(request, editor, document, state.published, revision)
    except ConcurrentUpdate:
        return _stale(request, editor)
    except ValidationError as error:
        _place_errors(form, error, len(state.blocks))
        return _block_page(request, editor, state, form, block_type=block_type, index=None, status=400)
    messages.success(request, gettext("Bloco acrescentado."), fail_silently=True)
    return redirect(editor.url("index"))


def _existing(state: PageState, index: int | None) -> tuple[int, dict[str, Any]]:
    blocks = state.blocks
    if index is None or not 0 <= index < len(blocks):
        raise Http404
    return index, blocks[index]


def _edit(request: Any, editor: PageEditor, index: int | None) -> HttpResponse:
    state = editor.store.load()
    if request.method in SAFE:
        index, block = _existing(state, index)
        form_class = block_form_class(block["type"], editor.widget_choices)
        form = form_class(initial={**_initial(block), "revision": state.revision})
        return _block_page(request, editor, state, form, block_type=block["type"], index=index)
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    revision = _revision(request)
    if revision is None or revision != state.revision:
        return _stale(request, editor)
    index, block = _existing(state, index)
    form_class = block_form_class(block["type"], editor.widget_choices)
    if _unknown(request, {*form_class.base_fields}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    form = form_class(request.POST)
    if not form.is_valid():
        return _block_page(request, editor, state, form, block_type=block["type"], index=index, status=400)
    blocks = state.blocks
    blocks[index] = _block_from(block["type"], form.cleaned_data)
    try:
        _save(
            request, editor, {"version": state.document.get("version", 1), "blocks": blocks}, state.published, revision
        )
    except ConcurrentUpdate:
        return _stale(request, editor)
    except ValidationError as error:
        _place_errors(form, error, index)
        return _block_page(request, editor, state, form, block_type=block["type"], index=index, status=400)
    messages.success(request, gettext("Bloco salvo."), fail_silently=True)
    return redirect(editor.url("index"))


def _move(request: Any, editor: PageEditor, index: int | None) -> HttpResponse:
    if _unknown(request, {"revision", "direction"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    state = editor.store.load()
    revision = _revision(request)
    if revision is None or revision != state.revision:
        return _stale(request, editor)
    index, _block = _existing(state, index)
    direction = request.POST.get("direction")
    target = index - 1 if direction == "up" else index + 1 if direction == "down" else None
    blocks = state.blocks
    if target is None or not 0 <= target < len(blocks):
        return _index(request, editor, state, error=gettext("Este bloco não pode ir nessa direção."), status=400)
    blocks[index], blocks[target] = blocks[target], blocks[index]
    try:
        _save(
            request, editor, {"version": state.document.get("version", 1), "blocks": blocks}, state.published, revision
        )
    except ConcurrentUpdate:
        return _stale(request, editor)
    except ValidationError as error:
        return _index(request, editor, state, error=_validation_message(error), status=400)
    messages.success(request, gettext("Bloco movido."), fail_silently=True)
    return redirect(editor.url("index"))


def _delete(request: Any, editor: PageEditor, index: int | None) -> HttpResponse:
    state = editor.store.load()
    if request.method in SAFE:
        index, block = _existing(state, index)
        context = _context(
            request,
            editor,
            state,
            block_label=BLOCK_LABELS.get(block["type"], block["type"]),
            block_summary=summary(block, dict(editor.widget_choices)),
            number=index + 1,
            delete_action=editor.url("delete", index=index),
        )
        return render(request, "skeleton/web/pages/editor-delete.html", context)
    if request.method != "POST":
        return HttpResponseNotAllowed(["GET", "POST"])
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    revision = _revision(request)
    if revision is None or revision != state.revision:
        return _stale(request, editor)
    index, _block = _existing(state, index)
    blocks = state.blocks
    del blocks[index]
    try:
        _save(
            request, editor, {"version": state.document.get("version", 1), "blocks": blocks}, state.published, revision
        )
    except ConcurrentUpdate:
        return _stale(request, editor)
    except ValidationError as error:
        return _index(request, editor, state, error=_validation_message(error), status=400)
    messages.success(request, gettext("Bloco excluído."), fail_silently=True)
    return redirect(editor.url("index"))


def _publish(request: Any, editor: PageEditor) -> HttpResponse:
    if _unknown(request, {"revision", "published"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    state = editor.store.load()
    revision = _revision(request)
    if revision is None or revision != state.revision:
        return _stale(request, editor)
    value = request.POST.get("published")
    if value not in ("on", "off"):
        return _index(request, editor, state, error=gettext("Escolha publicar ou despublicar."), status=400)
    published = value == "on"
    try:
        _save(request, editor, state.document, published, revision)
    except ConcurrentUpdate:
        return _stale(request, editor)
    except ValidationError as error:
        return _index(request, editor, state, error=_validation_message(error), status=400)
    messages.success(
        request, gettext("Página publicada.") if published else gettext("Página despublicada."), fail_silently=True
    )
    return redirect(editor.url("index"))
