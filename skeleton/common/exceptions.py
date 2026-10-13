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
