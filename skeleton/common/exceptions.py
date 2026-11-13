from django.core.exceptions import PermissionDenied, ValidationError


class RecordError(Exception):
    """A record operation violated an explicit application invariant."""


class MissingTenantContext(RecordError):
    pass


class ConcurrentUpdate(RecordError):
    pass


class UnsafeBulkMutation(RecordError):
    pass


class LiveReferencesExist(RecordError):
    pass


class ImmutableRecord(RecordError):
    pass


class ApplicationNotEntitled(ValidationError):
    """Activation refused because the organization has not contracted a paid app (#395)."""


class OrganizationAccessLimited(PermissionDenied):
    """The organization's billing standing forbids this operation (#395); ``code`` says how."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


class ApplicationUnavailable(PermissionDenied):
    """The platform withdrew the app from this organization; surfaces answer 404, not 403."""
