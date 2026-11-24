"""Notifications of the instance in the platform administration (#784).

The catalog of every notification type, grouped by context, and the page of one type: whether it
is sent, by which channels it reaches whoever has not chosen otherwise, and its text in each
language. Only the full platform administrator; every POST has CSRF, recent authentication, a
closed list of fields and the revision of the row it changes (409 when stale).
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_POST, require_safe

from skeleton.accounts.reauthentication import recent_authentication_required

from .guards import platform_page

SECTION = "notifications"


def _kind_or_404(type_code):
    from skeleton.notifications.registry import get_notification_type

    try:
        return get_notification_type(type_code)
    except KeyError:
        raise Http404 from None


def _unknown(request, allowed) -> bool:
    return bool(set(request.POST) - set(allowed) - {"csrfmiddlewaretoken"})


def _revision(request):
    raw = request.POST.get("revision", "")
    if raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return -1


def tabs(request, current):
    """The tabs of the notifications area of the platform; each only for who may open it."""
    from skeleton.accounts.platform import is_platform_admin
    from skeleton.notifications.platform import unread_count

    items = [
        {
            "label": gettext("Caixa"),
            "url": reverse("platform:notifications"),
            "key": "inbox",
            "count": unread_count(request.user),
        }
    ]
    if is_platform_admin(request.user):
        items.append({"label": gettext("Avisos"), "url": reverse("platform:notification-types"), "key": "types"})
        items.append(
            {"label": gettext("Comunicados"), "url": reverse("platform:announcements"), "key": "announcements"}
        )
    return [{**item, "current": item["key"] == current} for item in items]


VIEWS = {"unread": "", "all": "all", "archived": "archived"}


@platform_page(any_role=True)
@require_safe
def notifications_home(request):
    """The person's own platform inbox (#787): unread by default, all or archived."""
    from skeleton.notifications.platform import inbox_queryset
    from skeleton.web.views import pagination

    view = request.GET.get("view", "unread")
    if view not in VIEWS:
        view = "unread"
    filters = {"view": view} if view != "unread" else {}
    page = pagination(request, inbox_queryset(request.user, view=view), validated_filters=filters)
    base = reverse("platform:notifications")
    return render(
        request,
        "skeleton/web/platform/notifications/inbox.html",
        {
            "platform_section": SECTION,
            "tabs": tabs(request, "inbox"),
            "rows": page,
            "view": view,
            "views": [
                {"label": gettext("Não lidas"), "url": base, "current": view == "unread"},
                {"label": gettext("Todas"), "url": f"{base}?view=all", "current": view == "all"},
                {"label": gettext("Arquivadas"), "url": f"{base}?view=archived", "current": view == "archived"},
            ],
        },
    )


@platform_page(any_role=True)
@require_POST
def notification_change(request, sysid, action):
    from skeleton.notifications.platform import change

    if action not in ("read", "unread", "archive"):
        raise Http404
    if not change(request.user, sysid, action):
        raise Http404
    return redirect(
        request.POST.get("next") if request.POST.get("next") in _back_targets() else "platform:notifications"
    )


def _back_targets():
    base = reverse("platform:notifications")
    return {base, f"{base}?view=all", f"{base}?view=archived"}


@platform_page(any_role=True)
@require_POST
def notification_read_all(request):
    from skeleton.notifications.platform import read_all

    read_all(request.user)
    messages.success(request, gettext("Todos os avisos foram marcados como lidos."))
    return redirect("platform:notifications")


@platform_page(any_role=True)
@require_safe
def notification_open(request, sysid):
    """Mark the notice read and go where its type leads, re-authorized; else show the notice."""
    from skeleton.notifications.platform import change, destination, own_row

    row = own_row(request.user, sysid)
    if row is None:
        raise Http404
    change(request.user, sysid, "read")
    target = destination(request.user, row)
    if target:
        return redirect(target)
    return render(
        request,
        "skeleton/web/platform/notifications/notice.html",
        {
            "platform_section": SECTION,
            "row": row,
            "breadcrumbs": [
                {"label": gettext("Caixa"), "url": reverse("platform:notifications")},
                {"label": row.title, "url": ""},
            ],
        },
    )


@platform_page
@require_safe
def notification_types(request):
    from skeleton.notifications.catalog import AUDIENCE_LABELS, channel_list, group_types
    from skeleton.notifications.instance_settings import DEFAULT_STATE, enabled_in_instance, instance_state
    from skeleton.notifications.registry import all_notification_types

    query = (request.GET.get("q") or "").strip()[:100]
    state = instance_state()
    rows = []
    for kind in all_notification_types():
        if query and query.lower() not in f"{kind.label} {kind.description} {kind.code}".lower():
            continue
        chosen = state.get(kind.code, DEFAULT_STATE)
        channels = chosen.default_channels if chosen.default_channels is not None else kind.default_channels
        rows.append(
            {
                "kind": kind,
                "enabled": enabled_in_instance(kind, chosen),
                "channels": channel_list(channels),
                "audience": AUDIENCE_LABELS.get(kind.audience, kind.audience),
            }
        )
    return render(
        request,
        "skeleton/web/platform/notifications/types.html",
        {
            "platform_section": SECTION,
            "tabs": tabs(request, "types"),
            "groups": group_types(rows, kind_of=lambda row: row["kind"]),
            "query": query,
        },
    )


def _texts(kind):
    """Per language: the platform's row, the text that applies now and a preview with sample values."""
    from skeleton.notifications.instance_settings import languages, text_rows
    from skeleton.notifications.texts import render, sample_values

    rows = text_rows(kind.code)
    result = []
    names = dict(settings.LANGUAGES)
    for code in languages():
        row = rows.get(code)
        with translation.override(code):
            factory_title, factory_body = str(kind.title or ""), str(kind.body or "")
            values = {
                **sample_values(kind.fields),
                "organizacao": gettext("Organização Exemplo"),
                "aviso": str(kind.label),
            }
            title = row.title if row else (factory_title or str(kind.label))
            body = row.body if row else factory_body
            result.append(
                {
                    "language": code,
                    "language_label": names.get(code, code),
                    "row": row,
                    "title": title,
                    "body": body,
                    "source": "instance" if row else ("type" if factory_title else "label"),
                    "preview_title": render(title, values) if title else str(kind.label),
                    "preview_body": render(body, values) if body else "",
                }
            )
    return result


def _detail(request, kind, *, errors=None, posted=None, status=200):
    from skeleton.notifications.catalog import AUDIENCE_LABELS, CHANNEL_LABELS
    from skeleton.notifications.instance_settings import allowed_channels, enabled_in_instance, state_of

    state = state_of(kind.code)
    channels = state.default_channels if state.default_channels is not None else kind.default_channels
    texts = _texts(kind)
    for item in texts:
        if posted and posted.get("language") == item["language"]:
            item["title"], item["body"] = posted.get("title", ""), posted.get("body", "")
            item["errors"] = errors or {}
    return render(
        request,
        "skeleton/web/platform/notifications/type.html",
        {
            "platform_section": SECTION,
            "tabs": tabs(request, "types"),
            "kind": kind,
            "audience": AUDIENCE_LABELS.get(kind.audience, kind.audience),
            "enabled": enabled_in_instance(kind, state),
            "state": state,
            "channel_choices": [
                {"value": value, "label": CHANNEL_LABELS[value], "checked": value in channels}
                for value in allowed_channels(kind)
            ],
            "channels_from_instance": state.default_channels is not None,
            "texts": texts,
            "fields": [{"placeholder": "{" + item.name + "}", "label": item.label} for item in kind.fields],
            "breadcrumbs": [
                {"label": gettext("Avisos"), "url": reverse("platform:notification-types")},
                {"label": kind.label},
            ],
            "errors": {} if posted else (errors or {}),
            "error_list": [] if posted else [m for items in (errors or {}).values() for m in items],
        },
        status=status,
    )


@platform_page
@require_safe
def notification_type(request, type_code):
    return _detail(request, _kind_or_404(type_code))


def _messages(error: ValidationError) -> dict:
    if hasattr(error, "error_dict"):
        return {
            name: [str(message) for item in items for message in item.messages]
            for name, items in error.error_dict.items()
        }
    return {"__all__": [str(message) for message in error.messages]}


@platform_page
@require_POST
@recent_authentication_required
def notification_type_channels(request, type_code):
    from skeleton.notifications.instance_settings import set_instance_setting, state_of

    kind = _kind_or_404(type_code)
    if _unknown(request, {"channels", "revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    state = state_of(kind.code)
    try:
        set_instance_setting(
            actor=request.user,
            type_code=kind.code,
            enabled=state.enabled or kind.mandatory,
            default_channels=request.POST.getlist("channels"),
            expected_revision=_revision(request),
        )
    except ValidationError as error:
        return _detail(request, kind, errors=_messages(error), status=400)
    messages.success(request, gettext("Canais padrão salvos."))
    return redirect("platform:notification-type", type_code=kind.code)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def notification_type_switch(request, type_code, action):
    """Turn a type on (POST) or off (a confirmation, then POST): nobody receives a type turned off."""
    from skeleton.notifications.instance_settings import set_instance_setting, state_of

    kind = _kind_or_404(type_code)
    if action not in ("enable", "disable") or kind.mandatory:
        raise Http404
    state = state_of(kind.code)
    channels = list(state.default_channels if state.default_channels is not None else kind.default_channels)
    context = {
        "platform_section": SECTION,
        "eyebrow": gettext("Avisos"),
        "title": gettext("Desligar aviso"),
        "subject_label": kind.label,
        "message": gettext(
            "Nenhuma organização recebe este aviso enquanto ele estiver desligado. Quem já recebeu continua com o "
            "aviso na caixa."
        ),
        "submit_label": gettext("Desligar"),
        "cancel_url": reverse("platform:notification-type", kwargs={"type_code": kind.code}),
        "form": None,
        "revision": state.revision,
    }
    if request.method != "POST":
        if action == "enable":
            raise Http404
        return render(request, "skeleton/web/platform/notifications/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        set_instance_setting(
            actor=request.user,
            type_code=kind.code,
            enabled=action == "enable",
            default_channels=channels,
            expected_revision=_revision(request),
        )
    except ValidationError as error:
        context["error"] = " ".join(message for items in _messages(error).values() for message in items)
        return render(request, "skeleton/web/platform/notifications/confirm.html", context, status=400)
    messages.success(request, gettext("Aviso ligado.") if action == "enable" else gettext("Aviso desligado."))
    return redirect("platform:notification-type", type_code=kind.code)


@platform_page
@require_POST
@recent_authentication_required
def notification_type_text(request, type_code, language):
    from skeleton.notifications.instance_settings import languages, set_instance_text

    kind = _kind_or_404(type_code)
    if language not in languages() or kind.fixed_text:
        raise Http404
    if _unknown(request, {"title", "body", "revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    posted = {"language": language, "title": request.POST.get("title", ""), "body": request.POST.get("body", "")}
    try:
        set_instance_text(
            actor=request.user,
            type_code=kind.code,
            language=language,
            title=posted["title"],
            body=posted["body"],
            expected_revision=_revision(request),
        )
    except ValidationError as error:
        return _detail(request, kind, errors=_messages(error), posted=posted, status=400)
    messages.success(request, gettext("Texto salvo."))
    return redirect("platform:notification-type", type_code=kind.code)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def notification_type_text_restore(request, type_code, language):
    from skeleton.notifications.instance_settings import clear_instance_text, languages, text_rows

    kind = _kind_or_404(type_code)
    row = text_rows(kind.code).get(language)
    if language not in languages() or row is None:
        raise Http404
    context = {
        "platform_section": SECTION,
        "eyebrow": gettext("Avisos"),
        "title": gettext("Restaurar texto de fábrica"),
        "subject_label": kind.label,
        "message": gettext(
            "O texto da plataforma neste idioma é descartado e volta a valer o texto de fábrica do aviso. "
            "Os avisos já entregues não mudam."
        ),
        "submit_label": gettext("Restaurar"),
        "cancel_url": reverse("platform:notification-type", kwargs={"type_code": kind.code}),
        "revision": row.revision,
    }
    if request.method != "POST":
        return render(request, "skeleton/web/platform/notifications/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    clear_instance_text(
        actor=request.user, type_code=kind.code, language=language, expected_revision=_revision(request)
    )
    messages.success(request, gettext("Texto de fábrica restaurado."))
    return redirect("platform:notification-type", type_code=kind.code)


# --------------------------------------------------------------------------- announcements (#789)


def _announcement_or_404(sysid):
    from skeleton.notifications.models import PlatformAnnouncement

    record = PlatformAnnouncement.objects.filter(pk=sysid).first()
    if record is None:
        raise Http404
    return record


def _slugs(record):
    """The chosen organizations as slugs, one per line, for the form."""
    from skeleton.tenancy.models import Organization

    if not record.organizations:
        return ""
    rows = Organization.objects.filter(pk__in=record.organizations).values_list("pk", "slug")
    names = {str(pk): slug for pk, slug in rows}
    return "\n".join(sorted(names.get(str(pk), str(pk)) for pk in record.organizations))


def _posted(request):
    scope = request.POST.get("scope", "all")
    organizations = [line for line in request.POST.get("organizations", "").splitlines() if line.strip()]
    return {
        "title": request.POST.get("title", ""),
        "body": request.POST.get("body", ""),
        "audience": request.POST.get("audience", ""),
        "scope": scope,
        "organizations_text": request.POST.get("organizations", ""),
        "organizations": organizations if scope == "list" else [],
    }


def _announcement_form(request, *, record=None, data=None, errors=None, status=200):
    from skeleton.notifications.models import PlatformAnnouncement
    from skeleton.notifications.texts import plain_html

    if data is None:
        data = {
            "title": record.title if record else "",
            "body": record.body if record else "",
            "audience": record.audience if record else PlatformAnnouncement.Audience.ADMINS,
            "scope": "list" if record and record.organizations else "all",
            "organizations_text": _slugs(record) if record else "",
        }
    return render(
        request,
        "skeleton/web/platform/notifications/announcement_form.html",
        {
            "platform_section": SECTION,
            "tabs": tabs(request, "announcements"),
            "record": record,
            "data": data,
            "errors": errors or {},
            "error_list": [m for items in (errors or {}).values() for m in items],
            "audiences": PlatformAnnouncement.Audience.choices,
            "preview_body": plain_html(data.get("body", "")),
            "breadcrumbs": [
                {"label": gettext("Comunicados"), "url": reverse("platform:announcements")},
                {"label": record.title if record else gettext("Novo comunicado"), "url": ""},
            ],
        },
        status=status,
    )


@platform_page
@require_safe
def announcements(request):
    from skeleton.notifications.models import PlatformAnnouncement
    from skeleton.web.views import pagination

    rows = pagination(request, PlatformAnnouncement.objects.order_by("-created_at", "sysid"))
    return render(
        request,
        "skeleton/web/platform/notifications/announcements.html",
        {"platform_section": SECTION, "tabs": tabs(request, "announcements"), "rows": rows},
    )


ANNOUNCEMENT_FIELDS = {"title", "body", "audience", "scope", "organizations", "revision", "action"}


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def announcement_create(request):
    from skeleton.notifications.announcements import create_announcement

    if request.method != "POST":
        return _announcement_form(request)
    if _unknown(request, ANNOUNCEMENT_FIELDS):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    data = _posted(request)
    try:
        record = create_announcement(
            actor=request.user,
            title=data["title"],
            body=data["body"],
            audience=data["audience"],
            organizations=data["organizations"],
        )
    except ValidationError as error:
        return _announcement_form(request, data=data, errors=_messages(error), status=400)
    if request.POST.get("action") == "review":
        return redirect("platform:announcement-send", sysid=record.pk)
    messages.success(request, gettext("Rascunho salvo."))
    return redirect("platform:announcement", sysid=record.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def announcement_detail(request, sysid):
    from skeleton.notifications.announcements import update_announcement

    record = _announcement_or_404(sysid)
    if record.state == record.State.SENT:
        if request.method == "POST":
            return HttpResponseBadRequest(gettext("Um comunicado enviado não muda."))
        return render(
            request,
            "skeleton/web/platform/notifications/announcement_sent.html",
            {
                "platform_section": SECTION,
                "tabs": tabs(request, "announcements"),
                "record": record,
                "sender": _sender(record),
                "breadcrumbs": [
                    {"label": gettext("Comunicados"), "url": reverse("platform:announcements")},
                    {"label": record.title, "url": ""},
                ],
            },
        )
    if request.method != "POST":
        return _announcement_form(request, record=record)
    if _unknown(request, ANNOUNCEMENT_FIELDS):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    data = _posted(request)
    try:
        update_announcement(
            actor=request.user,
            announcement_id=record.pk,
            expected_revision=_revision(request),
            title=data["title"],
            body=data["body"],
            audience=data["audience"],
            organizations=data["organizations"],
        )
    except ValidationError as error:
        return _announcement_form(request, record=record, data=data, errors=_messages(error), status=400)
    if request.POST.get("action") == "review":
        return redirect("platform:announcement-send", sysid=record.pk)
    messages.success(request, gettext("Rascunho salvo."))
    return redirect("platform:announcement", sysid=record.pk)


def _sender(record):
    from skeleton.accounts.models import User

    user = User.objects.filter(pk=record.sent_by).select_related("profile").first() if record.sent_by else None
    if user is None:
        return ""
    profile = getattr(user, "profile", None)
    return (getattr(profile, "display_name", "") or "").strip() or user.email


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def announcement_send(request, sysid):
    from skeleton.notifications.announcements import send_announcement, targets

    record = _announcement_or_404(sysid)
    if record.state != record.State.DRAFT:
        raise Http404
    count = len(targets(record))
    context = {
        "platform_section": SECTION,
        "eyebrow": gettext("Comunicados"),
        "title": gettext("Enviar comunicado"),
        "subject_label": record.title,
        "message": gettext(
            "Enviar a %(count)s organizações? Quem recebe: %(audience)s. Depois de enviado, o comunicado não pode "
            "ser alterado nem cancelado."
        )
        % {"count": count, "audience": record.get_audience_display()},
        "submit_label": gettext("Enviar"),
        "submit_class": "btn-primary",
        "cancel_url": reverse("platform:announcement", kwargs={"sysid": record.pk}),
        "revision": record.revision,
    }
    if request.method != "POST":
        return render(request, "skeleton/web/platform/notifications/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        sent = send_announcement(actor=request.user, announcement_id=record.pk, expected_revision=_revision(request))
    except ValidationError as error:
        context["error"] = " ".join(message for items in _messages(error).values() for message in items)
        return render(request, "skeleton/web/platform/notifications/confirm.html", context, status=400)
    messages.success(
        request, gettext("Comunicado enviado a %(count)s organizações.") % {"count": sent.organization_count}
    )
    return redirect("platform:announcement", sysid=record.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
def announcement_delete(request, sysid):
    from skeleton.notifications.announcements import delete_announcement

    record = _announcement_or_404(sysid)
    if record.state != record.State.DRAFT:
        raise Http404
    context = {
        "platform_section": SECTION,
        "eyebrow": gettext("Comunicados"),
        "title": gettext("Excluir rascunho"),
        "subject_label": record.title,
        "message": gettext("O rascunho sai da lista. A exclusão é lógica e fica na auditoria."),
        "submit_label": gettext("Excluir rascunho"),
        "cancel_url": reverse("platform:announcement", kwargs={"sysid": record.pk}),
        "revision": record.revision,
    }
    if request.method != "POST":
        return render(request, "skeleton/web/platform/notifications/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    delete_announcement(actor=request.user, announcement_id=record.pk, expected_revision=_revision(request))
    messages.success(request, gettext("Rascunho excluído."))
    return redirect("platform:announcements")
