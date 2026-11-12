from django.apps import AppConfig
from django.db.backends.signals import connection_created
from django.utils.translation import gettext_lazy


class AuditConfig(AppConfig):
    name = "skeleton.audit"
    label = "sk_audit"
    verbose_name = gettext_lazy("Auditoria")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .connection import reset_context_settings

        connection_created.connect(reset_context_settings, dispatch_uid="skeleton.audit.context_functions", weak=False)
