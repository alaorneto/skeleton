"""Fresh PostgreSQL connections start with empty skeleton.* context settings."""

_NAMES = ("user_id", "tenant_id", "request_id", "source", "platform_admin", "platform_auditor")
_RESET = "SELECT " + ", ".join(f"set_config('skeleton.{name}', '', false)" for name in _NAMES)


def reset_context_settings(sender, connection, **kwargs):
    if connection.vendor != "postgresql":
        return  # E025 refuses any other backend; nothing to prepare here.
    with connection.cursor() as cursor:
        cursor.execute(_RESET)  # One statement per new connection (#237).
