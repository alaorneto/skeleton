from functools import wraps

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils.translation import gettext
from django.views.decorators.http import require_http_methods

from skeleton.access import services as access_services
from skeleton.access.capabilities import (
    capabilities_for,
    has_any_capability,
    require_admin_surface,
    require_resource_capability,
    resource_capability,
)
from skeleton.access.policies import require_record_action
from skeleton.accounts.mfa import mfa_required
from skeleton.api.serializers import LINKS, dedicated_input, dedicated_model
from skeleton.common.exceptions import ConcurrentUpdate, LiveReferencesExist
from skeleton.service_api import errors as service_errors
from skeleton.system.registry import get_spec
from skeleton.tenancy.context import current_context
from skeleton.tenancy.invitations import issue_invitation
from skeleton.web.forms import ProfileForm, dedicated_form, dedicated_label, explicit_form, record_form, wire_values
from skeleton.web.presentation import (
    detail_sections,
    form_sections,
    grouped_specs,
    list_context,
    list_fields,
    safe_record_label,
)
from skeleton.web.services import (
    ACCESS_FIELDS,
    change_record_access,
    create_record,
    delete_record,
    exposed_spec,
    readable_specs,
    record_queryset,
    require_access_management,
    restore_record,
    update_record,
)


def presented_specs(user):
    return [
        {
            "table_key": spec.model._meta.label_lower,
            "label": spec.label or spec.model._meta.verbose_name_plural,
            "description": spec.description,
            "read_fields": spec.read_fields,
        }
        for spec in readable_specs(user)
    ]


def record_breadcrumbs(spec, organization_id):
    from skeleton.system.app_registry import get_app

    try:
        manifest = get_app(spec.model._meta.app_label)
        app_label = manifest.label
    except KeyError:
        app_label = spec.model._meta.app_label
    key = spec.model._meta.label_lower
    return [
        {"label": gettext("Organização"), "url": reverse("web:dashboard", kwargs={"organization_id": organization_id})},
        {
            "label": app_label,
            "url": reverse("web:catalog", kwargs={"organization_id": organization_id})
            + "#app_"
            + spec.model._meta.app_label,
        },
        {
            "label": spec.label or spec.model._meta.verbose_name_plural,
            "url": reverse("web:records", kwargs={"organization_id": organization_id, "table_key": key}),
        },
    ]


def _display(value):
    """Booleans read as Sim/Não on the screens; other values stay as they are."""
    if value is True:
        return gettext("Sim")
    if value is False:
        return gettext("Não")
    return value


def tenant_page(*, admin=False, capability=None):
    """Tenant HTML page; ``admin`` requires the administrator, ``capability`` a delegated capability.

    ``capability`` is a capability name or ``callable(request, kwargs) -> name``
    for pages whose resource selects it.
    """

    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if current_context().organization_id is None:
                raise Http404
            needed = capability(request, kwargs) if callable(capability) else capability
            if admin:
                require_admin_surface(request.user)
            elif needed:
                require_resource_capability(request.user, needed)
            if has_any_capability(request.user) and mfa_required(request.user):
                return redirect("mfa_index")
            try:
                # Language and time zone come from skeleton.common.locale.LocaleMiddleware (#170).
                if (admin or needed) and request.method not in {"GET", "HEAD", "OPTIONS"}:
                    with access_services.organization_lock(request.user, capability=needed):
                        return view(request, *args, **kwargs)
                return view(request, *args, **kwargs)
            except (service_errors.NotFound, service_errors.Unavailable):
                # Neutral like the REST mapping: a disabled integration looks like a missing page.
                raise Http404 from None
            except service_errors.Forbidden:
                raise PermissionDenied from None
            except (ValidationError, service_errors.InvalidInput):
                return render(
                    request,
                    "skeleton/web/error.html",
                    {"detail": gettext("Verifique os campos ou filtros informados.")},
                    status=400,
                )
            except (IntegrityError, service_errors.Conflict):
                return render(
                    request,
                    "skeleton/web/error.html",
                    {"detail": gettext("A operação conflita com os dados existentes.")},
                    status=409,
                )

        return wrapper

    return decorator


PAGE_SIZE = 25


def _page_number(request):
    try:
        page = int(request.GET.get("page", "1"))
        if page < 1:
            raise ValueError
    except ValueError:
        raise ValidationError(gettext("Página inválida.")) from None
    return page


def pagination(request, queryset, *, validated_filters=None):
    from django.core.paginator import Paginator

    rows = Paginator(queryset, PAGE_SIZE).get_page(_page_number(request))
    return _page_links(rows, validated_filters)


def service_pagination(request, fetch, *, validated_filters=None):
    """Same page object as ``pagination`` for a service that returns a numbered page.

    ``fetch(page=..., page_size=...)`` returns an object with ``items`` and ``count``. A page
    past the end shows the last one, as ``Paginator.get_page`` does.
    """
    from django.core.paginator import Page, Paginator

    number = _page_number(request)
    result = fetch(page=number, page_size=PAGE_SIZE)
    paginator = Paginator((), PAGE_SIZE)
    paginator.count = result.count
    if number > paginator.num_pages:
        number = paginator.num_pages
        result = fetch(page=number, page_size=PAGE_SIZE)
        paginator = Paginator((), PAGE_SIZE)
        paginator.count = result.count
    return _page_links(Page(list(result.items), number, paginator), validated_filters)


def _page_links(rows, validated_filters):
    from urllib.parse import urlencode

    params = dict(validated_filters or {})
    params.pop("page", None)
    if rows.has_previous():
        rows.previous_url = "?" + urlencode({**params, "page": rows.previous_page_number()})
    if rows.has_next():
        rows.next_url = "?" + urlencode({**params, "page": rows.next_page_number()})
    return rows


def form_error(form, error):
    from skeleton.accounts.platform import LastPlatformAdministrator

    if isinstance(error, access_services.LastAdministrator):
        form.add_error(None, gettext("A organização precisa manter ao menos um administrador ativo."))
        return 409
    if isinstance(error, LastPlatformAdministrator):
        form.add_error(None, gettext("A plataforma precisa manter ao menos um administrador ativo."))
        return 409
    if isinstance(error, ValidationError):
        if hasattr(error, "message_dict"):
            for field, errors in error.message_dict.items():
                form.add_error(field if field in form.fields else None, errors)
        else:
            form.add_error(None, error.messages)
        from skeleton.api.errors import validation_conflict

        return 409 if validation_conflict(error) else 400
    if isinstance(error, ConcurrentUpdate):
        form.add_error(None, gettext("O registro foi alterado. Atualize a página antes de tentar novamente."))
    elif isinstance(error, LiveReferencesExist):
        form.add_error(None, gettext("Existem vínculos ativos. Revogue-os antes de remover."))
    else:
        form.add_error(None, gettext("A operação conflita com os dados existentes."))
    return 409


def check_html_revision(form, record):
    if form.cleaned_data["revision"] != record.revision:
        raise ConcurrentUpdate(gettext("Revisão desatualizada."))


@login_required
def home(request):
    from skeleton.service_api.organizations import OrganizationService
    from skeleton.tenancy.admission import directory
    from skeleton.tenancy.domains import suggestions
    from skeleton.tenancy.invitations import pending_invitation
    from skeleton.web.admission_views import request_rows

    memberships = [
        {"organization_id": item.sysid, "organization_name": item.name, "organization_slug": item.slug}
        for item in OrganizationService().my_organizations(actor=request.user)
    ]
    proof = pending_invitation(request)
    return render(
        request,
        "skeleton/web/home.html",
        {
            "memberships": memberships,
            # Join paths (#25): only the invitation this session opened; never other people's.
            "pending_invitation": {"email": proof.email} if proof else None,
            # The person's own requests (#26, self-discovery) and their email domain's organizations (#28).
            "join_requests": request_rows(request.user),
            "suggested_organizations": [
                {"organization_id": item.organization_id, "organization_name": item.name, "domain": item.domain}
                for item in suggestions(request.user)
            ],
            "join_directory": bool(directory(request.user)),
        },
    )


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def leave_organization(request, organization_id):
    """Confirm (GET, no change) and leave (POST with CSRF and the membership revision)."""
    from skeleton.service_api.organizations import OrganizationService
    from skeleton.tenancy.models import Membership

    if current_context().organization_id is None:
        raise Http404
    organization = OrganizationService().current(actor=request.user)
    membership = Membership.objects.filter(user=request.user, is_active=True).first()
    if membership is None:
        raise Http404
    status, error = 200, None
    if request.method == "POST":
        try:
            revision = int(request.POST.get("revision", ""))
        except ValueError:
            revision = None
        try:
            if revision is None:
                raise ConcurrentUpdate("Missing membership revision.")
            with transaction.atomic():
                access_services.leave_organization(request.user, expected_revision=revision)
        except access_services.LastAdministrator:
            status = 409
            error = gettext(
                "Você é a última pessoa que administra esta organização. Designe outro administrador antes de sair."
            )
        except ConcurrentUpdate:
            status = 409
            error = gettext("Seu vínculo mudou desde que esta página foi aberta. Confira e tente de novo.")
        else:
            messages.success(request, gettext("Você saiu de %(name)s.") % {"name": organization.name})
            return redirect("web:home")
    return render(
        request,
        "skeleton/web/leave.html",
        {
            "organization": organization,
            "organization_id": organization_id,
            "revision": membership.revision,
            "error": error,
        },
        status=status,
    )


def _service_form_error(form, error):
    """Show a service error on the form with the same codes as the former internal exceptions."""
    if isinstance(error, service_errors.Conflict) and error.code == "stale_revision":
        return form_error(form, ConcurrentUpdate(gettext("Revisão desatualizada.")))
    if isinstance(error, service_errors.InvalidInput):
        return form_error(form, ValidationError({name: list(messages) for name, messages in error.errors.items()}))
    return form_error(form, ValidationError(error.detail))


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def profile(request):
    from skeleton.service_api.identity import IdentityService

    me = IdentityService().me(actor=request.user)
    initial = {
        "first_name": me.first_name,
        "last_name": me.last_name,
        "display_name": me.profile_display_name,
        "timezone": me.timezone,
        "language": me.language,
        "user_revision": me.user_revision,
        "profile_revision": me.revision,
    }
    form = ProfileForm(request.POST or None, initial=initial)
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            values = dict(form.cleaned_data)
            user_revision = values.pop("user_revision")
            profile_revision = values.pop("profile_revision")
            try:
                IdentityService().update_my_profile(
                    actor=request.user,
                    expected_user_revision=user_revision,
                    expected_revision=profile_revision,
                    changes=tuple(values.items()),
                )
                messages.success(request, gettext("Perfil atualizado."))
                return redirect("web:profile")
            except service_errors.ServiceError as exc:
                status = _service_form_error(form, exc)
        else:
            status = 400
    return render(request, "account/profile.html", {"title": gettext("Perfil"), "form": form}, status=status)


def access_capability(request, kwargs):
    return resource_capability(kwargs.get("resource", ""), method=request.method)


@tenant_page()
def dashboard(request, organization_id):
    from skeleton.service_api.organizations import OrganizationService

    organization = OrganizationService().current(actor=request.user)
    specs = presented_specs(request.user)
    return render(
        request,
        "skeleton/web/dashboard.html",
        {
            "organization": organization,
            "specs": specs,
            "app_groups": grouped_specs(specs),
            "organization_id": organization_id,
            "capabilities": capabilities_for(request.user),
        },
    )


@tenant_page(capability=access_capability)
def access_list(request, organization_id, resource):
    model, fields = dedicated_model(resource)
    queryset = model.objects.order_by("created_at", "sysid")
    relations = [name for name in fields if model._meta.get_field(name).is_relation]
    if relations:
        queryset = queryset.select_related(*relations)
    rows = pagination(request, queryset)
    rows.object_list = [
        {
            "sysid": item.sysid,
            "revision": item.revision,
            "fields": [(dedicated_label(model, name), _display(getattr(item, name))) for name in fields],
        }
        for item in rows
    ]
    return render(
        request,
        "skeleton/web/access_list.html",
        {
            "title": get_spec(model._meta.label_lower).label,
            "rows": rows,
            "organization_id": organization_id,
            "resource": resource,
            "is_link": resource in LINKS,
        },
    )


@tenant_page(capability=access_capability)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def access_edit(request, organization_id, resource, sysid=None):
    model, fields = dedicated_model(resource)
    item = None
    if sysid:
        if resource in LINKS or resource == "invitations":
            raise Http404
        item = model.objects.select_for_update().filter(pk=sysid).first()
        if item is None:
            raise Http404
    form = dedicated_form(
        resource, user=request.user, data=request.POST if request.method == "POST" else None, instance=item
    )
    status = 200
    if request.method == "POST":
        allowed = set(form.fields) | {"csrfmiddlewaretoken"}
        if set(request.POST) - allowed:
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            try:
                if item:
                    check_html_revision(form, item)
                values = dedicated_input(
                    resource, wire_values(form.cleaned_data), user=request.user, creating=item is None
                )
                if resource == "invitations":
                    issue_invitation(user=request.user, request=request, **values)
                elif item:
                    name = {"memberships": "update_membership", "roles": "update_role", "groups": "update_group"}[
                        resource
                    ]
                    getattr(access_services, name)(item, user=request.user, data=values)
                else:
                    name = {
                        "memberships": "create_membership",
                        "roles": "create_role",
                        "groups": "create_group",
                        "member-roles": "assign_member_role",
                        "group-members": "add_group_member",
                        "group-roles": "assign_group_role",
                        "table-permissions": "grant_table_permission",
                        "record-grants": "create_record_grant",
                    }[resource]
                    getattr(access_services, name)(user=request.user, data=values)
                messages.success(request, gettext("Operação concluída."))
                return redirect("web:access-list", organization_id=organization_id, resource=resource)
            except (ValidationError, IntegrityError, ConcurrentUpdate, LiveReferencesExist) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/form.html",
        {"title": gettext("Editar") if item else gettext("Criar"), "form": form, "organization_id": organization_id},
        status=status,
    )


@tenant_page(capability=access_capability)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def access_delete(request, organization_id, resource, sysid):
    model, _ = dedicated_model(resource)
    item = model.objects.select_for_update().filter(pk=sysid).first()
    if item is None:
        raise Http404
    form = explicit_form(
        model,
        (),
        user=request.user,
        data=request.POST if request.method == "POST" else None,
        instance=item,
        revision=True,
    )
    status = 200
    if request.method == "POST":
        if form.is_valid():
            try:
                check_html_revision(form, item)
                if resource in LINKS:
                    access_services.revoke_link(item, user=request.user)
                elif resource == "invitations":
                    with transaction.atomic():
                        item.delete()
                else:
                    name = {"memberships": "delete_membership", "roles": "delete_role", "groups": "delete_group"}[
                        resource
                    ]
                    getattr(access_services, name)(item, user=request.user)
                return redirect("web:access-list", organization_id=organization_id, resource=resource)
            except (ValidationError, IntegrityError, ConcurrentUpdate, LiveReferencesExist) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    return render(
        request,
        "skeleton/web/form.html",
        {"title": "Revogar / remover", "form": form, "organization_id": organization_id},
        status=status,
    )


@tenant_page()
def catalog(request, organization_id):
    specs = presented_specs(request.user)
    return render(
        request,
        "skeleton/web/catalog.html",
        {
            "title": gettext("Catálogo"),
            "specs": specs,
            "app_groups": grouped_specs(specs),
            "organization_id": organization_id,
        },
    )


@tenant_page()
def audit(request, organization_id):
    from skeleton.access.policies import is_organization_admin
    from skeleton.api.serializers import audit_data
    from skeleton.audit.filters import operation_choices, parse_audit_filters
    from skeleton.service_api.audit import AuditLogService

    filters = parse_audit_filters(request.GET)
    rows = service_pagination(
        request,
        lambda **page: AuditLogService().events(actor=request.user, **filters, **page),
        validated_filters={name: value for name, value in request.GET.dict().items() if value != ""},
    )
    rows.object_list = [audit_data(event) for event in rows]
    # Domain events of the same request, still retained, for the administrator (#184).
    related = ()
    if "request_id" in filters and is_organization_admin(request.user):
        related = AuditLogService().related_domain_events(actor=request.user, request_id=filters["request_id"])
    return render(
        request,
        "skeleton/web/audit.html",
        {
            "title": gettext("Auditoria"),
            "rows": rows,
            "organization_id": organization_id,
            "filters": request.GET,
            "operations": operation_choices(),
            "related_events": related,
            # The filtered audit as CSV, for the administrator (#186).
            "can_export_audit": is_organization_admin(request.user),
        },
    )


@tenant_page()
def records(request, organization_id, table_key, deleted=False):
    spec = exposed_spec(table_key)
    queryset = record_queryset(
        spec, user=request.user, action="restore" if deleted else "read", include_deleted=deleted
    ).order_by("created_at", "sysid")
    if deleted:
        queryset = queryset.filter(deleted_at__isnull=False)
    from skeleton.web.services import filter_records

    queryset = filter_records(queryset, spec, request.GET, ignore_empty=True)
    filters = {name: value for name, value in request.GET.items() if value != ""}
    queryset = queryset.only(*list_fields(spec))
    rows = pagination(request, queryset, validated_filters=filters)
    presentation = list_context(spec, rows.object_list, user=request.user, params=filters)
    return render(
        request,
        "skeleton/web/records.html",
        {
            "title": spec.label or spec.model._meta.verbose_name_plural,
            "description": spec.description,
            "rows": rows,
            "organization_id": organization_id,
            "table_key": table_key,
            "can_create": not deleted and can_action(spec, request.user, "create"),
            "can_restore": can_action(spec, request.user, "restore"),
            # CSV export of the same filters, by job (#182).
            "can_export": not deleted,
            "export_filters": {name: value for name, value in filters.items() if name not in {"page", "page_size"}},
            "deleted": deleted,
            "breadcrumbs": record_breadcrumbs(spec, organization_id),
            **presentation,
        },
    )


def can_action(spec, user, action):
    try:
        record_queryset(spec, user=user, action=action)
    except PermissionDenied:
        return False
    return True


@tenant_page()
def record_detail(request, organization_id, table_key, sysid):
    spec = exposed_spec(table_key)
    item = record_queryset(spec, user=request.user).filter(pk=sysid).first()
    if item is None:
        raise Http404
    permissions = {}
    for action in ("update", "delete"):
        try:
            record_queryset(spec, user=request.user, action=action)
            require_record_action(item, user=request.user, action=action)
            permissions[action] = True
        except PermissionDenied:
            permissions[action] = False
    try:
        require_access_management(item, user=request.user)
        permissions["manage_access"] = True
    except PermissionDenied:
        permissions["manage_access"] = False
    names = {
        "update": ("web:record-edit", gettext("Editar")),
        "delete": ("web:record-delete", gettext("Remover")),
        "manage_access": ("web:record-access", gettext("Gerenciar acesso")),
    }
    actions = [
        {
            "url": reverse(
                route, kwargs={"organization_id": organization_id, "table_key": table_key, "sysid": item.pk}
            ),
            "label": label,
        }
        for action, (route, label) in names.items()
        if permissions[action]
    ]
    from skeleton.comments.views import panel_context as comments_panel
    from skeleton.files.views import panel_context
    from skeleton.web.timeline import timeline_for

    return render(
        request,
        "skeleton/web/detail.html",
        {
            "attachments": panel_context(spec, item, user=request.user),
            "comments": comments_panel(spec, item, user=request.user),
            # Audit and attachments of the record, each filtered for the reader (#185).
            "timeline": timeline_for(request.user, spec, item),
            "record": {"sysid": item.sysid},
            "title": safe_record_label(item, spec),
            "actions": actions,
            "organization_id": organization_id,
            "breadcrumbs": record_breadcrumbs(spec, organization_id),
            "table_key": table_key,
            **detail_sections(item, spec, user=request.user),
        },
    )


@tenant_page()
@require_http_methods(["GET", "POST"])
@transaction.atomic
def record_edit(request, organization_id, table_key, sysid=None, operation="edit"):
    spec = exposed_spec(table_key)
    item = None
    action = {"edit": "update", "delete": "delete", "restore": "restore", "access": "manage_access"}[operation]
    if sysid:
        item = (
            record_queryset(spec, user=request.user, action=action, include_deleted=operation == "restore")
            .select_for_update()
            .filter(pk=sysid)
            .first()
        )
        if item is None:
            raise Http404
        if operation == "access":
            require_access_management(item, user=request.user)
    else:
        record_queryset(spec, user=request.user, action="create")
    names = (
        ACCESS_FIELDS if operation == "access" else (() if operation in ("delete", "restore") else spec.write_fields)
    )
    if operation == "edit":
        form = record_form(
            spec, user=request.user, data=request.POST if request.method == "POST" else None, instance=item
        )
    else:
        form = explicit_form(
            spec.model,
            names,
            user=request.user,
            data=request.POST if request.method == "POST" else None,
            instance=item,
            revision=item is not None,
        )
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, gettext("Campo não permitido."))
            status = 400
        elif form.is_valid():
            try:
                if item:
                    check_html_revision(form, item)
                values = wire_values(form.cleaned_data)
                if item is None:
                    item = create_record(spec, user=request.user, data=values)
                elif operation == "access":
                    change_record_access(item, user=request.user, data=values)
                elif operation in ("delete", "restore"):
                    (delete_record if operation == "delete" else restore_record)(item, user=request.user)
                else:
                    update_record(item, spec, user=request.user, data=values)
                if operation == "delete":
                    return redirect("web:records", organization_id=organization_id, table_key=table_key)
                return redirect("web:record", organization_id=organization_id, table_key=table_key, sysid=item.pk)
            except (ValidationError, IntegrityError, ConcurrentUpdate, LiveReferencesExist) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    cancel_kwargs = {"organization_id": organization_id, "table_key": table_key}
    cancel_route = "web:deleted-records" if operation == "restore" else "web:records"
    if item is not None and operation != "restore":
        try:
            require_record_action(item, user=request.user, action="read")
        except PermissionDenied:
            pass
        else:
            cancel_route = "web:record"
            cancel_kwargs["sysid"] = item.pk
    return render(
        request,
        "skeleton/web/form.html",
        {
            "title": {
                "edit": gettext("Editar") if item else gettext("Criar"),
                "delete": gettext("Remover"),
                "restore": gettext("Restaurar"),
                "access": gettext("Gerenciar acesso"),
            }[operation],
            "form": form,
            "organization_id": organization_id,
            "table_key": table_key,
            "cancel_url": reverse(cancel_route, kwargs=cancel_kwargs),
            "record": {"sysid": item.sysid} if item else None,
            "operation": operation,
            "submit_label": {
                "edit": gettext("Salvar"),
                "delete": gettext("Remover"),
                "restore": gettext("Restaurar"),
                "access": gettext("Salvar acesso"),
            }[operation],
            "confirmation": (
                gettext("Confirmar %(value1)s do registro %(pk)s.")
                % {
                    "value1": gettext("remoção lógica") if operation == "delete" else gettext("restauração"),
                    "pk": item.pk,
                }
                if operation in {"delete", "restore"}
                else None
            ),
            "breadcrumbs": record_breadcrumbs(spec, organization_id),
            **form_sections(form, spec, instance=item, user=request.user, operation=operation),
        },
        status=status,
    )


@tenant_page()
@require_http_methods(["GET"])
def reference_picker(request, organization_id, table_key, field_name):
    from urllib.parse import urlencode

    from django.urls import reverse

    from skeleton.web.references import reference_request

    spec = exposed_spec(table_key)
    data, item, access = reference_request(spec, field_name, user=request.user, params=request.GET)
    if item is None:
        back_url = reverse("web:record-create", kwargs={"organization_id": organization_id, "table_key": table_key})
    else:
        back_url = reverse(
            "web:record-access" if access else "web:record-edit",
            kwargs={"organization_id": organization_id, "table_key": table_key, "sysid": item.pk},
        )
    params = request.GET.dict()

    def page_url(number):
        return "?" + urlencode({**params, "page": number})

    return render(
        request,
        "skeleton/web/reference_picker.html",
        {
            "title": gettext("Selecionar referência"),
            "organization_id": organization_id,
            "options": data["results"],
            "count": data["count"],
            "back_url": back_url,
            "previous_url": page_url(data["page"] - 1) if data["page"] > 1 else None,
            "next_url": page_url(data["page"] + 1) if data["page"] * data["page_size"] < data["count"] else None,
            "picker_context": {
                "table_key": table_key,
                "field_name": field_name,
                "record": str(item.pk) if item else None,
            },
        },
    )
