from django.apps import AppConfig
from django.db.models.signals import post_migrate


class SystemConfig(AppConfig):
    name = "skeleton.system"
    label = "sk_system"
    verbose_name = "Catálogo de sistema"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .registry import register_core_specs
        register_core_specs()
        from . import checks
        from .synchronization import post_migrate_catalog
        post_migrate.connect(post_migrate_catalog, sender=self, dispatch_uid="skeleton.sync.catalog")
