from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class TenancyConfig(AppConfig):
    name = "skeleton.tenancy"
    label = "sk_tenancy"
    verbose_name = gettext_lazy("Organizações")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registers the organization site checks (#477)
        from .setting_locks import register_tenancy_providers

        register_tenancy_providers()  # Settings the platform may lock (#701).
        from .provisioning import register_provisioning_jobs

        register_provisioning_jobs()  # Provisioning rules applied to existing organizations (#703).
        from .limit_notices import register_limit_notices

        register_limit_notices()
