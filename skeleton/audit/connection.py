"""Fresh PostgreSQL connections start with empty skeleton.* context settings."""


def reset_context_settings(sender, connection, **kwargs):
    if connection.vendor != "postgresql":
        return  # E025 refuses any other backend; nothing to prepare here.
    with connection.cursor() as cursor:
        for name in ("user_id", "tenant_id", "request_id", "source", "platform_admin"):
            cursor.execute("SELECT set_config(%s, '', false)", ["skeleton." + name])
