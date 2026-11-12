"""Platform administration views. Every view is wrapped by ``platform_page``.

Identity changes go through ``skeleton.accounts.services`` (CAS, lifecycle guards,
``auth_version``), which also require the platform actor; catalog edits only touch
label/description. Creating, editing or removing an identity and creating or editing an
organization require a recent authentication on POST (#226); catalog edits do not. Tenant data is never
read here, except ``organization_overview`` (member count and administrator emails, read
only): platform administration does not grant organization access.
"""

from pathlib import Path

from django.apps import apps
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.access.administration import designate_organization_admin, organization_overview
from skeleton.access.services import LastAdministrator
from skeleton.accounts.platform import LastPlatformAdministrator
from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.accounts.services import (
    PLATFORM_USER_FIELDS,
    create_identity,
    deactivate_user,
    update_platform_user,
)
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.tenancy.services import create_organization, update_organization
from skeleton.web.views import form_error, pagination, service_pagination

from .forms import (
    AppAvailabilityForm,
    AppGrantForm,
    AppRevokeForm,
    CatalogLabelForm,
    OrganizationAdministratorForm,
    OrganizationCreateForm,
    OrganizationForm,
    PlatformUserCreateForm,
    PlatformUserForm,
    RevisionForm,
)
from .guards import platform_page

__all__ = [
    "app_availability",
    "app_detail",
    "app_grant",
    "app_revoke",
    "app_documentation",
    "app_list",
    "app_screen",
    "audit",
    "catalog",
    "catalog_field",
    "catalog_table",
    "domain_revoke",
    "domains",
    "organization_administrators",
    "organization_create",
    "organization_detail",
    "organizations",
    "overview",
    "user_create",
    "user_delete",
    "user_role_revoke",
    "user_roles",
    "user_detail",
    "users",
]


def _model(label):
    return apps.get_model(label)


def _search(request):
    query = request.GET.get("q", "").strip()[:200]
    return query, ({"q": query} if query else {})


# --------------------------------------------------------------------------- overview


@platform_page
@require_safe
def overview(request):
    from skeleton.system.app_registry import all_apps

    from .updates import running_core

    User = _model("sk_accounts.User")
    Organization = _model("sk_tenancy.Organization")
    TableDefinition = _model("sk_system.TableDefinition")
    DeploymentOperation = _model("sk_system.DeploymentOperation")
    return render(
        request,
        "skeleton/web/platform/overview.html",
        {
            "platform_section": "overview",
            "core": running_core(),
            "stats": [
                (
                    gettext("Usuários ativos"),
                    User.objects.filter(is_active=True, account_type="human").count(),
                    reverse("platform:users"),
                ),
                (
                    gettext("Organizações ativas"),
                    Organization.objects.filter(is_active=True).count(),
                    reverse("platform:organizations"),
                ),
                (gettext("Aplicativos instalados"), len(list(all_apps())), reverse("platform:apps")),
                (
                    gettext("Tabelas catalogadas"),
                    TableDefinition.objects.filter(available=True).count(),
                    reverse("platform:catalog"),
                ),
            ],
            "operations": DeploymentOperation.objects.order_by("-created_at")[:5],
        },
    )


# --------------------------------------------------------------------------- users


@platform_page
@require_safe
def users(request):
    query, filters = _search(request)
    # Service accounts belong to their organization, outside platform lists and limits (#44).
    queryset = _model("sk_accounts.User").objects.filter(account_type="human").order_by("email")
    if query:
        queryset = queryset.filter(Q(email__icontains=query) | Q(first_name__icontains=query))
    return render(
        request,
        "skeleton/web/platform/users.html",
        {"platform_section": "users", "rows": pagination(request, queryset, validated_filters=filters), "q": query},
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def user_create(request):
    form = PlatformUserCreateForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                with transaction.atomic():
                    user = create_identity(
                        email=data["email"],
                        password=data["password1"],
                        first_name=data["first_name"],
                        last_name=data["last_name"],
                    )
            except ValidationError as exc:
                status = form_error(form, exc)
            except IntegrityError:
                form.add_error("email", gettext("Já existe uma identidade com este email."))
                status = 409
            else:
                messages.success(request, gettext("Identidade %(email)s criada.") % {"email": user.email})
                return redirect("platform:user", sysid=user.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "users",
            "form": form,
            "title": gettext("Nova identidade"),
            "eyebrow": gettext("Usuários"),
            "description": gettext("A identidade não recebe vínculo nem permissão em nenhuma organização."),
            "submit_label": gettext("Criar identidade"),
            "cancel_url": reverse("platform:users"),
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def user_detail(request, sysid):
    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    initial = {name: getattr(user, name) for name in PLATFORM_USER_FIELDS}
    form = PlatformUserForm(request.POST or None, initial={**initial, "expected_revision": user.revision})
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            revision = data.pop("expected_revision")
            try:
                with transaction.atomic():
                    user = update_platform_user(user, actor=request.user, data=data, expected_revision=revision)
            except (ValidationError, ConcurrentUpdate, LastAdministrator) as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Identidade atualizada."))
                return redirect("platform:user", sysid=user.pk)
        else:
            status = 400
    profile = _model("sk_accounts.UserProfile").objects.filter(user_id=user.pk).first()
    roles = _model("sk_accounts.PlatformRoleAssignment").objects.filter(user=user).order_by("role")
    return render(
        request,
        "skeleton/web/platform/user.html",
        {"platform_section": "users", "subject": user, "profile": profile, "form": form, "platform_roles": roles},
        status=status,
    )


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def user_roles(request, sysid):
    """Grant a limited platform role (#34); only the platform administrator."""
    from skeleton.accounts.platform import grant_platform_role

    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    try:
        with transaction.atomic():
            grant_platform_role(actor=request.user, user=user, role=request.POST.get("role", ""))
    except ValidationError as exc:
        messages.error(request, " ".join(exc.messages))
        return redirect("platform:user", sysid=user.pk)
    messages.success(request, "Papel de plataforma concedido.")
    return redirect("platform:user", sysid=user.pk)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def user_role_revoke(request, sysid, assignment):
    from skeleton.accounts.platform import revoke_platform_role

    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    if not _model("sk_accounts.PlatformRoleAssignment").objects.filter(pk=assignment, user=user).exists():
        raise Http404
    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = None
    with transaction.atomic():
        revoke_platform_role(actor=request.user, assignment_id=assignment, expected_revision=revision)
    messages.success(request, "Papel de plataforma revogado.")
    return redirect("platform:user", sysid=user.pk)


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def user_delete(request, sysid):
    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    form = RevisionForm(
        request.POST if request.method == "POST" else None, initial={"expected_revision": user.revision}
    )
    status = 200
    error = None
    if request.method == "POST":
        if not form.is_valid():
            error = gettext("Formulário inválido. Atualize a página e tente novamente.")
            status = 400
        else:
            try:
                with transaction.atomic():
                    deactivate_user(
                        user,
                        actor=request.user,
                        delete=True,
                        expected_revision=form.cleaned_data["expected_revision"],
                    )
            except ConcurrentUpdate:
                error = gettext("A identidade mudou. Atualize a página antes de tentar novamente.")
                status = 409
            except LastPlatformAdministrator:
                error = gettext("A remoção deixaria a plataforma sem administrador ativo.")
                status = 409
            except LastAdministrator:
                error = gettext("A remoção deixaria uma organização sem administrador ativo.")
                status = 409
            else:
                messages.success(
                    request,
                    gettext("Identidade %(email)s removida. Perfil e vínculos históricos foram mantidos.")
                    % {"email": user.email},
                )
                return redirect("platform:users")
    return render(
        request,
        "skeleton/web/platform/confirm.html",
        {
            "platform_section": "users",
            "title": gettext("Remover identidade"),
            "eyebrow": gettext("Usuários"),
            "subject_label": user.email,
            "message": gettext(
                "A identidade é desativada e marcada como removida; sessões são revogadas. "
                "Perfil, vínculos e auditoria são preservados. Não há exclusão física."
            ),
            "error": error,
            "submit_label": gettext("Remover identidade"),
            "cancel_url": reverse("platform:user", kwargs={"sysid": user.pk}),
            "form": form,
        },
        status=status,
    )


# --------------------------------------------------------------------------- organizations


@platform_page
@require_safe
def organizations(request):
    query, filters = _search(request)
    queryset = _model("sk_tenancy.Organization").objects.order_by("name")
    if query:
        queryset = queryset.filter(Q(name__icontains=query) | Q(slug__icontains=query))
    return render(
        request,
        "skeleton/web/platform/organizations.html",
        {
            "platform_section": "organizations",
            "rows": pagination(request, queryset, validated_filters=filters),
            "q": query,
        },
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_create(request):
    form = OrganizationCreateForm(request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = form.cleaned_data
            try:
                with transaction.atomic():
                    organization = create_organization(owner=data["owner"], slug=data["slug"], name=data["name"])
            except ValidationError as exc:
                status = form_error(form, exc)
            except IntegrityError:
                form.add_error("slug", gettext("Já existe uma organização com este identificador."))
                status = 409
            else:
                messages.success(request, gettext("Organização %(name)s criada.") % {"name": organization.name})
                return redirect("platform:organization", sysid=organization.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "organizations",
            "form": form,
            "title": gettext("Nova organização"),
            "eyebrow": gettext("Organizações"),
            "description": gettext("Cria a organização, os papéis padrão e o vínculo do administrador inicial."),
            "submit_label": gettext("Criar organização"),
            "cancel_url": reverse("platform:organizations"),
        },
        status=status,
    )


def _organization_form(organization, data=None):
    return OrganizationForm(
        data,
        initial={
            "name": organization.name,
            "is_active": organization.is_active,
            "expected_revision": organization.revision,
        },
    )


def _organization_page(request, organization, *, form=None, admin_form=None, quota_form=None, status=200):
    from skeleton.accounts.identity_providers import organization_providers
    from skeleton.files.quota import platform_usage

    # One organization per request: the overview opens a read-only system context for it.
    overview = organization_overview(actor=request.user, organization=organization)
    storage = platform_usage(actor=request.user, organization=organization)
    if admin_form is None and organization.is_active:
        admin_form = OrganizationAdministratorForm(
            administrators=overview.administrators,
            initial={"expected_version": overview.administrators_version},
        )
    return render(
        request,
        "skeleton/web/platform/organization.html",
        {
            "platform_section": "organizations",
            "subject": organization,
            "form": form or _organization_form(organization),
            "overview": overview,
            "admin_form": admin_form,
            "storage": storage,
            "quota_form": quota_form or _quota_form(organization),
            "identity_providers": organization_providers(organization),
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_detail(request, sysid):
    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid)
    form = _organization_form(organization, request.POST or None)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            revision = data.pop("expected_revision")
            try:
                with transaction.atomic():
                    organization = update_organization(
                        organization, actor=request.user, data=data, expected_revision=revision
                    )
            except (ValidationError, ConcurrentUpdate, IntegrityError) as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Organização atualizada."))
                return redirect("platform:organization", sysid=organization.pk)
        else:
            status = 400
    return _organization_page(request, organization, form=form, status=status)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def organization_administrators(request, sysid):
    """Designate or replace an organization administrator; never reads the organization's data."""
    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid, is_active=True)
    overview = organization_overview(actor=request.user, organization=organization)
    admin_form = OrganizationAdministratorForm(request.POST, administrators=overview.administrators)
    if not admin_form.is_valid():
        return _organization_page(request, organization, admin_form=admin_form, status=400)
    data = admin_form.cleaned_data
    try:
        with transaction.atomic():
            designate_organization_admin(
                actor=request.user,
                organization=organization,
                user=data["email"],
                replace=data["replace"],
                expected_version=data["expected_version"],
            )
    except PermissionDenied:
        admin_form.add_error(
            "email", gettext("A administração da plataforma não designa a si mesma para uma organização.")
        )
        return _organization_page(request, organization, admin_form=admin_form, status=403)
    except (ValidationError, ConcurrentUpdate, LastAdministrator) as exc:
        status = form_error(admin_form, exc)
        return _organization_page(request, organization, admin_form=admin_form, status=status)
    if data["replace"] is not None:
        messages.success(
            request,
            gettext("%(email)s substituiu %(email_)s na administração.")
            % {"email": data["email"].email, "email_": data["replace"].email},
        )
    else:
        messages.success(request, gettext("%(email)s agora administra a organização.") % {"email": data["email"].email})
    return redirect("platform:organization", sysid=organization.pk)


# --------------------------------------------------------------------------- catalog


def _update_labels(model, pk, data, *, expected_revision):
    with transaction.atomic():
        record = model.objects.select_for_update().get(pk=pk)
        if expected_revision != record.revision:
            raise ConcurrentUpdate("The catalog form is stale.")
        changed = {name for name in ("label", "description") if getattr(record, name) != data[name]}
        if changed:
            for name in changed:
                setattr(record, name, data[name])
            record.save(update_fields=changed)
        return record


def _label_form(request, record):
    return CatalogLabelForm(
        request.POST or None,
        initial={"label": record.label, "description": record.description, "expected_revision": record.revision},
    )


@platform_page(capability="inventory.read")
@require_safe
def catalog(request):
    query, filters = _search(request)
    queryset = _model("sk_system.TableDefinition").objects.order_by("db_table")
    if query:
        queryset = queryset.filter(
            Q(db_table__icontains=query) | Q(label__icontains=query) | Q(model_label__icontains=query)
        )
    return render(
        request,
        "skeleton/web/platform/catalog.html",
        {"platform_section": "catalog", "rows": pagination(request, queryset, validated_filters=filters), "q": query},
    )


@platform_page(capability="inventory.read")
@require_http_methods(["GET", "POST"])
@transaction.atomic
def catalog_table(request, sysid):
    TableDefinition = _model("sk_system.TableDefinition")
    table = get_object_or_404(TableDefinition.objects, pk=sysid)
    form = _label_form(request, table)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            revision = data.pop("expected_revision")
            try:
                table = _update_labels(TableDefinition, table.pk, data, expected_revision=revision)
            except ConcurrentUpdate as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Rótulo da tabela atualizado."))
                return redirect("platform:catalog-table", sysid=table.pk)
        else:
            status = 400
    fields = _model("sk_system.FieldDefinition").objects.filter(table=table).order_by("name")
    return render(
        request,
        "skeleton/web/platform/catalog_table.html",
        {"platform_section": "catalog", "table": table, "fields": fields, "form": form},
        status=status,
    )


@platform_page(capability="inventory.read")
@require_http_methods(["GET", "POST"])
@transaction.atomic
def catalog_field(request, sysid):
    FieldDefinition = _model("sk_system.FieldDefinition")
    field = get_object_or_404(FieldDefinition.objects.select_related("table"), pk=sysid)
    form = _label_form(request, field)
    status = 200
    table_url = reverse("platform:catalog-table", kwargs={"sysid": field.table_id})
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            revision = data.pop("expected_revision")
            try:
                _update_labels(FieldDefinition, field.pk, data, expected_revision=revision)
            except ConcurrentUpdate as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Rótulo do campo %(name)s atualizado.") % {"name": field.name})
                return redirect(table_url)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "catalog",
            "form": form,
            "title": gettext("Campo %(name)s") % {"name": field.name},
            "eyebrow": gettext("Catálogo · %(label)s") % {"label": field.table.label},
            "description": gettext(
                "Somente rótulo e descrição são editáveis; tipo, coluna e sensibilidade vêm do código."
            ),
            "submit_label": gettext("Salvar"),
            "cancel_url": table_url,
        },
        status=status,
    )


# --------------------------------------------------------------------------- audit


@platform_page(capability="audit.read")
@require_safe
def audit(request):
    from skeleton.accounts.platform import is_platform_admin
    from skeleton.api.serializers import audit_data
    from skeleton.audit.filters import operation_choices, parse_audit_filters
    from skeleton.service_api.audit import AuditLogService
    from skeleton.tenancy.context import platform_audit_scope

    try:
        filters = parse_audit_filters(request.GET)
    except ValidationError:
        return render(
            request, "skeleton/web/error.html", {"detail": gettext("Verifique os filtros informados.")}, status=400
        )

    def page(**page):
        if is_platform_admin(request.user):
            return AuditLogService().events(actor=request.user, **filters, **page)
        with platform_audit_scope(request.user):  # The auditor's GUC, only for this read (#34).
            return AuditLogService().events(actor=request.user, **filters, **page)

    rows = service_pagination(
        request,
        page,
        validated_filters={name: value for name, value in request.GET.dict().items() if value != ""},
    )
    rows.object_list = [audit_data(event) for event in rows]
    return render(
        request,
        "skeleton/web/platform/audit.html",
        {
            "platform_section": "audit",
            "rows": rows,
            "filters": request.GET,
            "operations": operation_choices(),
        },
    )


# --------------------------------------------------------------------------- applications


def _manifest(app_label):
    from skeleton.system.administration import application_manifest

    return application_manifest(app_label)


@platform_page
@require_safe
def app_list(request):
    from skeleton.system.administration import application_identity
    from skeleton.system.app_registry import all_apps

    applications = sorted((application_identity(manifest) for manifest in all_apps()), key=lambda app: app["label"])
    return render(
        request, "skeleton/web/platform/apps.html", {"platform_section": "apps", "applications": applications}
    )


@platform_page
@require_safe
def app_detail(request, app_label):
    from skeleton.system.administration import application_identity, screen_sections
    from skeleton.system.availability import availability_summary

    manifest = _manifest(app_label)
    return render(
        request,
        "skeleton/web/platform/app.html",
        {
            "platform_section": "apps",
            "application": application_identity(manifest),
            "availability": availability_summary(manifest.app_label, user=request.user),
            "has_documentation": bool(manifest.documentation_root),
            "sections": screen_sections(manifest, user=request.user, scope="platform"),
            "tenant_screens": sorted(
                (screen for screen in manifest.administration.screens if screen.scope == "organization"),
                key=lambda item: (item.order, item.id),
            ),
        },
    )


def _app_labels(labels):
    from skeleton.system.app_registry import all_apps

    names = {manifest.app_label: manifest.label for manifest in all_apps()}
    return [{"app_label": label, "label": names.get(label, label)} for label in labels]


def _availability_page(request, manifest, *, mode_form=None, grant_form=None, revoke_error=None, status=200):
    """Availability of one app (#23): global rows only; no organization's activation is read here."""
    from skeleton.system.administration import application_identity
    from skeleton.system.availability import availability_summary

    summary = availability_summary(manifest.app_label, user=request.user)
    return render(
        request,
        "skeleton/web/platform/app_availability.html",
        {
            "platform_section": "apps",
            "application": application_identity(manifest),
            "availability": summary,
            "requires": _app_labels(summary.requires),
            "required_by": _app_labels(summary.required_by),
            "mode_form": mode_form
            or AppAvailabilityForm(initial={"mode": summary.mode or "selected", "expected_revision": summary.revision}),
            "grant_form": grant_form or AppGrantForm(granted_ids=summary.granted_ids),
            "revoke_error": revoke_error,
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def app_availability(request, app_label):
    """Show the gate; POST changes the mode with CAS on the availability row."""
    from skeleton.system.availability import set_app_availability

    manifest = _manifest(app_label)
    if request.method != "POST":
        return _availability_page(request, manifest)
    form = AppAvailabilityForm(request.POST)
    if not form.is_valid():
        return _availability_page(request, manifest, mode_form=form, status=400)
    try:
        with transaction.atomic():
            set_app_availability(
                manifest.app_label,
                user=request.user,
                mode=form.cleaned_data["mode"],
                revision=form.cleaned_data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _availability_page(request, manifest, mode_form=form, status=status)
    messages.success(request, "Disponibilidade atualizada.")
    return redirect("platform:app-availability", app_label=manifest.app_label)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def app_grant(request, app_label):
    """Admit one organization to the app; it still decides activation itself."""
    from skeleton.system.availability import availability_summary, grant_app

    manifest = _manifest(app_label)
    granted = availability_summary(manifest.app_label, user=request.user).granted_ids
    form = AppGrantForm(request.POST, granted_ids=granted)
    if not form.is_valid():
        return _availability_page(request, manifest, grant_form=form, status=400)
    organization = form.cleaned_data["organization"]
    try:
        with transaction.atomic():
            grant_app(manifest.app_label, user=request.user, organization_id=organization.pk)
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _availability_page(request, manifest, grant_form=form, status=status)
    messages.success(request, f"{organization.name} pode usar {manifest.label}.")
    return redirect("platform:app-availability", app_label=manifest.app_label)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def app_revoke(request, app_label):
    """Close the gate for one organization; its activation and configuration are kept (#21)."""
    from skeleton.system.availability import revoke_app

    manifest = _manifest(app_label)
    form = AppRevokeForm(request.POST)
    if not form.is_valid():
        return _availability_page(request, manifest, revoke_error="Pedido de revogação inválido.", status=400)
    try:
        with transaction.atomic():
            revoke_app(
                manifest.app_label,
                user=request.user,
                organization_id=form.cleaned_data["organization"],
                expected_revision=form.cleaned_data["expected_revision"],
            )
    except ConcurrentUpdate:
        detail = "A concessão mudou desde que a página foi aberta. Revise antes de revogar."
        return _availability_page(request, manifest, revoke_error=detail, status=409)
    except ValidationError as exc:
        return _availability_page(request, manifest, revoke_error=" ".join(exc.messages), status=400)
    messages.success(request, "Concessão revogada. A escolha de ativação da organização foi preservada.")
    return redirect("platform:app-availability", app_label=manifest.app_label)


@platform_page
@require_safe
def app_documentation(request, app_label):
    from skeleton.system.administration import application_identity

    manifest = _manifest(app_label)
    if not manifest.documentation_root:
        raise Http404
    root = Path(manifest.documentation_root).resolve()
    documents = []
    for filename in ("spec.md", "adr.md", "service-api.md"):
        source = root / filename
        if filename == "service-api.md" and not source.is_file():
            continue  # Historical apps predate it; check E012 reports it for apps that declare a service API.
        if not source.resolve().is_relative_to(root) or not source.is_file():
            raise Http404
        documents.append({"name": filename, "text": source.read_text(encoding="utf-8")})
    return render(
        request,
        "skeleton/web/platform/app_documentation.html",
        {"platform_section": "apps", "application": application_identity(manifest), "documents": documents},
    )


@platform_page
def app_screen(request, app_label, screen_id):
    from skeleton.system.administration import resolve_application_screen
    from skeleton.system.presentation import get_admin_handler

    manifest, screen = resolve_application_screen(app_label, screen_id, user=request.user, scope="platform")
    if screen.kind != "custom":
        raise Http404
    try:
        handler = get_admin_handler(manifest.app_label, screen.handler_id)
    except KeyError:
        raise Http404 from None
    if screen.sensitive:
        from .updates import recent_authentication

        @recent_authentication_required(allow_get=True)
        def sensitive_handler(request):
            recent_authentication(request)
            return handler(request)

        return sensitive_handler(request)
    return handler(request)


# --------------------------------------------------------------------------- storage quota


def _quota_form(organization, data=None):
    from skeleton.files.models import StorageQuota

    from .forms import StorageQuotaForm

    row = StorageQuota.objects.filter(quota_organization_id=organization.pk).first()
    if row is None:
        initial = {"mode": "default"}
    elif row.quota_bytes is None:
        initial = {"mode": "unlimited", "expected_revision": row.revision}
    else:
        initial = {
            "mode": "limited",
            "quota_mib": row.quota_bytes // StorageQuotaForm.MiB,
            "expected_revision": row.revision,
        }
    return StorageQuotaForm(data, initial=initial)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def organization_storage(request, sysid):
    """Set the organization's storage quota; never reads its attachments."""
    from skeleton.files.quota import clear_quota, set_quota

    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid)
    form = _quota_form(organization, request.POST)
    if not form.is_valid():
        return _organization_page(request, organization, quota_form=form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            if data["mode"] == "default":
                if data["expected_revision"] is not None:
                    clear_quota(organization, expected_revision=data["expected_revision"])
            else:
                set_quota(organization, quota_bytes=form.quota_bytes(), expected_revision=data["expected_revision"])
    except (ConcurrentUpdate, IntegrityError):
        form.add_error(None, gettext("A cota foi alterada por outra operação. Revise e salve de novo."))
        return _organization_page(request, organization, quota_form=form, status=409)
    messages.success(request, gettext("Cota de armazenamento atualizada."))
    return redirect("platform:organization", sysid=organization.pk)


# --------------------------------------------------------------------------- verified domains (#28)


def _domains_page(request, *, status=200, error=None):
    from skeleton.tenancy.domains import claims

    return render(
        request,
        "skeleton/web/platform/domains.html",
        {"platform_section": "domains", "rows": list(claims()), "error": error},
        status=status,
    )


@platform_page
@require_safe
def domains(request):
    return _domains_page(request)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def domain_revoke(request, sysid):
    """Settle a disputed domain: drop the global claim; the organization's own row is untouched."""
    from skeleton.tenancy.domains import DomainUnavailable, revoke_claim

    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = None
    try:
        with transaction.atomic():
            claim = revoke_claim(actor=request.user, claim_id=sysid, expected_revision=revision)
    except DomainUnavailable:
        raise Http404 from None
    except ConcurrentUpdate:
        return _domains_page(request, status=409, error="A posse mudou. Atualize a página.")
    messages.success(request, f"Posse de {claim.domain} revogada.")
    return redirect("platform:domains")
