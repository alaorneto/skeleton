from django.apps import apps
from collections.abc import Mapping
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.http import Http404
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from skeleton.access import services as access_services
from skeleton.access.policies import authorized_audit_queryset, is_organization_admin, require_record_action
from skeleton.accounts.mfa import mfa_required
from skeleton.accounts.services import update_profile
from skeleton.api.errors import BoundaryError, parse_revision, require_revision
from skeleton.api.serializers import LINKS, audit_data, dedicated_data, dedicated_input, dedicated_model, profile_data
from skeleton.tenancy.context import current_context
from skeleton.tenancy.invitations import issue_invitation
from skeleton.tenancy.context import memberships_for_user
from skeleton.web.services import change_record_access, create_record, exposed_spec, readable_specs, record_data, record_queryset, require_access_management, update_record


class SessionAPI(APIView):
    authentication_classes = [SessionAuthentication]
    permission_classes = [IsAuthenticated]

    def finalize_response(self, request, response, *args, **kwargs):
        response = super().finalize_response(request, response, *args, **kwargs)
        request_id = current_context().request_id
        if request_id:
            response["X-Request-ID"] = str(request_id)
        return response


class TenantAPI(SessionAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        if current_context().organization_id is None:
            raise Http404
        if is_organization_admin(request.user) and mfa_required(request.user):
            raise BoundaryError("mfa_required", "Configure autenticação multifator para continuar.", 403)


def page(queryset, request, serialize):
    allowed = {"page", "page_size"}
    try:
        number = int(request.query_params.get("page", "1"))
        size = int(request.query_params.get("page_size", "25"))
        if number < 1 or not 1 <= size <= 100:
            raise ValueError
    except ValueError:
        raise BoundaryError("invalid_pagination", "Página ou tamanho inválido.") from None
    offset = (number - 1) * size
    count = queryset.count()
    return Response({"count": count, "page": number, "page_size": size, "results": [serialize(item) for item in queryset[offset:offset + size]]})


def etag(response, *revisions):
    response["ETag"] = '"' + "-".join(str(revision) for revision in revisions) + '"'
    return response


def request_fields(request):
    if not isinstance(request.data, Mapping):
        raise BoundaryError("invalid_request", "Envie um objeto de campos.")
    return dict(request.data.items())


class MeView(SessionAPI):
    def get(self, request):
        profile = apps.get_model("sk_accounts", "UserProfile").objects.get(user=request.user)
        return etag(Response(profile_data(request.user, profile)), request.user.revision, profile.revision)

    @transaction.atomic
    def patch(self, request):
        revisions = parse_revision(request.headers.get("If-Match"), composite=True)
        user, profile = update_profile(user=request.user, data=request_fields(request), user_revision=revisions[0], profile_revision=revisions[1])
        return etag(Response(profile_data(user, profile)), user.revision, profile.revision)


class OrganizationsView(SessionAPI):
    def get(self, request):
        return Response({"results": list(memberships_for_user(request.user))})


class DedicatedView(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        access_services.require_organization_admin(request.user)

    def get(self, request, organization_id, resource, sysid=None):
        model, fields = dedicated_model(resource)
        queryset = model.objects.order_by("created_at", "sysid")
        if sysid:
            record = queryset.filter(pk=sysid).first()
            if record is None:
                raise Http404
            return etag(Response(dedicated_data(resource, record)), record.revision)
        if set(request.query_params) - {"page", "page_size"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        return page(queryset, request, lambda row: dedicated_data(resource, row))

    @transaction.atomic
    def post(self, request, organization_id, resource, sysid=None):
        if sysid:
            raise BoundaryError("method_not_allowed", "Criação exige a URL da coleção.", 405)
        values = dedicated_input(resource, request_fields(request), user=request.user)
        if resource == "invitations":
            record = issue_invitation(user=request.user, request=request, **values)
        else:
            names = {"memberships": "create_membership", "roles": "create_role", "groups": "create_group", "member-roles": "assign_member_role", "group-members": "add_group_member", "group-roles": "assign_group_role", "table-permissions": "grant_table_permission", "record-grants": "create_record_grant"}
            record = getattr(access_services, names[resource])(user=request.user, data=values)
        return etag(Response(dedicated_data(resource, record), status=201), record.revision)

    @transaction.atomic
    def patch(self, request, organization_id, resource, sysid=None):
        if not sysid or resource in LINKS or resource == "invitations":
            raise BoundaryError("method_not_allowed", "Este recurso não aceita atualização.", 405)
        model, fields = dedicated_model(resource)
        with access_services.organization_lock(request.user):
            record = model.objects.select_for_update().filter(pk=sysid).first()
            if record is None:
                raise Http404
            require_revision(request, record)
            values = dedicated_input(resource, request_fields(request), user=request.user, creating=False)
            name = {"memberships": "update_membership", "roles": "update_role", "groups": "update_group"}[resource]
            record = getattr(access_services, name)(record, user=request.user, data=values)
            return etag(Response(dedicated_data(resource, record)), record.revision)

    @transaction.atomic
    def delete(self, request, organization_id, resource, sysid=None):
        if not sysid:
            raise BoundaryError("method_not_allowed", "Selecione o registro.", 405)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Remoção não aceita campos.")
        model, fields = dedicated_model(resource)
        with access_services.organization_lock(request.user):
            record = model.objects.select_for_update().filter(pk=sysid).first()
            if record is None:
                raise Http404
            require_revision(request, record)
            if resource in LINKS:
                access_services.revoke_link(record, user=request.user)
            elif resource == "invitations":
                record.delete()
            else:
                name = {"memberships": "delete_membership", "roles": "delete_role", "groups": "delete_group"}[resource]
                getattr(access_services, name)(record, user=request.user)
            return Response(status=204)


class CatalogView(TenantAPI):
    def get(self, request, organization_id):
        tables = apps.get_model("sk_system", "TableDefinition").objects
        keys = [spec.model._meta.label_lower for spec in readable_specs(request.user)]
        queryset = tables.filter(model_label__in=keys, available=True).order_by("model_label")
        return page(queryset, request, lambda row: {"sysid": row.sysid, "table_key": row.model_label, "label": row.label, "description": row.description, "scope": row.scope})


class AuditView(TenantAPI):
    def get(self, request, organization_id, sysid=None):
        queryset = authorized_audit_queryset(request.user).order_by("-created_at", "sysid")
        if sysid:
            event = queryset.filter(pk=sysid).first()
            if event is None:
                raise Http404
            return Response(audit_data(event))
        if set(request.query_params) - {"page", "page_size", "record_sysid", "model_label"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        for name in ("record_sysid", "model_label"):
            if name in request.query_params:
                try:
                    queryset = queryset.filter(**{name: request.query_params[name]})
                except (ValidationError, ValueError):
                    raise BoundaryError("invalid_filter", "Filtro inválido.") from None
        return page(queryset, request, audit_data)


class RecordsView(TenantAPI):
    def get(self, request, organization_id, table_key, sysid=None):
        spec = exposed_spec(table_key)
        queryset = record_queryset(spec, user=request.user).order_by("created_at", "sysid")
        if sysid:
            record = queryset.filter(pk=sysid).first()
            if record is None:
                raise Http404
            return etag(Response(record_data(record, spec.read_fields, user=request.user)), record.revision)
        from skeleton.web.services import filter_records
        try:
            queryset = filter_records(queryset, spec, request.query_params)
        except ValidationError:
            raise BoundaryError("invalid_filter", "Filtro não permitido ou inválido.") from None
        return page(queryset, request, lambda row: record_data(row, spec.read_fields, user=request.user))

    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid=None):
        if sysid:
            raise BoundaryError("method_not_allowed", "Use a coleção para criar.", 405)
        spec = exposed_spec(table_key)
        record = create_record(spec, user=request.user, data=request_fields(request))
        return etag(Response(record_data(record, spec.read_fields, user=request.user), status=201), record.revision)

    @transaction.atomic
    def patch(self, request, organization_id, table_key, sysid=None):
        if sysid is None:
            raise BoundaryError("method_not_allowed", "Selecione o registro.", 405)
        spec = exposed_spec(table_key)
        record = record_queryset(spec, user=request.user, action="update").select_for_update().filter(pk=sysid).first()
        if record is None:
            raise Http404
        require_revision(request, record)
        record = update_record(record, spec, user=request.user, data=request_fields(request))
        return etag(Response(record_data(record, spec.read_fields, user=request.user)), record.revision)

    @transaction.atomic
    def delete(self, request, organization_id, table_key, sysid=None):
        if sysid is None:
            raise BoundaryError("method_not_allowed", "Selecione o registro.", 405)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Remoção não aceita campos.")
        spec = exposed_spec(table_key)
        record = record_queryset(spec, user=request.user, action="delete").select_for_update().filter(pk=sysid).first()
        if record is None:
            raise Http404
        require_revision(request, record)
        require_record_action(record, user=request.user, action="delete")
        record.delete()
        return Response(status=204)


class RestoreView(TenantAPI):
    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid):
        spec = exposed_spec(table_key)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Restauração não aceita campos.")
        record = record_queryset(spec, user=request.user, action="restore", include_deleted=True).select_for_update().filter(pk=sysid).first()
        if record is None:
            raise Http404
        require_revision(request, record)
        require_record_action(record, user=request.user, action="restore")
        record.restore()
        return etag(Response(record_data(record, spec.read_fields, user=request.user)), record.revision)


class RecordAccessView(TenantAPI):
    def get_record(self, request, table_key, sysid, *, lock=False):
        spec = exposed_spec(table_key)
        queryset = record_queryset(spec, user=request.user, action="manage_access")
        if lock:
            queryset = queryset.select_for_update()
        record = queryset.filter(pk=sysid).first()
        if record is None:
            raise Http404
        require_access_management(record, user=request.user)
        return record

    def get(self, request, organization_id, table_key, sysid):
        record = self.get_record(request, table_key, sysid)
        return etag(Response(record_data(record, ("owner", "visibility", "owning_group"))), record.revision)

    @transaction.atomic
    def patch(self, request, organization_id, table_key, sysid):
        record = self.get_record(request, table_key, sysid, lock=True)
        require_revision(request, record)
        record = change_record_access(record, user=request.user, data=request_fields(request))
        return etag(Response(record_data(record, ("owner", "visibility", "owning_group"))), record.revision)


