from django.apps import AppConfig


class InstanceConfig(AppConfig):
    """Only hosts the ``instance`` management command; the store itself has no models."""

    name = "skeleton.instance"
    label = "sk_instance"
    verbose_name = "Configuração da instância"

    def ready(self):
        from . import checks  # noqa: F401 - registers skeleton.W036
