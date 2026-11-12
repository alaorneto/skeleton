"""Setup forms; credentials use password inputs that never render their value back."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext, gettext_lazy


class TokenForm(forms.Form):
    token = forms.CharField(label=gettext_lazy("Token de primeiro acesso"), max_length=200, strip=True)


class InstanceForm(forms.Form):
    allowed_hosts = forms.CharField(
        label="Hosts aceitos",
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


class EmailForm(forms.Form):
    email_url = forms.CharField(
        label="Servidor de email",
        max_length=500,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("smtp+tls://usuário:senha@smtp.example.com:587"),
    )
    default_from_email = forms.EmailField(label=gettext_lazy("Remetente padrão"))
    test_recipient = forms.EmailField(label=gettext_lazy("Enviar email de teste para"))


class StorageForm(forms.Form):
    cache_url = forms.CharField(
        label="Cache compartilhado (Redis)",
        max_length=500,
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy("redis://redis:6379/0. Vazio mantém o cache local (só desenvolvimento)."),
    )
    files_backend = forms.ChoiceField(label="Armazenamento de anexos", choices=(("local", "Local"), ("s3", "S3")))
    files_root = forms.CharField(label=gettext_lazy("Pasta local dos anexos"), max_length=500, required=False)
    files_s3_bucket = forms.CharField(label="Bucket S3", max_length=200, required=False)
    files_s3_region = forms.CharField(label=gettext_lazy("Região S3"), max_length=100, required=False)
    files_s3_endpoint = forms.CharField(label="Endpoint S3", max_length=500, required=False)

    def clean(self):
        data = super().clean()
        if data.get("files_backend") == "s3" and not data.get("files_s3_bucket"):
            self.add_error("files_s3_bucket", gettext("Informe o bucket."))
        if data.get("files_backend") == "local" and not data.get("files_root"):
            self.add_error("files_root", gettext("Informe a pasta local."))
        return data


class AdminForm(forms.Form):
    email = forms.EmailField(label=gettext_lazy("Email do administrador da plataforma"))
    password1 = forms.CharField(label="Senha", widget=forms.PasswordInput(render_value=False))
    password2 = forms.CharField(label="Confirme a senha", widget=forms.PasswordInput(render_value=False))
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
