from django.apps import AppConfig
from django.db.models.signals import post_migrate
from django.utils.translation import gettext_lazy


class SystemConfig(AppConfig):
    name = "skeleton.system"
    label = "sk_system"
    verbose_name = gettext_lazy("Catálogo de sistema")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from .registry import register_core_specs

        register_core_specs()
        from . import checks  # noqa: F401 - registra os system checks
        from .synchronization import post_migrate_catalog

        post_migrate.connect(post_migrate_catalog, sender=self, dispatch_uid="skeleton.sync.catalog")
