from functools import wraps

from django.apps import apps
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import IntegrityError, transaction
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods
from django.urls import reverse
from django.utils import timezone

from skeleton.access import services as access_services
from skeleton.access.policies import authorized_audit_queryset, is_organization_admin, require_record_action
from skeleton.accounts.mfa import mfa_required
from skeleton.accounts.services import update_profile
from skeleton.api.serializers import LINKS, dedicated_input, dedicated_model
from skeleton.common.exceptions import ConcurrentUpdate, LiveReferencesExist
from skeleton.system.registry import get_spec
from skeleton.tenancy.context import current_context
from skeleton.tenancy.invitations import issue_invitation
from skeleton.tenancy.context import memberships_for_user
from skeleton.web.forms import ProfileForm, dedicated_form, explicit_form, record_form, wire_values
from skeleton.web.services import ACCESS_FIELDS, change_record_access, create_record, delete_record, exposed_spec, readable_specs, record_queryset, require_access_management, restore_record, update_record
from skeleton.web.presentation import detail_sections, form_sections, grouped_specs, list_context, list_fields, safe_record_label

def presented_specs(user):
    return [{"table_key": spec.model._meta.label_lower,
             "label": spec.label or spec.model._meta.verbose_name_plural,
             "description": spec.description, "read_fields": spec.read_fields}
            for spec in readable_specs(user)]

def record_breadcrumbs(spec, organization_id):
    from skeleton.system.app_registry import get_app
    try:
        manifest = get_app(spec.model._meta.app_label)
        app_label = manifest.label
    except KeyError:
        app_label = spec.model._meta.app_label
    key = spec.model._meta.label_lower
    return [
        {"label": "Organização", "url": reverse("web:dashboard", kwargs={"organization_id": organization_id})},
        {"label": app_label, "url": reverse("web:catalog", kwargs={"organization_id": organization_id}) + "#app_" + spec.model._meta.app_label},
        {"label": spec.label or spec.model._meta.verbose_name_plural,
         "url": reverse("web:records", kwargs={"organization_id": organization_id, "table_key": key})},
    ]


def tenant_page(*, admin=False):
    def decorator(view):
        @login_required
        @wraps(view)
        def wrapper(request, *args, **kwargs):
            if current_context().organization_id is None:
                raise Http404
            if admin:
                access_services.require_organization_admin(request.user)
            if is_organization_admin(request.user) and mfa_required(request.user):
                return redirect("mfa_index")
            try:
                with timezone.override(request.user.profile.timezone):
                    if admin and request.method not in {"GET", "HEAD", "OPTIONS"}:
                        with access_services.organization_lock(request.user):
                            return view(request, *args, **kwargs)
                    return view(request, *args, **kwargs)
            except ValidationError:
                return render(request, "skeleton/web/error.html", {"detail": "Verifique os campos ou filtros informados."}, status=400)
            except IntegrityError:
                return render(request, "skeleton/web/error.html", {"detail": "A operação conflita com os dados existentes."}, status=409)
        return wrapper
    return decorator


def pagination(request, queryset, *, validated_filters=None):
    from django.core.paginator import Paginator
    from urllib.parse import urlencode
    try:
        page = int(request.GET.get("page", "1"))
        if page < 1:
            raise ValueError
    except ValueError:
        raise ValidationError("Página inválida.") from None
    rows = Paginator(queryset, 25).get_page(page)
    params = dict(validated_filters or {})
    params.pop("page", None)
    if rows.has_previous():
        rows.previous_url = "?" + urlencode({**params, "page": rows.previous_page_number()})
    if rows.has_next():
        rows.next_url = "?" + urlencode({**params, "page": rows.next_page_number()})
    return rows


def form_error(form, error):
    if isinstance(error, access_services.LastAdministrator):
        form.add_error(None, "A organização precisa manter ao menos um administrador ativo.")
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
        form.add_error(None, "O registro foi alterado. Atualize a página antes de tentar novamente.")
    elif isinstance(error, LiveReferencesExist):
        form.add_error(None, "Existem vínculos ativos. Revogue-os antes de remover.")
    else:
        form.add_error(None, "A operação conflita com os dados existentes.")
    return 409


def check_html_revision(form, record):
    if form.cleaned_data["revision"] != record.revision:
        raise ConcurrentUpdate("Revisão desatualizada.")


@login_required
def home(request):
    return render(request, "skeleton/web/home.html", {"memberships": list(memberships_for_user(request.user))})


@login_required
@require_http_methods(["GET", "POST"])
@transaction.atomic
def profile(request):
    user = request.user
    item = apps.get_model("sk_accounts", "UserProfile").objects.get(user=user)
    initial = {name: getattr(user if name in ("first_name", "last_name") else item, name) for name in ("first_name", "last_name", "display_name", "timezone", "language")}
    initial.update(user_revision=user.revision, profile_revision=item.revision)
    form = ProfileForm(request.POST or None, initial=initial)
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, "Campo não permitido.")
            status = 400
        elif form.is_valid():
            values = dict(form.cleaned_data)
            user_revision = values.pop("user_revision")
            profile_revision = values.pop("profile_revision")
            try:
                update_profile(user=user, data=values, user_revision=user_revision, profile_revision=profile_revision)
                messages.success(request, "Perfil atualizado.")
                return redirect("web:profile")
            except (ValidationError, IntegrityError, ConcurrentUpdate) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    return render(request, "skeleton/web/form.html", {"title": "Meu perfil", "form": form}, status=status)


@tenant_page()
def dashboard(request, organization_id):
    organization = apps.get_model("sk_tenancy", "Organization").objects.get(pk=organization_id)
    specs = presented_specs(request.user)
    return render(request, "skeleton/web/dashboard.html", {"organization": organization, "specs": specs, "app_groups": grouped_specs(specs), "organization_id": organization_id, "is_org_admin": is_organization_admin(request.user)})


@tenant_page(admin=True)
def access_list(request, organization_id, resource):
    model, fields = dedicated_model(resource)
    queryset = model.objects.order_by("created_at", "sysid")
    relations = [name for name in fields if model._meta.get_field(name).is_relation]
    if relations:
        queryset = queryset.select_related(*relations)
    rows = pagination(request, queryset)
    rows.object_list = [
        {"sysid": item.sysid, "revision": item.revision,
         "fields": [(model._meta.get_field(name).verbose_name, getattr(item, name))
                    for name in fields]}
        for item in rows
    ]
    return render(request, "skeleton/web/access_list.html", {"title": get_spec(model._meta.label_lower).label, "rows": rows, "organization_id": organization_id, "resource": resource, "is_link": resource in LINKS})


@tenant_page(admin=True)
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
    form = dedicated_form(resource, user=request.user, data=request.POST if request.method == "POST" else None, instance=item)
    status = 200
    if request.method == "POST":
        allowed = set(form.fields) | {"csrfmiddlewaretoken"}
        if set(request.POST) - allowed:
            form.add_error(None, "Campo não permitido.")
            status = 400
        elif form.is_valid():
            try:
                if item:
                    check_html_revision(form, item)
                values = dedicated_input(resource, wire_values(form.cleaned_data), user=request.user, creating=item is None)
                if resource == "invitations":
                    issue_invitation(user=request.user, request=request, **values)
                elif item:
                    name = {"memberships": "update_membership", "roles": "update_role", "groups": "update_group"}[resource]
                    getattr(access_services, name)(item, user=request.user, data=values)
                else:
                    name = {"memberships": "create_membership", "roles": "create_role", "groups": "create_group", "member-roles": "assign_member_role", "group-members": "add_group_member", "group-roles": "assign_group_role", "table-permissions": "grant_table_permission", "record-grants": "create_record_grant"}[resource]
                    getattr(access_services, name)(user=request.user, data=values)
                messages.success(request, "Operação concluída.")
                return redirect("web:access-list", organization_id=organization_id, resource=resource)
            except (ValidationError, IntegrityError, ConcurrentUpdate, LiveReferencesExist) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    return render(request, "skeleton/web/form.html", {"title": "Editar" if item else "Criar", "form": form, "organization_id": organization_id}, status=status)


@tenant_page(admin=True)
@require_http_methods(["GET", "POST"])
@transaction.atomic
def access_delete(request, organization_id, resource, sysid):
    model, _ = dedicated_model(resource)
    item = model.objects.select_for_update().filter(pk=sysid).first()
    if item is None:
        raise Http404
    form = explicit_form(model, (), user=request.user, data=request.POST if request.method == "POST" else None, instance=item, revision=True)
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
                    name = {"memberships": "delete_membership", "roles": "delete_role", "groups": "delete_group"}[resource]
                    getattr(access_services, name)(item, user=request.user)
                return redirect("web:access-list", organization_id=organization_id, resource=resource)
            except (ValidationError, IntegrityError, ConcurrentUpdate, LiveReferencesExist) as exc:
                status = form_error(form, exc)
        else:
            status = 400
    return render(request, "skeleton/web/form.html", {"title": "Revogar / remover", "form": form, "organization_id": organization_id}, status=status)


@tenant_page()
def catalog(request, organization_id):
    specs = presented_specs(request.user)
    return render(request, "skeleton/web/catalog.html", {"title": "Catálogo", "specs": specs, "app_groups": grouped_specs(specs), "organization_id": organization_id})


@tenant_page()
def audit(request, organization_id):
    queryset = authorized_audit_queryset(request.user).order_by("-created_at", "sysid")
    if set(request.GET) - {"page", "record_sysid", "model_label"}:
        raise ValidationError("Filtro não permitido.")
    for name in ("record_sysid", "model_label"):
        if request.GET.get(name):
            queryset = queryset.filter(**{name: request.GET[name]})
    from skeleton.api.serializers import audit_data
    rows = pagination(request, queryset, validated_filters=request.GET.dict())
    rows.object_list = [audit_data(event) for event in rows]
    return render(request, "skeleton/web/audit.html", {"title": "Auditoria", "rows": rows, "organization_id": organization_id})


@tenant_page()
def records(request, organization_id, table_key, deleted=False):
    spec = exposed_spec(table_key)
    queryset = record_queryset(spec, user=request.user, action="restore" if deleted else "read", include_deleted=deleted).order_by("created_at", "sysid")
    if deleted:
        queryset = queryset.filter(deleted_at__isnull=False)
    from skeleton.web.services import filter_records
    queryset = filter_records(queryset, spec, request.GET, ignore_empty=True)
    filters = {name: value for name, value in request.GET.items() if value != ""}
    queryset = queryset.only(*list_fields(spec))
    rows = pagination(request, queryset, validated_filters=filters)
    presentation = list_context(spec, rows.object_list, user=request.user, params=filters)
    return render(request, "skeleton/web/records.html", {
        "title": spec.label or spec.model._meta.verbose_name_plural, "description": spec.description,
        "rows": rows, "organization_id": organization_id, "table_key": table_key,
        "can_create": not deleted and can_action(spec, request.user, "create"),
        "can_restore": can_action(spec, request.user, "restore"), "deleted": deleted,
        "breadcrumbs": record_breadcrumbs(spec, organization_id),
        **presentation,
    })


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
    names = {"update": ("web:record-edit", "Editar"), "delete": ("web:record-delete", "Remover"),
             "manage_access": ("web:record-access", "Gerenciar acesso")}
    actions = [{"url": reverse(route, kwargs={"organization_id": organization_id, "table_key": table_key, "sysid": item.pk}),
                "label": label} for action, (route, label) in names.items() if permissions[action]]
    return render(request, "skeleton/web/detail.html", {
        "record": {"sysid": item.sysid}, "title": safe_record_label(item, spec),
        "actions": actions, "organization_id": organization_id,
        "breadcrumbs": record_breadcrumbs(spec, organization_id),
        "table_key": table_key, **detail_sections(item, spec, user=request.user),
    })


@tenant_page()
@require_http_methods(["GET", "POST"])
@transaction.atomic
def record_edit(request, organization_id, table_key, sysid=None, operation="edit"):
    spec = exposed_spec(table_key)
    item = None
    action = {"edit": "update", "delete": "delete", "restore": "restore", "access": "manage_access"}[operation]
    if sysid:
        item = record_queryset(spec, user=request.user, action=action, include_deleted=operation == "restore").select_for_update().filter(pk=sysid).first()
        if item is None:
            raise Http404
        if operation == "access":
            require_access_management(item, user=request.user)
    else:
        record_queryset(spec, user=request.user, action="create")
    names = ACCESS_FIELDS if operation == "access" else (() if operation in ("delete", "restore") else spec.write_fields)
    if operation == "edit":
        form = record_form(spec, user=request.user, data=request.POST if request.method == "POST" else None, instance=item)
    else:
        form = explicit_form(spec.model, names, user=request.user, data=request.POST if request.method == "POST" else None, instance=item, revision=item is not None)
    status = 200
    if request.method == "POST":
        if set(request.POST) - (set(form.fields) | {"csrfmiddlewaretoken"}):
            form.add_error(None, "Campo não permitido.")
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
    return render(request, "skeleton/web/form.html", {
        "title": {"edit": "Editar" if item else "Criar", "delete": "Remover", "restore": "Restaurar", "access": "Gerenciar acesso"}[operation],
        "form": form, "organization_id": organization_id, "table_key": table_key,
        "cancel_url": reverse(cancel_route, kwargs=cancel_kwargs),
        "record": {"sysid": item.sysid} if item else None, "operation": operation,
        "submit_label": {"edit": "Salvar", "delete": "Remover", "restore": "Restaurar", "access": "Salvar acesso"}[operation],
        "confirmation": (f"Confirmar {'remoção lógica' if operation == 'delete' else 'restauração'} do registro {item.pk}."
                         if operation in {"delete", "restore"} else None),
        "breadcrumbs": record_breadcrumbs(spec, organization_id),
        **form_sections(form, spec, instance=item, user=request.user, operation=operation),
    }, status=status)


@login_required
def platform_catalog(request):
    if not request.user.is_superuser or not request.user.is_active:
        raise PermissionDenied
    if mfa_required(request.user):
        return redirect("mfa_index")
    tables = apps.get_model("sk_system", "TableDefinition").objects.order_by("db_table")
    return render(request, "skeleton/web/platform_catalog.html", {"title": "Inventário físico", "rows": pagination(request, tables)})


@login_required
def platform_audit(request):
    if not request.user.is_superuser or not request.user.is_active:
        raise PermissionDenied
    if mfa_required(request.user):
        return redirect("mfa_index")
    events = apps.get_model("sk_audit", "AuditEvent").objects.filter(organization_id__isnull=True).order_by("-created_at", "sysid")
    from skeleton.api.serializers import audit_data
    rows = pagination(request, events)
    rows.object_list = [audit_data(event) for event in rows]
    return render(request, "skeleton/web/audit.html", {"title": "Auditoria global", "rows": rows})


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
        back_url = reverse("web:record-access" if access else "web:record-edit",
                           kwargs={"organization_id": organization_id, "table_key": table_key, "sysid": item.pk})
    params = request.GET.dict()
    def page_url(number):
        return "?" + urlencode({**params, "page": number})
    return render(request, "skeleton/web/reference_picker.html", {
        "title": "Selecionar referência", "organization_id": organization_id,
        "options": data["results"], "count": data["count"], "back_url": back_url,
        "previous_url": page_url(data["page"] - 1) if data["page"] > 1 else None,
        "next_url": page_url(data["page"] + 1) if data["page"] * data["page_size"] < data["count"] else None,
        "picker_context": {"table_key": table_key, "field_name": field_name,
                           "record": str(item.pk) if item else None},
    })
