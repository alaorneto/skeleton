from django.core.exceptions import PermissionDenied, ValidationError


class RecordError(Exception):
    """A record operation violated an explicit application invariant."""


class MissingTenantContext(RecordError):
    pass


class SystemContextRequired(RecordError):
    """A global table declared by an app is written only in a system context without organization (#576)."""


class ConcurrentUpdate(RecordError):
    pass


class UnsafeBulkMutation(RecordError):
    pass


class LiveReferencesExist(RecordError):
    pass


class ImmutableRecord(RecordError):
    pass


class RecordSealed(RecordError):
    """A sealed record (or a child of one) cannot change its frozen columns (#526); surfaces answer 409."""


class ApplicationNotEntitled(ValidationError):
    """Activation refused because the organization has not contracted a paid app (#395)."""


class OrganizationAccessLimited(PermissionDenied):
    """The organization's billing standing forbids this operation (#395); ``code`` says how."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class QuotaLimitExceeded(ValidationError):
    """The limit of a metric would be passed (#398, #695); surfaces answer 409 ``quota_exceeded``.

    The message is the one a person sees (#698): for ``members``, that the organization has no
    seats; for another metric, which limit was reached.
    """

    def __init__(self, metric, limit):
        from django.utils.translation import gettext

        self.metric = metric
        self.limit = limit
        if metric == "members":
            message = gettext("Esta organização não tem vagas no momento. Fale com o administrador.")
        else:
            message = gettext("A organização atingiu o limite de %(metric)s (%(limit)s).") % {
                "metric": metric,
                "limit": limit,
            }
        super().__init__({"__all__": ValidationError(message, code="quota_exceeded")})


class ApplicationUnavailable(PermissionDenied):
    """The platform withdrew the app from this organization; surfaces answer 404, not 403."""
