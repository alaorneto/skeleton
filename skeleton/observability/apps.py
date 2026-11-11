from django.apps import AppConfig


class ObservabilityConfig(AppConfig):
    """Starts tracing when configured; metrics start with their middleware."""

    name = "skeleton.observability"
    label = "sk_observability"
    verbose_name = "Observabilidade"

    def ready(self) -> None:
        from . import checks  # noqa: F401 - registers the checks
        from .tracing import configure_tracing

        # Before the WSGI handler loads the middleware: the Django instrumentation inserts its own.
        configure_tracing()
