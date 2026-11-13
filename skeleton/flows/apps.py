from django.apps import AppConfig
from django.utils.translation import gettext_lazy


class FlowsConfig(AppConfig):
    name = "skeleton.flows"
    label = "sk_flows"
    verbose_name = gettext_lazy("Fluxos de processo")
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from skeleton.integrations.tokens.scopes import register_token_scope

        from . import checks  # noqa: F401 - registers the flow checks (skeleton.E029)
        from .api_views import FLOWS_READ, FLOWS_WRITE
        from .automatic import register_automatic_steps
        from .events import register_flow_event_types
        from .notifications import register_flow_notification_types
        from .tasks import register_human_steps

        register_token_scope(FLOWS_READ, gettext_lazy("Ler tarefas, instâncias e histórico de fluxos"))
        register_token_scope(FLOWS_WRITE, gettext_lazy("Agir em tarefas e instâncias de fluxos"))
        register_flow_event_types()
        from .event_waits import register_event_waits

        register_event_waits()
        register_automatic_steps()
        from .subflows import register_subflows

        register_subflows()
        from .migration import register_migration_job

        register_migration_job()
        register_human_steps()
        register_flow_notification_types()
