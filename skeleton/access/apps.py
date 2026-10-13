from django.apps import AppConfig


class AccessConfig(AppConfig):
    name = "skeleton.access"
    label = "sk_access"
    verbose_name = "Controle de acesso"
    default_auto_field = "django.db.models.BigAutoField"
