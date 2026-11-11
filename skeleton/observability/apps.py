from django.apps import AppConfig


class ObservabilityConfig(AppConfig):
    """Registers the observability checks; metrics start with their middleware."""

    name = "skeleton.observability"
    label = "sk_observability"
    verbose_name = "Observabilidade"

    def ready(self) -> None:
        from . import checks  # noqa: F401 - registers the checks
