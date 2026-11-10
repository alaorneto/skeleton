"""Platform administration views. Every view is wrapped by ``platform_page``.

Identity changes go through ``skeleton.accounts.services`` (CAS, lifecycle guards,
``auth_version``); catalog edits only touch label/description. Tenant data is never
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
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.access.services import LastAdministrator
from skeleton.accounts.platform import LastPlatformAdministrator
from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.accounts.services import (
    PLATFORM_USER_FIELDS,
    create_identity,
    create_organization,
    deactivate_user,
    designate_organization_admin,
    organization_overview,
    update_organization,
    update_platform_user,
)
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.web.views import form_error, pagination

from .forms import (
    CatalogLabelForm,
    OrganizationAdministratorForm,
    OrganizationCreateForm,
    OrganizationForm,
    PlatformUserCreateForm,
    PlatformUserForm,
)
from .guards import platform_page

__all__ = [
    "app_detail",
    "app_documentation",
    "app_list",
    "app_screen",
    "audit",
    "catalog",
    "catalog_field",
    "catalog_table",
    "organization_administrators",
    "organization_create",
    "organization_detail",
    "organizations",
    "overview",
    "user_create",
    "user_delete",
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
    from skeleton.versioning import core_version

    User = _model("sk_accounts.User")
    Organization = _model("sk_tenancy.Organization")
    TableDefinition = _model("sk_system.TableDefinition")
    DeploymentOperation = _model("sk_system.DeploymentOperation")
    return render(
        request,
        "skeleton/web/platform/overview.html",
        {
            "platform_section": "overview",
            "core": core_version(),
            "stats": [
                (
                    "Usuários ativos",
                    User.objects.filter(is_active=True, account_type="human").count(),
                    reverse("platform:users"),
                ),
                (
                    "Organizações ativas",
                    Organization.objects.filter(is_active=True).count(),
                    reverse("platform:organizations"),
                ),
                ("Aplicativos instalados", len(list(all_apps())), reverse("platform:apps")),
                (
                    "Tabelas catalogadas",
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
                form.add_error("email", "Já existe uma identidade com este email.")
                status = 409
            else:
                messages.success(request, f"Identidade {user.email} criada.")
                return redirect("platform:user", sysid=user.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "users",
            "form": form,
            "title": "Nova identidade",
            "eyebrow": "Usuários",
            "description": "A identidade não recebe vínculo nem permissão em nenhuma organização.",
            "submit_label": "Criar identidade",
            "cancel_url": reverse("platform:users"),
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
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
                    user = update_platform_user(user, data=data, expected_revision=revision)
            except (ValidationError, ConcurrentUpdate, LastAdministrator) as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, "Identidade atualizada.")
                return redirect("platform:user", sysid=user.pk)
        else:
            status = 400
    profile = _model("sk_accounts.UserProfile").objects.filter(user_id=user.pk).first()
    return render(
        request,
        "skeleton/web/platform/user.html",
        {"platform_section": "users", "subject": user, "profile": profile, "form": form},
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@transaction.atomic
def user_delete(request, sysid):
    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    status = 200
    error = None
    if request.method == "POST":
        try:
            with transaction.atomic():
                deactivate_user(user, delete=True)
        except LastPlatformAdministrator:
            error = "A remoção deixaria a plataforma sem administrador ativo."
            status = 409
        except LastAdministrator:
            error = "A remoção deixaria uma organização sem administrador ativo."
            status = 409
        else:
            messages.success(request, f"Identidade {user.email} removida. Perfil e vínculos históricos foram mantidos.")
            return redirect("platform:users")
    return render(
        request,
        "skeleton/web/platform/confirm.html",
        {
            "platform_section": "users",
            "title": "Remover identidade",
            "eyebrow": "Usuários",
            "subject_label": user.email,
            "message": "A identidade é desativada e marcada como removida; sessões são revogadas. "
            "Perfil, vínculos e auditoria são preservados. Não há exclusão física.",
            "error": error,
            "submit_label": "Remover identidade",
            "cancel_url": reverse("platform:user", kwargs={"sysid": user.pk}),
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
                form.add_error("slug", "Já existe uma organização com este identificador.")
                status = 409
            else:
                messages.success(request, f"Organização {organization.name} criada.")
                return redirect("platform:organization", sysid=organization.pk)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "organizations",
            "form": form,
            "title": "Nova organização",
            "eyebrow": "Organizações",
            "description": "Cria a organização, os papéis padrão e o vínculo do administrador inicial.",
            "submit_label": "Criar organização",
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
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
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
                    organization = update_organization(organization, data=data, expected_revision=revision)
            except (ValidationError, ConcurrentUpdate, IntegrityError) as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, "Organização atualizada.")
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
        admin_form.add_error("email", "A administração da plataforma não designa a si mesma para uma organização.")
        return _organization_page(request, organization, admin_form=admin_form, status=403)
    except (ValidationError, ConcurrentUpdate, LastAdministrator) as exc:
        status = form_error(admin_form, exc)
        return _organization_page(request, organization, admin_form=admin_form, status=status)
    if data["replace"] is not None:
        messages.success(request, f"{data['email'].email} substituiu {data['replace'].email} na administração.")
    else:
        messages.success(request, f"{data['email'].email} agora administra a organização.")
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


@platform_page
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


@platform_page
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
                messages.success(request, "Rótulo da tabela atualizado.")
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


@platform_page
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
                messages.success(request, f"Rótulo do campo {field.name} atualizado.")
                return redirect(table_url)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/form.html",
        {
            "platform_section": "catalog",
            "form": form,
            "title": f"Campo {field.name}",
            "eyebrow": f"Catálogo · {field.table.label}",
            "description": "Somente rótulo e descrição são editáveis; tipo, coluna e sensibilidade vêm do código.",
            "submit_label": "Salvar",
            "cancel_url": table_url,
        },
        status=status,
    )


# --------------------------------------------------------------------------- audit


@platform_page
@require_safe
def audit(request):
    from skeleton.api.serializers import audit_data

    events = _model("sk_audit.AuditEvent").objects.filter(organization_id__isnull=True).order_by("-created_at", "sysid")
    rows = pagination(request, events)
    rows.object_list = [audit_data(event) for event in rows]
    return render(request, "skeleton/web/platform/audit.html", {"platform_section": "audit", "rows": rows})


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

    manifest = _manifest(app_label)
    return render(
        request,
        "skeleton/web/platform/app.html",
        {
            "platform_section": "apps",
            "application": application_identity(manifest),
            "has_documentation": bool(manifest.documentation_root),
            "sections": screen_sections(manifest, user=request.user, scope="platform"),
            "tenant_screens": sorted(
                (screen for screen in manifest.administration.screens if screen.scope == "organization"),
                key=lambda item: (item.order, item.id),
            ),
        },
    )


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
        form.add_error(None, "A cota foi alterada por outra operação. Revise e salve de novo.")
        return _organization_page(request, organization, quota_form=form, status=409)
    messages.success(request, "Cota de armazenamento atualizada.")
    return redirect("platform:organization", sysid=organization.pk)
