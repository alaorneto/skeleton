from django.apps import AppConfig


class FlowsConfig(AppConfig):
    name = "skeleton.flows"
    label = "sk_flows"
    verbose_name = "Fluxos de processo"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registers the flow checks (skeleton.E029)
