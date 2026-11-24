"""The organization's notifications in its settings area (#785): pages and REST.

``notifications.manage`` opens them; the service re-checks it on every read and write. A type that
does not exist here (unknown, or of an app that is not active) answers the same 404 as no access.
"""

from __future__ import annotations

from django.conf import settings
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import translation
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_POST, require_safe
from rest_framework.response import Response

from skeleton.access.capabilities import NOTIFICATIONS_MANAGE
from skeleton.api.errors import BoundaryError, parse_revision
from skeleton.api.openapi import BOOLEAN, INTEGER, Operation, nullable, obj, results, string
from skeleton.api.views import TenantAPI, request_fields
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import tenant_page

from . import organization_settings as service


def _changed(request):
    from skeleton.web.errors import error_response

    return error_response(request, gettext("O aviso mudou. Atualize a página antes de tentar novamente."), status=409)


def _config_or_404(request, type_code):
    try:
        return service.type_config(actor=request.user, type_code=type_code)
    except (service.UnknownType, PermissionDenied):
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


def _messages(error: ValidationError) -> dict:
    if hasattr(error, "error_dict"):
        return {name: [m for item in items for m in item.messages] for name, items in error.error_dict.items()}
    return {"__all__": list(error.messages)}


def _crumbs(organization_id, *extra):
    return [
        {
            "label": gettext("Configuração"),
            "url": reverse("web:organization-admin", kwargs={"organization_id": organization_id}),
        },
        {
            "label": gettext("Notificações"),
            "url": reverse("notifications:organization-settings", kwargs={"organization_id": organization_id})
            if extra
            else "",
        },
        *extra,
    ]


@tenant_page(capability=NOTIFICATIONS_MANAGE)
@require_safe
def settings_list(request, organization_id):
    from .catalog import group_types

    try:
        configs = service.organization_overview(actor=request.user)
    except PermissionDenied:
        raise Http404 from None
    return render(
        request,
        "skeleton/notifications/settings/list.html",
        {
            "organization_id": organization_id,
            "groups": group_types(configs, kind_of=lambda config: config.kind),
            "breadcrumbs": _crumbs(organization_id),
        },
    )


def _texts(config):
    from .instance_settings import instance_texts, languages
    from .texts import render as render_text
    from .texts import sample_values

    names = dict(settings.LANGUAGES)
    platform = instance_texts(config.kind.code)
    items = []
    for code in languages():
        row = config.texts.get(code)
        with translation.override(code):
            factory = (str(config.kind.title or ""), str(config.kind.body or ""))
            if row is not None:
                title, body, source = row.title, row.body, "organization"
            elif code in platform:
                (title, body), source = platform[code], "instance"
            elif factory[0]:
                (title, body), source = factory, "type"
            else:
                title, body, source = str(config.kind.label), "", "label"
            values = {
                **sample_values(config.kind.fields),
                "organizacao": gettext("Sua organização"),
                "aviso": str(config.kind.label),
            }
            items.append(
                {
                    "language": code,
                    "language_label": names.get(code, code),
                    "row": row,
                    "title": title,
                    "body": body,
                    "source": source,
                    "preview_title": render_text(title, values) if source != "label" else title,
                    "preview_body": render_text(body, values) if body else "",
                }
            )
    return items


def _detail(request, organization_id, config, *, errors=None, posted=None, status=200):
    texts = _texts(config)
    for item in texts:
        if posted and posted.get("language") == item["language"]:
            item["title"], item["body"], item["errors"] = posted["title"], posted["body"], errors or {}
    return render(
        request,
        "skeleton/notifications/settings/detail.html",
        {
            "organization_id": organization_id,
            "config": config,
            "kind": config.kind,
            "texts": texts,
            "fields": [{"placeholder": "{" + item.name + "}", "label": item.label} for item in config.kind.fields],
            "errors": {} if posted else (errors or {}),
            "breadcrumbs": _crumbs(organization_id, {"label": config.kind.label, "url": ""}),
        },
        status=status,
    )


@tenant_page(capability=NOTIFICATIONS_MANAGE)
@require_safe
def settings_detail(request, organization_id, type_code):
    return _detail(request, organization_id, _config_or_404(request, type_code))


@tenant_page(capability=NOTIFICATIONS_MANAGE)
@require_http_methods(["GET", "POST"])
def settings_switch(request, organization_id, type_code, action):
    config = _config_or_404(request, type_code)
    if action not in ("enable", "disable") or not config.can_disable:
        raise Http404
    detail = reverse(
        "notifications:organization-setting", kwargs={"organization_id": organization_id, "type_code": config.kind.code}
    )
    context = {
        "organization_id": organization_id,
        "title": gettext("Desligar aviso"),
        "subject_label": config.kind.label,
        "message": gettext(
            "Nenhum membro da organização recebe este aviso enquanto ele estiver desligado. Cada pessoa continua "
            "escolhendo como recebe os outros avisos."
        ),
        "submit_label": gettext("Desligar"),
        "cancel_url": detail,
        "revision": config.setting.revision if config.setting is not None else None,
        "breadcrumbs": _crumbs(
            organization_id, {"label": config.kind.label, "url": detail}, {"label": gettext("Desligar"), "url": ""}
        ),
    }
    if request.method != "POST":
        if action == "enable":
            raise Http404
        return render(request, "skeleton/notifications/settings/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        with transaction.atomic():
            service.set_enabled(
                actor=request.user,
                type_code=config.kind.code,
                enabled=action == "enable",
                expected_revision=_revision(request),
            )
    except ConcurrentUpdate:
        return _changed(request)
    except ValidationError as error:
        codes = (
            {item.code for items in error.error_dict.values() for item in items}
            if hasattr(error, "error_dict")
            else set()
        )
        context["error"] = " ".join(m for items in _messages(error).values() for m in items)
        return render(
            request,
            "skeleton/notifications/settings/confirm.html",
            context,
            status=409 if "setting_locked" in codes else 400,
        )
    messages.success(request, gettext("Aviso ligado.") if action == "enable" else gettext("Aviso desligado."))
    return redirect(detail)


@tenant_page(capability=NOTIFICATIONS_MANAGE)
@require_POST
def settings_text(request, organization_id, type_code, language):
    from .instance_settings import languages

    config = _config_or_404(request, type_code)
    if language not in languages() or not config.can_edit_text:
        raise Http404
    if _unknown(request, {"title", "body", "revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    posted = {"language": language, "title": request.POST.get("title", ""), "body": request.POST.get("body", "")}
    try:
        with transaction.atomic():
            service.set_text(
                actor=request.user,
                type_code=config.kind.code,
                language=language,
                title=posted["title"],
                body=posted["body"],
                expected_revision=_revision(request),
            )
    except ConcurrentUpdate:
        return _changed(request)
    except ValidationError as error:
        locked = hasattr(error, "error_dict") and any(
            item.code == "setting_locked" for items in error.error_dict.values() for item in items
        )
        return _detail(
            request, organization_id, config, errors=_messages(error), posted=posted, status=409 if locked else 400
        )
    messages.success(request, gettext("Texto salvo."))
    return redirect("notifications:organization-setting", organization_id=organization_id, type_code=config.kind.code)


@tenant_page(capability=NOTIFICATIONS_MANAGE)
@require_http_methods(["GET", "POST"])
def settings_text_restore(request, organization_id, type_code, language):
    config = _config_or_404(request, type_code)
    row = config.texts.get(language)
    if row is None or not config.can_edit_text:
        raise Http404
    detail = reverse(
        "notifications:organization-setting", kwargs={"organization_id": organization_id, "type_code": config.kind.code}
    )
    context = {
        "organization_id": organization_id,
        "title": gettext("Restaurar texto padrão"),
        "subject_label": config.kind.label,
        "message": gettext(
            "O texto da organização neste idioma é descartado e volta a valer o texto da plataforma. Os avisos já "
            "entregues não mudam."
        ),
        "submit_label": gettext("Restaurar"),
        "submit_class": "btn-primary",
        "cancel_url": detail,
        "revision": row.revision,
        "breadcrumbs": _crumbs(
            organization_id, {"label": config.kind.label, "url": detail}, {"label": gettext("Restaurar"), "url": ""}
        ),
    }
    if request.method != "POST":
        return render(request, "skeleton/notifications/settings/confirm.html", context)
    if _unknown(request, {"revision"}):
        return HttpResponseBadRequest(gettext("Campo não permitido."))
    try:
        with transaction.atomic():
            service.clear_text(
                actor=request.user, type_code=config.kind.code, language=language, expected_revision=_revision(request)
            )
    except ConcurrentUpdate:
        return _changed(request)
    except ValidationError as error:
        context["error"] = " ".join(m for items in _messages(error).values() for m in items)
        return render(request, "skeleton/notifications/settings/confirm.html", context, status=409)
    messages.success(request, gettext("Texto padrão restaurado."))
    return redirect(detail)


# --------------------------------------------------------------------------- REST


TEXT = obj(
    {"language": string(), "title": string(), "body": string(), "source": string(), "revision": nullable(INTEGER)}
)
SETTING = obj(
    {
        "type_code": string(),
        "label": string(),
        "description": string(),
        "context": string(enum=["platform", "organization", "app"]),
        "mandatory": BOOLEAN,
        "enabled": BOOLEAN,
        "reason": string(),
        "can_disable": BOOLEAN,
        "can_edit_text": BOOLEAN,
        "locked": BOOLEAN,
        "revision": nullable(INTEGER),
    }
)
SETTING_DETAIL = obj({**SETTING["properties"], "texts": {"type": "array", "items": TEXT}})


def _setting_data(config, *, with_texts=False):
    from .registry import type_context

    data = {
        "type_code": config.kind.code,
        "label": str(config.kind.label),
        "description": str(config.kind.description or ""),
        "context": type_context(config.kind) or "organization",
        "mandatory": config.kind.mandatory,
        "enabled": config.enabled,
        "reason": config.reason,
        "can_disable": config.can_disable,
        "can_edit_text": config.can_edit_text,
        "locked": config.locked_enabled,
        "revision": config.setting.revision if config.setting is not None else None,
    }
    if with_texts:
        data["texts"] = [
            {
                "language": item["language"],
                "title": item["title"],
                "body": item["body"],
                "source": item["source"],
                "revision": item["row"].revision if item["row"] is not None else None,
            }
            for item in _texts(config)
        ]
    return data


def _api_config(request, type_code):
    try:
        return service.type_config(actor=request.user, type_code=type_code)
    except (service.UnknownType, PermissionDenied):
        raise Http404 from None


class SettingsAPI(TenantAPI):
    openapi = {
        "GET": Operation(
            "Avisos da organização e o estado de cada um", results(SETTING), capability=NOTIFICATIONS_MANAGE
        )
    }

    def get(self, request, organization_id):
        try:
            configs = service.organization_overview(actor=request.user)
        except PermissionDenied:
            raise Http404 from None
        return Response({"results": [_setting_data(config) for config in configs]})


class SettingAPI(TenantAPI):
    openapi = {
        "GET": Operation("Um aviso da organização, com os textos", SETTING_DETAIL, capability=NOTIFICATIONS_MANAGE),
        "PUT": Operation(
            "Liga ou desliga o aviso para todos os membros",
            SETTING_DETAIL,
            request=obj({"enabled": BOOLEAN}, required=("enabled",)),
            if_match=True,
            if_match_optional=True,
            capability=NOTIFICATIONS_MANAGE,
        ),
    }

    def get(self, request, organization_id, type_code):
        return Response(_setting_data(_api_config(request, type_code), with_texts=True))

    @transaction.atomic
    def put(self, request, organization_id, type_code):
        config = _api_config(request, type_code)
        data = request_fields(request)
        if set(data) != {"enabled"} or type(data["enabled"]) is not bool:
            raise BoundaryError("invalid_request", gettext("Envie enabled."))
        header = request.headers.get("If-Match")
        if config.setting is not None and header is None:
            raise BoundaryError("revision_required", gettext("Envie If-Match com a revisão atual."), 428)
        expected = parse_revision(header) if header is not None else None
        try:
            service.set_enabled(
                actor=request.user, type_code=config.kind.code, enabled=data["enabled"], expected_revision=expected
            )
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", gettext("O aviso foi alterado por outra operação."), 412) from None
        return Response(_setting_data(_api_config(request, type_code), with_texts=True))


class SettingTextAPI(TenantAPI):
    openapi = {
        "PUT": Operation(
            "Grava o texto da organização num idioma",
            SETTING_DETAIL,
            request=obj({"title": string(), "body": string()}, required=("title",)),
            if_match=True,
            if_match_optional=True,
            capability=NOTIFICATIONS_MANAGE,
        ),
        "DELETE": Operation(
            "Volta ao texto da plataforma", None, status=204, if_match=True, capability=NOTIFICATIONS_MANAGE
        ),
    }

    @transaction.atomic
    def put(self, request, organization_id, type_code, language):
        config = _api_config(request, type_code)
        data = request_fields(request)
        if set(data) - {"title", "body"} or "title" not in data:
            raise BoundaryError("invalid_request", gettext("Envie title e, opcionalmente, body."))
        row = config.texts.get(language)
        header = request.headers.get("If-Match")
        if row is not None and header is None:
            raise BoundaryError("revision_required", gettext("Envie If-Match com a revisão atual."), 428)
        expected = parse_revision(header) if header is not None else None
        try:
            service.set_text(
                actor=request.user,
                type_code=config.kind.code,
                language=language,
                title=data["title"],
                body=data.get("body", ""),
                expected_revision=expected,
            )
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", gettext("O texto foi alterado por outra operação."), 412) from None
        return Response(_setting_data(_api_config(request, type_code), with_texts=True))

    @transaction.atomic
    def delete(self, request, organization_id, type_code, language):
        config = _api_config(request, type_code)
        if language not in config.texts:
            raise Http404
        expected = parse_revision(request.headers.get("If-Match"))
        try:
            service.clear_text(
                actor=request.user, type_code=config.kind.code, language=language, expected_revision=expected
            )
        except ConcurrentUpdate:
            raise BoundaryError("revision_conflict", gettext("O texto foi alterado por outra operação."), 412) from None
        return Response(status=204)
