"""HTML administration of the organization's apps (/o/<org>/apps/), over the service API (#338)."""

from django import forms
from django.contrib import messages
from django.shortcuts import redirect, render
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.access.capabilities import APPS_MANAGE
from skeleton.service_api import errors as service_errors
from skeleton.service_api.applications import ApplicationAdministrationService
from skeleton.system.administration import (
    application_identity,
    require_apps_manager,
    resolve_application_screen,
    screen_sections,
    tenant_screen_url,
)
from skeleton.system.app_registry import get_app
from skeleton.web.views import tenant_page


def _apps_capability(request, kwargs):
    require_apps_manager(request.user)
    return APPS_MANAGE


def _dependency_states(service, actor, manifest):
    """(label, enabled) of each dependency; one the organization cannot see reads as disabled."""
    visible = {state.app_label: state for state in service.applications(actor=actor)}
    result = []
    for label in manifest.dependencies:
        state = visible.get(label)
        result.append((state.label, state.enabled) if state else (get_app(label).label, False))
    return result


@tenant_page(capability=_apps_capability)
@require_safe
def application_list(request, organization_id):
    states = ApplicationAdministrationService().applications(actor=request.user)
    return render(
        request,
        "skeleton/web/applications.html",
        {
            "organization_id": organization_id,
            "parameter_panel": True,
            "applications": list(states),  # Available apps and enabled ones the platform withdrew.
        },
    )


@tenant_page(capability=_apps_capability)
@require_safe
def application_admin(request, organization_id, app_label):
    state = ApplicationAdministrationService().application(actor=request.user, app_label=app_label)
    manifest = get_app(app_label)
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
    ApplicationAdministrationService().application(actor=request.user, app_label=app_label)  # 404 if hidden.
    manifest, screen = resolve_application_screen(app_label, screen_id, user=request.user, scope="organization")
    return redirect(tenant_screen_url(manifest, screen, organization_id))


@tenant_page(capability=_apps_capability)
@require_http_methods(["GET", "POST"])
def application_configuration(request, organization_id, app_label):
    service = ApplicationAdministrationService()
    state = service.application(actor=request.user, app_label=app_label)  # NotFound: 404, as before.
    manifest = get_app(app_label)
    fields = {
        "enabled": forms.BooleanField(required=False, label=gettext("Aplicativo ativo")),
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
    dependencies = _dependency_states(service, request.user, manifest)
    fields["enabled"].help_text = gettext("Desativar preserva os registros e a configuração. ") + (
        gettext("Dependências: ")
        + ", ".join(f"{label} ({'ativa' if enabled else 'desativada'})" for label, enabled in dependencies)
        + "."
        if dependencies
        else gettext("Sem dependências.")
    )
    form_type = type("ApplicationConfigurationForm", (forms.Form,), fields)
    form = form_type(
        request.POST if request.method == "POST" else None,
        initial={
            "enabled": state.enabled,
            "revision": state.revision,
            **{item.name: item.value for item in state.configuration},
        },
    )
    status = 200
    incompatible = not state.configuration_valid  # #251
    if incompatible:
        messages.error(
            request,
            gettext(
                "A configuração gravada desta app é incompatível com a versão instalada. Ela fica fechada nesta "
                "organização até o operador aplicar a migration de dados; nada pode ser alterado por aqui."
            ),
        )
    if request.method == "POST":
        if incompatible:
            status = 400
        elif set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            data = dict(form.cleaned_data)
            enabled, revision = data.pop("enabled"), data.pop("revision")
            try:
                service.configure(
                    actor=request.user,
                    app_label=app_label,
                    enabled=enabled,
                    configuration=tuple(data.items()),
                    expected_revision=revision,
                )
                return redirect("application-admin", organization_id=organization_id, app_label=app_label)
            except service_errors.Conflict as exc:
                if exc.code != "stale_revision":
                    raise
                form.add_error(None, gettext("A configuração mudou. Atualize a página."))
                status = 409
            except service_errors.InvalidInput as exc:
                # The service's own messages: dependency, availability or value (#338).
                for messages_ in exc.errors.values():
                    for message in messages_:
                        form.add_error(None, message)
                if not exc.errors:
                    form.add_error(None, gettext("Verifique os campos e as dependências ativas."))
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

    sections = [section("application-activation", gettext("Ativação"), ["enabled"])]
    for group in sorted(manifest.administration.configuration_sections, key=lambda item: (item.order, item.id)):
        names = [field.name for field in manifest.configuration_fields if field.section_id == group.id]
        if names:
            sections.append(section(group.id, group.label, names))
    unassigned = [field.name for field in manifest.configuration_fields if field.section_id is None]
    if unassigned:
        sections.append(section("application-configuration", gettext("Configuração"), unassigned))
    from django.urls import reverse

    return render(
        request,
        "skeleton/web/form.html",
        {
            "title": gettext("%(label)s — Configuração") % {"label": manifest.label},
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
                {"label": gettext("Configuração")},
            ],
            "configuration_dependencies": manifest.dependencies,
        },
        status=status,
    )
