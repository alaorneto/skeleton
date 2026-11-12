from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class FlowsConfig(AppConfig):
    name = "skeleton.flows"
    label = "sk_flows"
    verbose_name = gettext_lazy("Fluxos de processo")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from . import checks  # noqa: F401 - registers the flow checks (skeleton.E029)
        from .automatic import register_automatic_steps
        from .events import register_flow_event_types
        from .notifications import register_flow_notification_types
        from .tasks import register_human_steps

        register_flow_event_types()
        register_automatic_steps()
        register_human_steps()
        register_flow_notification_types()
