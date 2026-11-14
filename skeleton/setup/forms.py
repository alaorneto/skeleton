"""Setup forms; credentials use password inputs that never render their value back."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext, gettext_lazy


class TokenForm(forms.Form):
    token = forms.CharField(label=gettext_lazy("Token de primeiro acesso"), max_length=200, strip=True)


class InstanceForm(forms.Form):
    allowed_hosts = forms.CharField(
        label=gettext_lazy("Hosts aceitos"),
        max_length=500,
        help_text=gettext_lazy("Separados por vírgula, por exemplo app.example.com."),
    )
    csrf_trusted_origins = forms.CharField(
        label=gettext_lazy("Origens confiáveis (CSRF)"),
        max_length=500,
        required=False,
        help_text=gettext_lazy("Opcional: https://app.example.com, separadas por vírgula."),
    )
    public_url = forms.CharField(
        label=gettext_lazy("Origem pública"),
        max_length=200,
        required=False,
        help_text=gettext_lazy("Usada nos links dos emails."),
    )
    trust_proxy = forms.BooleanField(
        label=gettext_lazy("Atrás de um proxy reverso que termina o HTTPS"), required=False
    )

    def __init__(self, *args, **kwargs):
        from skeleton.instance.schema import CATALOG

        super().__init__(*args, **kwargs)
        if "public_url" not in CATALOG:  # The key arrives with the notifications epic (#54).
            del self.fields["public_url"]


class DatabaseForm(forms.Form):
    database_url = forms.CharField(
        label=gettext_lazy("URL da role runtime"),
        max_length=500,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy(
            "postgres://skeleton_runtime:senha@host:5432/banco. Sem superuser, sem BYPASSRLS, sem ser dona do schema."
        ),
    )


class SchemaForm(forms.Form):
    owner_url = forms.CharField(
        label=gettext_lazy("URL da role de migração (dona do schema)"),
        max_length=500,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("Usada só nesta requisição para migrar; nunca é gravada."),
    )


REDIS_CACHE = "django.core.cache.backends.redis.RedisCache"


def production_target() -> bool:
    """Whether the instance starts with production rules after the setup (#457); unknown is yes."""
    from django.conf import settings

    return getattr(settings, "SKELETON_SETUP_PRODUCTION", True)


class EmailForm(forms.Form):
    email_url = forms.CharField(
        label=gettext_lazy("Servidor de email"),
        max_length=500,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("smtp+tls://usuário:senha@smtp.example.com:587"),
    )
    default_from_email = forms.EmailField(label=gettext_lazy("Remetente padrão"))
    test_recipient = forms.EmailField(label=gettext_lazy("Enviar email de teste para"))

    def clean_email_url(self):
        # Messages never echo the value: the URL carries the SMTP password. Other parser refusals
        # (unknown scheme or parameter) stay with the view, which reports names only.
        value = self.cleaned_data["email_url"]
        if not production_target():
            return value
        from django.core.exceptions import ImproperlyConfigured

        from skeleton.conf.mailers import SMTP, mailer_from_url

        try:
            backend = mailer_from_url(value).get("BACKEND")
        except (ImproperlyConfigured, ValueError):
            return value
        if backend != SMTP:
            raise forms.ValidationError(
                gettext("Em produção o email precisa de um servidor SMTP: smtp+tls://, smtp+ssl:// ou smtp://.")
            )
        return value


class StorageForm(forms.Form):
    cache_url = forms.CharField(
        label=gettext_lazy("Cache compartilhado (Redis)"),
        max_length=500,
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy(
            "redis://:senha@redis:6379/0. Obrigatório em produção; vazio mantém o cache local, só no runserver de"
            " desenvolvimento."
        ),
    )
    files_backend = forms.ChoiceField(
        label=gettext_lazy("Armazenamento de anexos"), choices=(("local", gettext_lazy("Local")), ("s3", "S3"))
    )
    files_root = forms.CharField(label=gettext_lazy("Pasta local dos anexos"), max_length=500, required=False)
    files_s3_bucket = forms.CharField(label="Bucket S3", max_length=200, required=False)
    files_s3_region = forms.CharField(label=gettext_lazy("Região S3"), max_length=100, required=False)
    files_s3_endpoint = forms.CharField(label="Endpoint S3", max_length=500, required=False)

    def clean_cache_url(self):
        # Messages never echo the value: the URL carries the Redis password.
        value = self.cleaned_data["cache_url"]
        if not value:
            if production_target():
                raise forms.ValidationError(
                    gettext("Em produção o cache precisa de um Redis compartilhado: redis://:senha@redis:6379/0.")
                )
            return value
        try:
            import environ  # type: ignore[import-untyped]

            backend = environ.Env.cache_url_config(value).get("BACKEND")
        except Exception:  # noqa: BLE001 - any parser failure is the same refusal, without the value
            raise forms.ValidationError(gettext("URL de cache inválida.")) from None
        if production_target() and backend != REDIS_CACHE:
            raise forms.ValidationError(
                gettext("Em produção o cache precisa de um Redis compartilhado: redis://:senha@redis:6379/0.")
            )
        return value

    def clean(self):
        data = super().clean()
        if data.get("files_backend") == "s3" and not data.get("files_s3_bucket"):
            self.add_error("files_s3_bucket", gettext("Informe o bucket."))
        if data.get("files_backend") == "local" and not data.get("files_root"):
            self.add_error("files_root", gettext("Informe a pasta local."))
        return data


class AdminForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("Email do administrador da plataforma"))
    password1 = forms.CharField(label=gettext_lazy("Senha"), widget=forms.PasswordInput(render_value=False))
    password2 = forms.CharField(label=gettext_lazy("Confirme a senha"), widget=forms.PasswordInput(render_value=False))
    organization_name = forms.CharField(label=gettext_lazy("Nome da primeira organização"), max_length=200)
    organization_slug = forms.SlugField(label=gettext_lazy("Identificador da organização"), max_length=80)

    def clean(self):
        data = super().clean()
        if data.get("password1") and data.get("password1") != data.get("password2"):
            self.add_error("password2", gettext("As senhas não conferem."))
        elif data.get("password1"):
            try:
                validate_password(data["password1"])
            except forms.ValidationError as error:
                self.add_error("password1", error)
        return data
