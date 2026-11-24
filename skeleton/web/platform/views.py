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
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods, require_safe

from skeleton.access.administration import designate_organization_admin, organization_overview
from skeleton.access.services import LastAdministrator
from skeleton.accounts.platform import LastPlatformAdministrator
from skeleton.accounts.reauthentication import recent_authentication_required
from skeleton.accounts.services import (
    PLATFORM_USER_FIELDS,
    MFAResetRefused,
    create_identity,
    deactivate_user,
    mfa_methods,
    reset_mfa,
    update_platform_user,
)
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.system.catalog_labels import update_catalog_labels
from skeleton.tenancy.services import create_organization, update_organization
from skeleton.web.confirm import PLATFORM_TEMPLATE, Confirmation, confirmation_step
from skeleton.web.errors import error_response
from skeleton.web.views import form_error, pagination, service_pagination

from .forms import (
    AppAvailabilityForm,
    AppGrantForm,
    AppLimitsForm,
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
    "user_mfa_reset",
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
    from skeleton.system.releases import CLOSED_PREVIEW_STATES

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
            # Plans closed without running (#821) are history, not recent operations.
            "operations": DeploymentOperation.objects.exclude(state__in=CLOSED_PREVIEW_STATES).order_by("-created_at")[
                :5
            ],
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
        {
            "platform_section": "users",
            "subject": user,
            "profile": profile,
            "form": form,
            "platform_roles": roles,
            "mfa_methods": mfa_methods(user),
            "is_self": user.pk == request.user.pk,
        },
        status=status,
    )


MFA_METHOD_LABELS = {
    "totp": gettext_lazy("Aplicativo autenticador"),
    "recovery_codes": gettext_lazy("Códigos de recuperação"),
}


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def user_mfa_reset(request, sysid):
    """Reset the MFA of a person who lost the authenticator and the recovery codes (#736)."""
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
                    reset_mfa(user, actor=request.user, expected_revision=form.cleaned_data["expected_revision"])
            except ConcurrentUpdate:
                error = gettext("A identidade mudou. Atualize a página antes de tentar novamente.")
                status = 409
            except MFAResetRefused as exc:
                error = " ".join(exc.messages)
                status = 409
            else:
                messages.success(
                    request,
                    gettext("MFA de %(email)s redefinido. A pessoa cadastra o MFA de novo no próximo login.")
                    % {"email": user.email},
                )
                return redirect("platform:user", sysid=user.pk)
    methods = [str(MFA_METHOD_LABELS.get(method, method)) for method in mfa_methods(user)]
    return render(
        request,
        "skeleton/web/platform/confirm.html",
        {
            "platform_section": "users",
            "title": gettext("Redefinir MFA"),
            "eyebrow": gettext("Usuários"),
            "subject_label": user.email,
            "message": gettext(
                "Remove todos os métodos de MFA da pessoa (%(methods)s) e encerra todas as sessões dela. "
                "Ela entra de novo só com a senha ou o SSO e cadastra o MFA outra vez; um email a avisa da "
                "redefinição. Confira a identidade de quem pediu por outro canal antes de continuar."
            )
            % {"methods": ", ".join(methods) or gettext("nenhum")},
            "error": error,
            "submit_label": gettext("Redefinir MFA"),
            "cancel_url": reverse("platform:user", kwargs={"sysid": user.pk}),
            "form": form,
        },
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


def _describe_role_revoke(request, sysid, assignment):
    user = get_object_or_404(_model("sk_accounts.User").objects, pk=sysid, account_type="human")
    row = get_object_or_404(_model("sk_accounts.PlatformRoleAssignment").objects, pk=assignment, user=user)
    return Confirmation(
        title=gettext("Revogar papel de plataforma"),
        subject=f"{user.email} · {row.get_role_display()}",
        message=gettext("A pessoa perde o papel e as sessões dela são encerradas na hora."),
        submit_label=gettext("Revogar papel"),
        cancel_url=reverse("platform:user", kwargs={"sysid": user.pk}),
        eyebrow=gettext("Usuários"),
        hidden={"revision": row.revision},
        template=PLATFORM_TEMPLATE,
        context={"platform_section": "users"},
    )


@platform_page
@confirmation_step(_describe_role_revoke)
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
    from skeleton.tenancy import signup
    from skeleton.tenancy.models import OrganizationSignup

    by_signup = request.GET.get("signup") == "1"
    if by_signup:
        # Created by the self-service signup (#715).
        queryset = queryset.filter(pk__in=OrganizationSignup.objects.values("signup_organization_id"))
        filters = {**filters, "signup": "1"}
    return render(
        request,
        "skeleton/web/platform/organizations.html",
        {
            "platform_section": "organizations",
            "rows": pagination(request, queryset, validated_filters=filters),
            "q": query,
            "by_signup": by_signup,
            "signup_enabled": signup.enabled(),
            "signup_blocked": signup.enabled() and signup.unavailable_reason() is not None,
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
                    organization = create_organization(
                        owner=data["owner"],
                        slug=data["slug"],
                        name=data["name"],
                        settings=form.provided_settings(),
                        storage_location=data["storage_location"] if "storage_location" in form.fields else "default",
                    )
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
        organization=organization,
        initial={
            "name": organization.name,
            "slug": organization.slug,
            "is_active": organization.is_active,
            "expected_revision": organization.revision,
        },
    )


def _member_limit(request, organization):
    """The member limit of the organization now (``None``: unlimited), for the platform pages."""
    from skeleton.system.limits import MEMBERS, limit_breakdown

    return next(
        row.status.limit
        for row in limit_breakdown(actor=request.user, organization=organization)
        if row.status.metric == MEMBERS
    )


def _organization_apps(overview):
    """The apps card (#699): each installed app with what it adds to the member limit."""
    from skeleton.system.models import AppAvailability

    labels = [app.app_label for app in overview.applications]
    rows = dict(AppAvailability.objects.filter(app_label__in=labels).values_list("app_label", "limits"))
    return [
        {
            "app": app,
            "offered": app.app_label in rows,
            "members": (rows.get(app.app_label) or {}).get("members", 0),
        }
        for app in overview.applications
    ]


def _organization_page(
    request, organization, *, form=None, admin_form=None, quota_form=None, apps_error=None, status=200
):
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
            "platform_configurable": _platform_configurable(),
            "storage_location": _storage_location(organization),
            "bundle": _bundle_choices(organization),
            "site_address": _site_address(organization),
            "apps": _organization_apps(overview),
            "apps_error": apps_error,
            "member_limit": _member_limit(request, organization),
        },
        status=status,
    )


def _bundle_choices(organization):
    """The organization's bundle (#714) and the active bundles to change to."""
    from skeleton.system.bundles import bundle_of
    from skeleton.system.models import AppBundle

    return {
        "current": bundle_of(organization.pk),
        "choices": list(AppBundle.objects.filter(is_active=True).order_by("position", "label")),
    }


def _storage_location(organization):
    """The organization's storage location row (#705) and the active locations to choose from."""
    from skeleton.files.models import OrganizationStorage, StorageLocation

    row = OrganizationStorage.objects.select_related("location").filter(storage_organization=organization).first()
    return {
        "current": row.location if row is not None else None,
        "choices": list(StorageLocation.objects.filter(is_active=True).order_by("label")),
    }


def _platform_configurable():
    """Labels of the installed apps that declare fields only the platform writes (#702)."""
    from skeleton.system.app_registry import all_apps
    from skeleton.system.applications import platform_fields

    return {manifest.app_label for manifest in all_apps() if platform_fields(manifest)}


def _site_address(organization):
    """The organization's own host for the "Endereço" row (#478): ``None`` hides it, ``""`` is unavailable."""
    from urllib.parse import urlsplit

    from skeleton.tenancy.site import base_domain, organization_origin

    if not base_domain():
        return None
    origin = organization_origin(organization.slug)
    return urlsplit(origin).netloc if origin else ""


@platform_page
@require_safe
def organization_slug(request):
    """Whether the typed identifier is free (#765); ``?organization=`` names the one being edited."""
    import uuid

    from skeleton.web.slug_check import slug_check_response

    try:
        organization_id = uuid.UUID(request.GET["organization"]) if request.GET.get("organization") else None
    except ValueError:
        organization_id = None
    return slug_check_response(request, organization_id=organization_id)


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
                table = update_catalog_labels(TableDefinition, table.pk, data, expected_revision=revision)
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
                update_catalog_labels(FieldDefinition, field.pk, data, expected_revision=revision)
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
    from skeleton.audit.filters import operation_choices, parse_audit_filters
    from skeleton.service_api.audit import AuditLogService
    from skeleton.tenancy.context import platform_audit_scope
    from skeleton.web.presentation import audit_rows

    try:
        filters = parse_audit_filters(request.GET)
    except ValidationError:
        return error_response(request, gettext("Verifique os filtros informados."), status=400)

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
    rows.object_list = audit_rows(rows)
    return render(
        request,
        "skeleton/web/platform/audit.html",
        {
            "platform_section": "audit",
            "rows": rows,
            # Global audit: the actor stays the full identifier, never a tenant's name (#475).
            "show_actor_uuid": True,
            "filters": request.GET,
            "operations": operation_choices(),
        },
    )


# --------------------------------------------------------------------------- applications


def _manifest(app_label):
    from skeleton.system.administration import application_manifest

    return application_manifest(app_label)


def _app_manifest(app_label):
    """An app with a gate of its own; a first-party library has no availability page (#792)."""
    manifest = _manifest(app_label)
    if manifest.library:
        raise Http404
    return manifest


@platform_page
@require_safe
def app_list(request):
    from skeleton.system.administration import application_identity
    from skeleton.system.app_registry import all_apps, dependent_app_labels

    def card(manifest):
        # One card for apps and libraries (#807): kind, version, origin and dependencies; a
        # first-party library (#792) also lists the apps that use it.
        return {
            **application_identity(manifest),
            "library": manifest.library,
            "requires": _app_labels(manifest.dependencies),
            "used_by": _app_labels(dependent_app_labels(manifest.app_label)) if manifest.library else [],
        }

    manifests = sorted(all_apps(), key=lambda manifest: manifest.label)
    applications = [card(manifest) for manifest in manifests if not manifest.library]
    libraries = [card(manifest) for manifest in manifests if manifest.library]
    return render(
        request,
        "skeleton/web/platform/apps.html",
        {"platform_section": "apps", "applications": applications, "libraries": libraries},
    )


@platform_page
@require_safe
def app_detail(request, app_label):
    from skeleton.appearance.app_themes import themes_of_app
    from skeleton.system.administration import application_identity, screen_sections
    from skeleton.system.app_registry import dependent_app_labels
    from skeleton.system.availability import availability_summary

    manifest = _manifest(app_label)
    return render(
        request,
        "skeleton/web/platform/app.html",
        {
            "platform_section": "apps",
            "instance_themes": themes_of_app(manifest),
            "application": application_identity(manifest),
            # A library has no availability: it follows the apps that use it (#792).
            "availability": None if manifest.library else availability_summary(manifest.app_label, user=request.user),
            "library": manifest.library,
            "used_by": _app_labels(dependent_app_labels(manifest.app_label)) if manifest.library else [],
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


def _availability_page(
    request, manifest, *, mode_form=None, grant_form=None, revoke_error=None, limits_form=None, status=200
):
    """Availability of one app (#23): global rows only; no organization's activation is read here."""
    from skeleton.system.administration import application_identity
    from skeleton.system.availability import availability_summary
    from skeleton.system.limits import MEMBERS, base_for

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
            "limits_form": limits_form or AppLimitsForm.for_limits(summary.limits, revision=summary.revision),
            # Without an instance default, what the app adds becomes the whole member limit (#696).
            "limits_without_base": bool(summary.limits.get(MEMBERS)) and base_for(MEMBERS) is None,
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

    manifest = _app_manifest(app_label)
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

    manifest = _app_manifest(app_label)
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

    manifest = _app_manifest(app_label)
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
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def app_limits(request, app_label):
    """What the app adds to each organization limit where it is effective (#696), with CAS."""
    from skeleton.system.availability import set_app_limits

    manifest = _app_manifest(app_label)
    form = AppLimitsForm(request.POST)
    if not form.is_valid():
        return _availability_page(request, manifest, limits_form=form, status=400)
    try:
        with transaction.atomic():
            set_app_limits(
                manifest.app_label,
                user=request.user,
                limits=form.limits(),
                revision=form.cleaned_data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _availability_page(request, manifest, limits_form=form, status=status)
    messages.success(request, gettext("Acréscimos de limite atualizados."))
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
        # A sensitive screen needs a recent authentication on every method, GET included (#311).
        return recent_authentication_required(handler)(request)
    return handler(request)


# --------------------------------------------------------------------------- apps of one organization (#699)


def _limit_text(value):
    return gettext("ilimitado") if value is None else str(value)


def _app_assignment(request, sysid, app_label, *, assign):
    from skeleton.system.applications import assign_app, unassign_app

    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid, is_active=True)
    manifest = _app_manifest(app_label)
    before = _member_limit(request, organization)
    try:
        with transaction.atomic():
            if assign:
                assign_app(manifest.app_label, user=request.user, organization_id=organization.pk)
            else:
                unassign_app(manifest.app_label, user=request.user, organization_id=organization.pk)
    except (ValidationError, ConcurrentUpdate) as exc:
        detail = " ".join(exc.messages) if isinstance(exc, ValidationError) else gettext("A app mudou. Tente de novo.")
        return _organization_page(request, organization, apps_error=detail, status=409)
    after = _member_limit(request, organization)
    done = gettext("%(app)s atribuída.") if assign else gettext("%(app)s retirada.")
    messages.success(
        request,
        f"{done % {'app': manifest.label}} "
        + gettext("Limite de membros: %(before)s → %(after)s")
        % {"before": _limit_text(before), "after": _limit_text(after)},
    )
    return redirect("platform:organization", sysid=organization.pk)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def organization_app_assign(request, sysid, app_label):
    """Offer (when selected) and activate the app with its dependencies in one organization."""
    return _app_assignment(request, sysid, app_label, assign=True)


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def organization_app_unassign(request, sysid, app_label):
    """Deactivate the app in one organization (and revoke its grant); its data is kept."""
    return _app_assignment(request, sysid, app_label, assign=False)


# --------------------------------------------------------------------------- organization limits (#697)


def _limit_source_label(source):
    """``billing``, ``platform`` or ``app:<label>`` as the page names it."""
    from skeleton.system.app_registry import get_app

    if source.startswith("app:"):
        label = source.removeprefix("app:")
        try:
            return gettext("App %(name)s") % {"name": get_app(label).label}
        except KeyError:
            return gettext("App %(name)s") % {"name": label}
    return {"billing": gettext("Cobrança"), "platform": gettext("Bônus da plataforma")}.get(source, source)


def _limit_override_form(rows, metric):
    """The adjustment form, filled with the adjustment of ``metric`` when the row has one."""
    from django.utils import timezone

    from .forms import LimitOverrideForm

    override = next((item["row"].override for item in rows if item["row"].status.metric == metric), None)
    if override is None:
        initial = {"expected_revision": 0, "mode": "bonus", "metric": metric}
    else:
        expires_at = timezone.localtime(override.expires_at) if override.expires_at else None
        initial = {
            "metric": metric,
            "mode": override.mode,
            "value": override.value,
            "reason": override.reason,
            "expires_at": expires_at,
            "expected_revision": override.revision,
        }
    return LimitOverrideForm(initial=initial)


def _limits_page(request, organization, *, form=None, error=None, status=200):
    from skeleton.system.limits import limit_breakdown

    rows = [
        {"row": row, "sources": [(_limit_source_label(item.source), item.amount) for item in row.status.contributions]}
        for row in limit_breakdown(actor=request.user, organization=organization)
    ]
    return render(
        request,
        "skeleton/web/platform/organization_limits.html",
        {
            "platform_section": "organizations",
            "subject": organization,
            "rows": rows,
            "form": form or _limit_override_form(rows, request.GET.get("metric")),
            "error": error,
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_limits(request, sysid):
    """Where each limit of the organization comes from, and the platform's adjustment of it.

    POST with ``action=remove`` removes one adjustment; any other POST sets one. Both with CAS
    on the adjustment's revision (0 creates or restores a removed one).
    """
    from skeleton.system.limits import remove_limit_override, set_limit_override

    from .forms import LimitOverrideForm, LimitOverrideRemoveForm

    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid)
    if request.method != "POST":
        return _limits_page(request, organization)
    if request.POST.get("action") == "remove":
        remove = LimitOverrideRemoveForm(request.POST)
        if not remove.is_valid():
            return _limits_page(request, organization, error=gettext("Pedido de remoção inválido."), status=400)
        try:
            with transaction.atomic():
                remove_limit_override(
                    actor=request.user,
                    organization=organization,
                    metric=remove.cleaned_data["metric"],
                    expected_revision=remove.cleaned_data["expected_revision"],
                )
        except ConcurrentUpdate:
            detail = gettext("O ajuste mudou desde que a página foi aberta. Revise antes de remover.")
            return _limits_page(request, organization, error=detail, status=409)
        messages.success(request, gettext("Ajuste removido: o limite volta ao cálculo."))
        return redirect("platform:organization-limits", sysid=organization.pk)
    form = LimitOverrideForm(request.POST)
    if not form.is_valid():
        return _limits_page(request, organization, form=form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            set_limit_override(
                actor=request.user,
                organization=organization,
                metric=data["metric"],
                mode=data["mode"],
                value=data["value"],
                reason=data["reason"],
                expires_at=data["expires_at"],
                expected_revision=data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _limits_page(request, organization, form=form, status=status)
    messages.success(request, gettext("Ajuste de limite salvo."))
    return redirect("platform:organization-limits", sysid=organization.pk)


# --------------------------------------------------------------------------- setting locks (#701)


def _setting_display(setting, value):
    if setting.kind == "frozen":
        return gettext("enviado") if value else gettext("nenhum")
    if setting.kind == "bool":
        return gettext("sim") if value else gettext("não")
    if setting.kind == "text":
        from skeleton.tenancy.business_hours import format_holidays, format_hours

        return (format_hours if setting.key.endswith("business_hours") else format_holidays)(value) or "—"
    return "—" if value in (None, "") else str(value)


def _setting_locks_page(request, organization, *, form=None, error=None, status=200):
    from itertools import groupby

    from skeleton.tenancy.setting_locks import lockable_settings, settings_overview

    from .forms import SettingLockForm

    rows = settings_overview(actor=request.user, organization=organization)
    groups = [
        {
            "label": group,
            "rows": [
                {
                    "key": row.setting.key,
                    "label": row.setting.label,
                    "current": _setting_display(row.setting, row.current),
                    "lock": row.lock,
                    "locked": _setting_display(row.setting, row.lock.value) if row.lock else "",
                }
                for row in items
            ],
        }
        for group, items in groupby(rows, key=lambda row: row.setting.group)
    ]
    chosen = request.GET.get("key")
    lock = next((row.lock for row in rows if row.setting.key == chosen and row.lock), None)
    initial = {"key": chosen, "expected_revision": lock.revision if lock else 0}
    if lock is not None:
        initial["reason"] = lock.reason
    return render(
        request,
        "skeleton/web/platform/organization_settings.html",
        {
            "platform_section": "organizations",
            "subject": organization,
            "groups": groups,
            "form": form or SettingLockForm(initial=initial, settings=lockable_settings()),
            "error": error,
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_setting_locks(request, sysid):
    """Lock or unlock settings of one organization; the value is written to the organization."""
    from skeleton.tenancy.setting_locks import lock_setting, lockable_settings, parse_form_value, unlock_setting

    from .forms import SettingLockForm, SettingUnlockForm

    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid)
    if request.method != "POST":
        return _setting_locks_page(request, organization)
    if request.POST.get("action") == "unlock":
        unlock = SettingUnlockForm(request.POST)
        if not unlock.is_valid():
            return _setting_locks_page(request, organization, error=gettext("Pedido inválido."), status=400)
        try:
            with transaction.atomic():
                unlock_setting(
                    actor=request.user,
                    organization=organization,
                    key=unlock.cleaned_data["key"],
                    expected_revision=unlock.cleaned_data["expected_revision"],
                )
        except ConcurrentUpdate:
            detail = gettext("A trava mudou desde que a página foi aberta. Revise antes de destravar.")
            return _setting_locks_page(request, organization, error=detail, status=409)
        messages.success(request, gettext("Configuração destravada: a organização pode mudá-la de novo."))
        return redirect("platform:organization-settings", sysid=organization.pk)
    settings = lockable_settings()
    form = SettingLockForm(request.POST, settings=settings)
    if not form.is_valid():
        return _setting_locks_page(request, organization, form=form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            value = parse_form_value(settings[data["key"]], data["value"])
            lock_setting(
                actor=request.user,
                organization=organization,
                key=data["key"],
                value=value,
                reason=data["reason"],
                expected_revision=data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _setting_locks_page(request, organization, form=form, status=status)
    messages.success(request, gettext("Configuração travada."))
    return redirect("platform:organization-settings", sysid=organization.pk)


# --------------------------------------------------------------------------- platform fields of an app (#702)


def _platform_fields_form(manifest, fields, data=None, initial=None):
    from django import forms

    declared = {"expected_revision": forms.IntegerField(min_value=0, widget=forms.HiddenInput)}
    for field in fields:
        options = {"label": field.label or field.name, "help_text": field.help_text, "required": False}
        if field.value_type is bool:
            declared[field.name] = forms.BooleanField(**options)
        elif field.value_type is int:
            declared[field.name] = forms.IntegerField(**{**options, "required": True})
        else:
            declared[field.name] = forms.CharField(max_length=field.max_length, **options)
    return type("PlatformFieldsForm", (forms.Form,), declared)(data, initial=initial)


def _stored_configuration(request, organization, manifest):
    """Revision and configuration of the app in the organization, read in the inspection context."""
    from skeleton.access.administration import read_only_inspection
    from skeleton.system.app_registry import validate_configuration
    from skeleton.system.models import AppInstallation
    from skeleton.tenancy.context import system_context

    with (
        system_context(reason="platform:organization-inspect", organization_id=organization.pk),
        read_only_inspection("default"),
    ):
        record = AppInstallation.objects.filter(app_label=manifest.app_label).first()
        stored = record.configuration if record else {}
    try:
        values = validate_configuration(manifest, stored)
    except ValidationError:
        values = validate_configuration(manifest, {})
    return (record.revision if record else 0), values


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_app_configuration(request, sysid, app_label):
    """The fields of an app only the platform writes (``managed_by: "platform"``), per organization."""
    from skeleton.system.administration import application_identity
    from skeleton.system.applications import configure_application_by_platform, platform_fields

    organization = get_object_or_404(_model("sk_tenancy.Organization").objects, pk=sysid)
    manifest = _app_manifest(app_label)
    fields = platform_fields(manifest)
    if not fields:
        raise Http404
    revision, values = _stored_configuration(request, organization, manifest)
    initial = {**{field.name: values[field.name] for field in fields}, "expected_revision": revision}
    form = _platform_fields_form(manifest, fields, request.POST if request.method == "POST" else None, initial)
    status = 200
    if request.method == "POST":
        if form.is_valid():
            data = dict(form.cleaned_data)
            expected = data.pop("expected_revision")
            data = {
                field.name: data[field.name] if data[field.name] is not None else values[field.name] for field in fields
            }
            try:
                with transaction.atomic():
                    configure_application_by_platform(
                        manifest.app_label,
                        user=request.user,
                        organization_id=organization.pk,
                        values=data,
                        revision=expected,
                    )
            except (ValidationError, ConcurrentUpdate) as exc:
                status = form_error(form, exc)
            else:
                messages.success(request, gettext("Campos da plataforma salvos."))
                return redirect("platform:organization-app-configuration", sysid=organization.pk, app_label=app_label)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/platform/organization_app_configuration.html",
        {
            "platform_section": "organizations",
            "subject": organization,
            "application": application_identity(manifest),
            "form": form,
        },
        status=status,
    )


# --------------------------------------------------------------------------- policy of new organizations (#703)


def _policy_page(request, *, form=None, error=None, status=200):
    from skeleton.tenancy.provisioning import rule_targets, rules

    from .forms import ProvisioningRuleForm

    targets = rule_targets()
    rows = [
        {
            "rule": rule,
            "target": targets.get(rule.key),
            "value": "—" if rule.value in (None, "") else rule.value,
        }
        for rule in rules()
    ]
    chosen = request.GET.get("key")
    rule = next((row["rule"] for row in rows if row["rule"].key == chosen), None)
    initial = {"key": chosen, "expected_revision": rule.revision if rule else 0, "mode": "default"}
    if rule is not None:
        initial.update(mode=rule.mode, value=rule.value, locked=rule.locked, reason=rule.reason)
    return render(
        request,
        "skeleton/web/platform/organization_policy.html",
        {
            "platform_section": "organizations",
            "rows": rows,
            "form": form or ProvisioningRuleForm(initial=initial, targets=targets),
            "error": error,
        },
        status=status,
    )


@platform_page
@require_http_methods(["GET", "POST"])
@recent_authentication_required(allow_get=True)
@transaction.atomic
def organization_policy(request):
    """The rules every new organization receives; ``action=apply`` queues one for the existing ones."""
    from skeleton.tenancy.provisioning import enqueue_apply, remove_rule, rule_targets, rules, save_rule
    from skeleton.tenancy.setting_locks import parse_form_value

    from .forms import ProvisioningRuleForm, ProvisioningRuleRemoveForm

    if request.method != "POST":
        return _policy_page(request)
    action = request.POST.get("action")
    if action in ("remove", "apply"):
        chosen = ProvisioningRuleRemoveForm(request.POST)
        if not chosen.is_valid():
            return _policy_page(request, error=gettext("Pedido inválido."), status=400)
        key, revision = chosen.cleaned_data["key"], chosen.cleaned_data["expected_revision"]
        try:
            with transaction.atomic():
                if action == "remove":
                    remove_rule(actor=request.user, key=key, expected_revision=revision)
                else:
                    rule = next((rule for rule in rules() if rule.key == key and rule.revision == revision), None)
                    if rule is None:
                        raise ConcurrentUpdate("Provisioning rule changed.")
                    enqueue_apply(actor=request.user, rule=rule)
        except ConcurrentUpdate:
            return _policy_page(request, error=gettext("A regra mudou. Atualize a página."), status=409)
        except ValidationError as exc:
            return _policy_page(request, error=" ".join(exc.messages), status=400)
        messages.success(
            request,
            gettext("Regra removida.")
            if action == "remove"
            else gettext("Aplicação às organizações existentes agendada."),
        )
        return redirect("platform:organization-policy")
    targets = rule_targets()
    form = ProvisioningRuleForm(request.POST, targets=targets)
    if not form.is_valid():
        return _policy_page(request, form=form, status=400)
    data = form.cleaned_data
    try:
        with transaction.atomic():
            target = targets[data["key"]]
            value = None
            if data["mode"] == "default" and (data["value"] or target.kind == "bool"):
                value = parse_form_value(target, data["value"])
            save_rule(
                actor=request.user,
                key=data["key"],
                mode=data["mode"],
                value=value,
                locked=data["locked"],
                reason=data["reason"],
                expected_revision=data["expected_revision"],
            )
    except (ValidationError, ConcurrentUpdate) as exc:
        status = form_error(form, exc)
        return _policy_page(request, form=form, status=status)
    messages.success(request, gettext("Regra salva: vale para as organizações criadas a partir de agora."))
    return redirect("platform:organization-policy")


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
    from skeleton.tenancy.site_domains import site_claims

    return render(
        request,
        "skeleton/web/platform/domains.html",
        {"platform_section": "domains", "rows": list(claims()), "site_rows": list(site_claims()), "error": error},
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


@platform_page
@require_http_methods(["POST"])
@recent_authentication_required
@transaction.atomic
def site_domain_revoke(request, sysid):
    """Drop the claim of a custom domain (#488): the organization's address stops answering on it."""
    from skeleton.tenancy.site_domains import DomainUnavailable, revoke_site_claim

    try:
        revision = int(request.POST.get("revision", ""))
    except ValueError:
        revision = None
    try:
        with transaction.atomic():
            claim = revoke_site_claim(actor=request.user, claim_id=sysid, expected_revision=revision)
    except DomainUnavailable:
        raise Http404 from None
    except ConcurrentUpdate:
        return _domains_page(request, status=409, error=gettext("A posse mudou. Atualize a página."))
    messages.success(request, gettext("Posse de %(domain)s revogada.") % {"domain": claim.domain})
    return redirect("platform:domains")
