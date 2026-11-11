from collections.abc import Mapping

from django.apps import apps
from django.core.exceptions import ValidationError
from django.db import transaction
from django.http import Http404
from rest_framework.authentication import SessionAuthentication
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from skeleton.access import services as access_services
from skeleton.access.capabilities import has_any_capability, require_resource_capability, resource_capability
from skeleton.accounts.mfa import mfa_required
from skeleton.api.errors import BoundaryError, parse_revision, require_revision
from skeleton.api.serializers import LINKS, audit_data, dedicated_data, dedicated_input, dedicated_model
from skeleton.tenancy.context import current_context
from skeleton.tenancy.invitations import issue_invitation
from skeleton.web.services import (
    change_record_access,
    create_record,
    delete_record,
    exposed_spec,
    readable_specs,
    record_data,
    record_queryset,
    records_data,
    require_access_management,
    restore_record,
    update_record,
)


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
        if has_any_capability(request.user) and mfa_required(request.user):
            raise BoundaryError("mfa_required", "Configure autenticação multifator para continuar.", 403)


def page_numbers(request):
    try:
        number = int(request.query_params.get("page", "1"))
        size = int(request.query_params.get("page_size", "25"))
        if number < 1 or not 1 <= size <= 100:
            raise ValueError
    except ValueError:
        raise BoundaryError("invalid_pagination", "Página ou tamanho inválido.") from None
    return number, size


def page(queryset, request, serialize=None, *, serialize_many=None):
    number, size = page_numbers(request)
    offset = (number - 1) * size
    count = queryset.count()
    items = queryset[offset : offset + size]
    results = serialize_many(items) if serialize_many is not None else [serialize(item) for item in items]
    return Response({"count": count, "page": number, "page_size": size, "results": results})


def etag(response, *revisions):
    response["ETag"] = '"' + "-".join(str(revision) for revision in revisions) + '"'
    return response


def request_fields(request):
    if not isinstance(request.data, Mapping):
        raise BoundaryError("invalid_request", "Envie um objeto de campos.")
    return dict(request.data.items())


def me_data(me):
    """REST shape of the actor's identity (unchanged); built from IdentityService's DTO."""
    return {
        "sysid": me.sysid,
        "email": me.email,
        "first_name": me.first_name,
        "last_name": me.last_name,
        "revision": me.user_revision,
        "profile": {
            "sysid": me.profile_sysid,
            "revision": me.revision,
            "display_name": me.profile_display_name,
            "timezone": me.timezone,
            "language": me.language,
        },
    }


class MeView(SessionAPI):
    def get(self, request):
        from skeleton.service_api.identity import IdentityService

        me = IdentityService().me(actor=request.user)
        return etag(Response(me_data(me)), me.user_revision, me.revision)

    @transaction.atomic
    def patch(self, request):
        from skeleton.api.errors import if_match_precondition
        from skeleton.service_api.identity import IdentityService

        revisions = parse_revision(request.headers.get("If-Match"), composite=True)
        with if_match_precondition():
            me = IdentityService().update_my_profile(
                actor=request.user,
                expected_user_revision=revisions[0],
                expected_revision=revisions[1],
                changes=tuple(request_fields(request).items()),
            )
        return etag(Response(me_data(me)), me.user_revision, me.revision)


class OrganizationsView(SessionAPI):
    def get(self, request):
        from skeleton.service_api.organizations import OrganizationService

        return Response(
            {
                "results": [
                    organization_data(item) for item in OrganizationService().my_organizations(actor=request.user)
                ]
            }
        )


def organization_data(item):
    """REST shape of one membership of the actor (unchanged)."""
    return {
        "sysid": item.membership_id,
        "organization_id": item.sysid,
        "is_active": True,
        "organization_name": item.name,
        "organization_slug": item.slug,
    }


def own_membership_data(membership):
    return {
        "sysid": membership.sysid,
        "organization_id": membership.organization_id,
        "is_active": membership.is_active,
        "revision": membership.revision,
    }


class OwnMembershipView(TenantAPI):
    """The actor's own membership in this organization; its ETag is what leaving requires."""

    def get(self, request, organization_id):
        from skeleton.tenancy.models import Membership

        membership = Membership.objects.filter(user=request.user, is_active=True).first()
        if membership is None:
            raise Http404
        return etag(Response(own_membership_data(membership)), membership.revision)


class LeaveOrganizationView(TenantAPI):
    """``POST`` with ``If-Match`` of the membership revision: the actor leaves the organization."""

    @transaction.atomic
    def post(self, request, organization_id):
        if request_fields(request):
            raise BoundaryError("invalid_request", "Sair da organização não aceita campos.")
        revision = parse_revision(request.headers.get("If-Match"))
        access_services.leave_organization(request.user, expected_revision=revision)
        return Response(status=204)


class DedicatedView(TenantAPI):
    def check_permissions(self, request):
        super().check_permissions(request)
        self.capability = resource_capability(self.kwargs.get("resource", ""), method=request.method)
        require_resource_capability(request.user, self.capability)

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
            names = {
                "memberships": "create_membership",
                "roles": "create_role",
                "groups": "create_group",
                "member-roles": "assign_member_role",
                "group-members": "add_group_member",
                "group-roles": "assign_group_role",
                "table-permissions": "grant_table_permission",
                "record-grants": "create_record_grant",
            }
            record = getattr(access_services, names[resource])(user=request.user, data=values)
        return etag(Response(dedicated_data(resource, record), status=201), record.revision)

    @transaction.atomic
    def patch(self, request, organization_id, resource, sysid=None):
        if not sysid or resource in LINKS or resource == "invitations":
            raise BoundaryError("method_not_allowed", "Este recurso não aceita atualização.", 405)
        model, fields = dedicated_model(resource)
        with access_services.organization_lock(request.user, capability=self.capability):
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
        with access_services.organization_lock(request.user, capability=self.capability):
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
        return page(
            queryset,
            request,
            lambda row: {
                "sysid": row.sysid,
                "table_key": row.model_label,
                "label": row.label,
                "description": row.description,
                "scope": row.scope,
            },
        )


class AuditView(TenantAPI):
    def get(self, request, organization_id, sysid=None):
        from uuid import UUID

        from skeleton.service_api.audit import AuditLogService

        service = AuditLogService()
        if sysid:
            return Response(audit_data(service.event(actor=request.user, event_id=sysid)))
        if set(request.query_params) - {"page", "page_size", "record_sysid", "model_label"}:
            raise BoundaryError("invalid_filter", "Filtro não permitido.")
        record_id = request.query_params.get("record_sysid")
        if record_id is not None:
            try:
                record_id = UUID(record_id)
            except ValueError:
                raise BoundaryError("invalid_filter", "Filtro inválido.") from None
        number, size = page_numbers(request)
        log = service.events(
            actor=request.user,
            model_label=request.query_params.get("model_label"),
            record_id=record_id,
            page=number,
            page_size=size,
        )
        return Response(
            {
                "count": log.count,
                "page": log.page,
                "page_size": log.page_size,
                "results": [audit_data(e) for e in log.items],
            }
        )


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
        return page(
            queryset, request, serialize_many=lambda rows: records_data(rows, spec.read_fields, user=request.user)
        )

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
        delete_record(record, user=request.user)
        return Response(status=204)


class RestoreView(TenantAPI):
    @transaction.atomic
    def post(self, request, organization_id, table_key, sysid):
        spec = exposed_spec(table_key)
        if request_fields(request):
            raise BoundaryError("invalid_request", "Restauração não aceita campos.")
        record = (
            record_queryset(spec, user=request.user, action="restore", include_deleted=True)
            .select_for_update()
            .filter(pk=sysid)
            .first()
        )
        if record is None:
            raise Http404
        require_revision(request, record)
        restore_record(record, user=request.user)
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


class ReferenceView(TenantAPI):
    def get(self, request, organization_id, table_key, field_name):
        from skeleton.web.references import reference_request

        spec = exposed_spec(table_key)
        result, _, _ = reference_request(spec, field_name, user=request.user, params=request.query_params)
        return Response(result)
