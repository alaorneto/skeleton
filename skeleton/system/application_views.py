import re

from django import forms
from django.core.exceptions import ValidationError
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods, require_safe
from rest_framework.response import Response

from skeleton.access.capabilities import APPS_MANAGE, capabilities_for
from skeleton.api.errors import BoundaryError
from skeleton.api.views import TenantAPI, etag, request_fields
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import tenant_page

from .administration import (
    application_identity,
    resolve_application_screen,
    screen_sections,
    tenant_screen_url,
)
from .app_registry import all_apps, get_app
from .applications import application_state, application_states, configure_application


def _manifest(app_label):
    try:
        return get_app(app_label)
    except KeyError:
        raise Http404 from None


def require_apps_manager(user):
    """The apps panel exists only for ``apps.manage`` (app manager or administrator): 404 otherwise (#24)."""
    if APPS_MANAGE not in capabilities_for(user):
        raise Http404


def _apps_capability(request, kwargs):
    require_apps_manager(request.user)
    return APPS_MANAGE


def visible(state):
    """The organization sees available apps, and enabled ones the platform withdrew (to close them)."""
    return state["available"] or state["enabled"]


def _visible_manifest(app_label):
    manifest = _manifest(app_label)
    if not visible(application_state(app_label)):
        raise Http404  # An unavailable app looks like an app that is not installed.
    return manifest


def state_data(state):
    """REST shape of an ApplicationState (unchanged from the former application_state dict)."""
    return {
        "app_label": state.app_label,
        "available": state.available,
        "label": state.label,
        "description": state.description,
        "app_id": state.app_id,
        "version": state.version,
        "enabled": state.enabled,
        "configuration": {item.name: item.value for item in state.configuration},
        "revision": state.revision,
        "sysid": state.sysid,
    }


class ApplicationAPI(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        require_apps_manager(request.user)

    def get(self, request, organization_id, app_label=None):
        from skeleton.service_api.applications import ApplicationAdministrationService

        service = ApplicationAdministrationService()
        if app_label is None:
            return Response({"results": [state_data(item) for item in service.applications(actor=request.user)]})
        state = service.application(actor=request.user, app_label=app_label)
        return etag(Response(state_data(state)), state.revision)

    def put(self, request, organization_id, app_label):
        _manifest(app_label)
        value = request.headers.get("If-Match")
        if value is None:
            raise BoundaryError("revision_required", "Informe a revisão.", 428)
        if not re.fullmatch(r'"(?:0|[1-9][0-9]{0,18})"', value):
            raise BoundaryError("invalid_revision", "Revisão inválida.")
        data = request_fields(request)
        if set(data) != {"enabled", "configuration"}:
            raise BoundaryError("invalid_request", "Informe enabled e configuration.")
        from skeleton.api.errors import if_match_precondition
        from skeleton.service_api.applications import ApplicationAdministrationService

        service = ApplicationAdministrationService()
        # The service lists available apps and enabled ones the platform withdrew (#24).
        state = next((item for item in service.applications(actor=request.user) if item.app_label == app_label), None)
        if state is None or (not state.available and data["enabled"] is not False):
            # Only closing an app the platform withdrew is accepted; the rest is unavailable.
            raise BoundaryError("app_unavailable", "Aplicativo indisponível para esta organização.", 400)
        configuration = data["configuration"]
        with if_match_precondition():
            state = service.configure(
                actor=request.user,
                app_label=app_label,
                enabled=data["enabled"],
                configuration=tuple(configuration.items()) if isinstance(configuration, dict) else configuration,
                expected_revision=int(value[1:-1]),
            )
        return etag(Response(state_data(state)), state.revision)


@tenant_page(capability=_apps_capability)
@require_safe
def application_list(request, organization_id):
    states = application_states()
    return render(
        request,
        "skeleton/web/applications.html",
        {
            "organization_id": organization_id,
            "parameter_panel": True,
            "applications": [states[app.app_label] for app in all_apps() if visible(states[app.app_label])],
        },
    )


@tenant_page(capability=_apps_capability)
@require_safe
def application_admin(request, organization_id, app_label):
    manifest = _visible_manifest(app_label)
    state = application_state(app_label)
    return render(
        request,
        "skeleton/web/application_admin.html",
        {
            "organization_id": organization_id,
            "application": application_identity(manifest),
            "parameter_panel": True,
            "state": state,
            "sections": screen_sections(manifest, user=request.user, scope="organization"),
        },
    )


@tenant_page(capability=_apps_capability)
@require_safe
def application_admin_screen(request, organization_id, app_label, screen_id):
    _visible_manifest(app_label)
    manifest, screen = resolve_application_screen(app_label, screen_id, user=request.user, scope="organization")
    return redirect(tenant_screen_url(manifest, screen, organization_id))


@tenant_page(capability=_apps_capability)
@require_http_methods(["GET", "POST"])
def application_configuration(request, organization_id, app_label):
    manifest = _visible_manifest(app_label)
    state = application_state(app_label)
    fields = {
        "enabled": forms.BooleanField(required=False, label="Aplicativo ativo"),
        "revision": forms.IntegerField(min_value=0, widget=forms.HiddenInput),
    }
    for field in manifest.configuration_fields:
        options = {"label": field.label or field.name.replace("_", " ").capitalize(), "help_text": field.help_text}
        if field.value_type is str:
            fields[field.name] = forms.CharField(max_length=field.max_length, required=False, **options)
        elif field.value_type is bool:
            fields[field.name] = forms.BooleanField(required=False, **options)
        else:
            fields[field.name] = forms.IntegerField(**options)
    dependencies = [application_state(label) for label in manifest.dependencies]
    fields["enabled"].help_text = "Desativar preserva os registros e a configuração. " + (
        "Dependências: "
        + ", ".join(f"{item['label']} ({'ativa' if item['enabled'] else 'desativada'})" for item in dependencies)
        + "."
        if dependencies
        else "Sem dependências."
    )
    form_type = type("ApplicationConfigurationForm", (forms.Form,), fields)
    form = form_type(
        request.POST if request.method == "POST" else None,
        initial={"enabled": state["enabled"], "revision": state["revision"], **state["configuration"]},
    )
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, "Campo não permitido.")
            status = 400
        elif form.is_valid():
            data = dict(form.cleaned_data)
            enabled, revision = data.pop("enabled"), data.pop("revision")
            try:
                configure_application(
                    app_label, user=request.user, enabled=enabled, configuration=data, revision=revision
                )
                return redirect("application-admin", organization_id=organization_id, app_label=app_label)
            except ConcurrentUpdate:
                form.add_error(None, "A configuração mudou. Atualize a página.")
                status = 409
            except ValidationError as exc:
                codes = {item.code for items in getattr(exc, "error_dict", {}).values() for item in items}
                form.add_error(
                    None,
                    "O aplicativo não está disponível para esta organização: só é possível desativá-lo."
                    if "app_unavailable" in codes
                    else "Verifique os campos e as dependências ativas.",
                )
                status = 400
        else:
            status = 400

    def section(identifier, label, names):
        return {
            "id": identifier,
            "label": label,
            "columns": 1,
            "tab_id": None,
            "fields": [
                {
                    "bound": form[name],
                    "label": form[name].label,
                    "help_text": form[name].help_text,
                    "read_only": False,
                    "col_span": 12,
                }
                for name in names
            ],
        }

    sections = [section("application-activation", "Ativação", ["enabled"])]
    for group in sorted(manifest.administration.configuration_sections, key=lambda item: (item.order, item.id)):
        names = [field.name for field in manifest.configuration_fields if field.section_id == group.id]
        if names:
            sections.append(section(group.id, group.label, names))
    unassigned = [field.name for field in manifest.configuration_fields if field.section_id is None]
    if unassigned:
        sections.append(section("application-configuration", "Configuração", unassigned))
    from django.urls import reverse

    return render(
        request,
        "skeleton/web/form.html",
        {
            "title": f"{manifest.label} — Configuração",
            "form": form,
            "sections": sections,
            "organization_id": organization_id,
            "parameter_panel": True,
            "breadcrumbs": [
                {"label": "Apps", "url": reverse("application-list", kwargs={"organization_id": organization_id})},
                {
                    "label": manifest.label,
                    "url": reverse(
                        "application-admin", kwargs={"organization_id": organization_id, "app_label": app_label}
                    ),
                },
                {"label": "Configuração"},
            ],
            "configuration_dependencies": manifest.dependencies,
        },
        status=status,
    )
