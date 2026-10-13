from uuid import uuid4
from django.utils import timezone
from skeleton.tenancy.context import current_context


def register_sqlite_functions(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        functions = {
            "skeleton_actor": lambda: current_context().actor.hex if current_context().actor else None,
            "skeleton_request_id": lambda: current_context().request_id.hex if current_context().request_id else None,
            "skeleton_source": lambda: current_context().source or "sql:unattributed",
            "skeleton_uuid": lambda: uuid4().hex,
            "skeleton_now": lambda: timezone.now().replace(tzinfo=None).isoformat(" "),
        }
        for name, function in functions.items():
            connection.connection.create_function(name, 0, function)
    elif connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            for name in ("user_id", "tenant_id", "request_id", "source", "platform_admin"):
                cursor.execute("SELECT set_config(%s, '', false)", ["skeleton." + name])
