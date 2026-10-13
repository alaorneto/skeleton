from django.apps import AppConfig
from django.db.backends.signals import connection_created


class AuditConfig(AppConfig):
    name = "skeleton.audit"
    label = "sk_audit"
    verbose_name = "Auditoria"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .sqlite import register_sqlite_functions

        connection_created.connect(register_sqlite_functions, dispatch_uid="skeleton.audit.context_functions", weak=False)
