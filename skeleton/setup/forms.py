"""Setup forms; credentials use password inputs that never render their value back."""

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext, gettext_lazy


class TokenForm(forms.Form):
    token = forms.CharField(label=gettext_lazy("Token de primeiro acesso"), max_length=200, strip=True)


class InstanceForm(forms.Form):
    allowed_hosts = forms.CharField(
        label=gettext_lazy("Endereços (hosts) aceitos"),
        max_length=500,
        help_text=gettext_lazy(
            "Obrigatório. Só o nome, sem http:// e sem porta. Separe vários com vírgula. Exemplo:"
            " app.exemplo.com.br. Para testar nesta máquina: localhost,127.0.0.1. Pedidos para qualquer outro"
            " endereço são recusados."
        ),
    )
    public_url = forms.CharField(
        label=gettext_lazy("Endereço público (URL)"),
        max_length=200,
        required=False,
        help_text=gettext_lazy(
            "Opcional, mas recomendado. Endereço completo que as pessoas usam, com https:// e sem caminho."
            " Exemplo: https://app.exemplo.com.br. É usado para montar os links enviados por e-mail e nas"
            " notificações."
        ),
    )
    csrf_trusted_origins = forms.CharField(
        label=gettext_lazy("Origens confiáveis para formulários (CSRF)"),
        max_length=500,
        required=False,
        help_text=gettext_lazy(
            "Opcional. Deixe em branco na maioria dos casos: se o endereço público acima foi informado, ele já é"
            " usado. Preencha só quando outro domínio também envia formulários ao sistema: origens completas com"
            " https://, separadas por vírgula."
        ),
    )
    trust_proxy = forms.BooleanField(
        label=gettext_lazy("Há um proxy reverso na frente recebendo o HTTPS"),
        required=False,
        help_text=gettext_lazy(
            "Marque se Nginx, Traefik, Caddy, Coolify ou um balanceador recebe o HTTPS e repassa os pedidos ao"
            " sistema. Deixe desmarcado se o navegador fala direto com este processo (por exemplo, no runserver"
            " local). Marcar sem ter proxy permite forjar o endereço de quem acessa."
        ),
    )

    def __init__(self, *args, **kwargs):
        from skeleton.instance.schema import CATALOG

        super().__init__(*args, **kwargs)
        if "public_url" not in CATALOG:  # The key arrives with the notifications epic (#54).
            del self.fields["public_url"]


FILL_IN = gettext_lazy("Informe este campo.")


class DatabaseForm(forms.Form):
    """Runtime role of the database by separate fields; the full URL only under "Avançado" (#464)."""

    advanced_fields = ("database_url",)

    db_host = forms.CharField(
        label=gettext_lazy("Servidor (host)"),
        max_length=255,
        required=False,
        help_text=gettext_lazy(
            "Nome ou IP do servidor PostgreSQL, como este processo o enxerga. No Docker Compose deste projeto:"
            " postgres. Na mesma máquina, fora do Docker: 127.0.0.1."
        ),
    )
    db_port = forms.IntegerField(
        label=gettext_lazy("Porta"),
        min_value=1,
        max_value=65535,
        initial=5432,
        required=False,
        help_text=gettext_lazy("O padrão do PostgreSQL é 5432."),
    )
    db_name = forms.CharField(
        label=gettext_lazy("Nome do banco"),
        max_length=63,
        required=False,
        help_text=gettext_lazy(
            "O banco criado para o sistema. Ele precisa existir e ter sido preparado com deploy/postgres/bootstrap.sql."
        ),
    )
    db_user = forms.CharField(
        label=gettext_lazy("Usuário (role de aplicação)"),
        max_length=63,
        required=False,
        initial="skeleton_runtime",
        help_text=gettext_lazy(
            "A role que o sistema usa no dia a dia. O bootstrap cria skeleton_runtime. Não pode ser superusuário"
            " nem dona do schema."
        ),
    )
    db_password = forms.CharField(
        label=gettext_lazy("Senha dessa role"),
        max_length=500,
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy("A senha definida com \\password skeleton_runtime. Pode conter qualquer caractere."),
    )
    database_url = forms.CharField(
        label=gettext_lazy("URL completa (avançado)"),
        max_length=500,
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy(
            "Só se precisar de parâmetros extras (por exemplo sslmode). Quando preenchida, os campos acima são"
            " ignorados. Formato: postgres://usuário:senha@host:5432/banco, com a senha em percent-encoding."
        ),
    )
    # Marked as required on the page: they are, unless the advanced URL is used; the HTML
    # attribute stays off so the browser does not block a submission with the URL.
    separate_required = ("db_host", "db_port", "db_name", "db_user", "db_password")

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.separate_required:
            self.fields[name].marked_required = True

    def clean(self):
        from .database import DEFAULT_PORT, build_dsn

        data = super().clean()
        if data.get("database_url"):
            data["dsn"] = data["database_url"]
            return data
        missing = [name for name in self.separate_required if name != "db_port" and not data.get(name)]
        for name in missing:
            if name not in self.errors:
                self.add_error(name, FILL_IN)
        if not missing and not self.errors:
            data["dsn"] = build_dsn(
                host=data["db_host"],
                port=data.get("db_port") or DEFAULT_PORT,
                name=data["db_name"],
                user=data["db_user"],
                password=data["db_password"],
            )
        return data


class SchemaForm(forms.Form):
    """The schema owner, only user and password: host, port and database come from the runtime step."""

    advanced_fields = ("owner_url",)
    separate_required = ("owner_user", "owner_password")

    owner_user = forms.CharField(
        label=gettext_lazy("Usuário dono do schema"),
        max_length=63,
        required=False,
        initial="skeleton_owner",
        help_text=gettext_lazy("A role que cria e altera as tabelas. O bootstrap cria skeleton_owner."),
    )
    owner_password = forms.CharField(
        label=gettext_lazy("Senha dessa role"),
        max_length=500,
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy("Usada só agora, para criar as tabelas. Não é gravada em lugar nenhum."),
    )
    owner_url = forms.CharField(
        label=gettext_lazy("URL completa (avançado)"),
        max_length=500,
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy(
            "Só se precisar de parâmetros extras. Quando preenchida, os campos acima são ignorados. Precisa apontar"
            " para o mesmo banco da etapa anterior. Usada só nesta requisição; nunca é gravada."
        ),
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.separate_required:
            self.fields[name].marked_required = True

    def clean(self):
        data = super().clean()
        if not data.get("owner_url"):
            for name in self.separate_required:
                if not data.get(name) and name not in self.errors:
                    self.add_error(name, FILL_IN)
        return data


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


def redis_url(location: str, password: str) -> str:
    """The cache URL from the address and a separate password, percent-encoded after ``://`` (#463)."""
    from urllib.parse import quote

    if not password:
        return location
    scheme, separator, rest = location.partition("://")
    if not separator:
        return location
    return f"{scheme}://:{quote(password, safe='')}@{rest}"


class StorageForm(forms.Form):
    cache_location = forms.CharField(
        label=gettext_lazy("Endereço do Redis"),
        max_length=500,
        required=False,
        widget=forms.TextInput(attrs={"placeholder": "redis://redis:6379/0", "autocomplete": "off"}),
    )
    cache_password = forms.CharField(
        label=gettext_lazy("Senha do Redis"),
        max_length=500,
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy("Em branco se o Redis não pede senha."),
    )
    files_backend = forms.ChoiceField(
        label=gettext_lazy("Onde guardar os anexos"),
        choices=(
            ("local", gettext_lazy("Pasta neste servidor")),
            ("s3", gettext_lazy("Armazenamento S3 ou compatível (MinIO, R2, Spaces)")),
        ),
        initial="local",
        widget=forms.RadioSelect,
    )
    files_root = forms.CharField(
        label=gettext_lazy("Pasta dos anexos"),
        max_length=500,
        required=False,
        help_text=gettext_lazy(
            "Obrigatório se escolheu “Pasta neste servidor”. Caminho completo de uma pasta que o sistema pode"
            " gravar e que não é servida ao público. Em Docker, precisa ser um volume. O setup cria a pasta e testa"
            " a gravação."
        ),
    )
    files_s3_bucket = forms.CharField(
        label=gettext_lazy("Bucket S3"),
        max_length=200,
        required=False,
        help_text=gettext_lazy("Obrigatório se escolheu S3. Só o nome do bucket."),
    )
    files_s3_region = forms.CharField(
        label=gettext_lazy("Região S3"),
        max_length=100,
        required=False,
        help_text=gettext_lazy(
            "Opcional. Na AWS, por exemplo sa-east-1. Em serviços compatíveis, costuma ficar em branco."
        ),
    )
    files_s3_endpoint = forms.CharField(
        label=gettext_lazy("Endpoint S3"),
        max_length=500,
        required=False,
        help_text=gettext_lazy(
            "Opcional. Em branco para a AWS. Para MinIO, R2 ou Spaces, o endereço completo com https://."
        ),
    )

    def __init__(self, *args, strict: bool | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.strict = production_target() if strict is None else strict
        location = self.fields["cache_location"]
        location.required = self.strict
        location.help_text = (
            gettext_lazy(
                "Obrigatório em produção. Formato: redis://host:6379/0. No Docker Compose deste projeto o host é redis."
            )
            if self.strict
            else gettext_lazy(
                "Opcional neste modo. Em branco, cada processo usa um cache próprio em memória (serve só para testar"
                " numa máquina)."
            )
        )

    def clean_cache_location(self):
        # Messages never echo the value: an address could still carry a password.
        value = self.cleaned_data["cache_location"].strip()
        if "@" in value:
            raise forms.ValidationError(gettext("Informe a senha no campo ao lado, não dentro do endereço."))
        return value

    def clean(self):
        data = super().clean()
        location = data.get("cache_location")
        data["cache_url"] = ""
        if location:
            url = redis_url(location, data.get("cache_password") or "")
            try:
                import environ  # type: ignore[import-untyped]

                backend = environ.Env.cache_url_config(url).get("BACKEND")
            except Exception:  # noqa: BLE001 - any parser failure is the same refusal, without the value
                self.add_error("cache_location", gettext("Endereço de cache inválido."))
            else:
                if self.strict and backend != REDIS_CACHE:
                    self.add_error(
                        "cache_location",
                        gettext("Em produção o cache precisa de um Redis compartilhado: redis://redis:6379/0."),
                    )
                else:
                    data["cache_url"] = url
        if data.get("files_backend") == "s3" and not data.get("files_s3_bucket"):
            self.add_error("files_s3_bucket", gettext("Informe o bucket."))
        if data.get("files_backend") == "local" and not data.get("files_root"):
            self.add_error("files_root", gettext("Informe a pasta local."))
        return data


class AdminForm(forms.Form):
    email = forms.EmailField(
        label=gettext_lazy("E-mail do administrador da plataforma"),
        help_text=gettext_lazy(
            "É o login da primeira conta, com acesso total à administração da plataforma. Use um e-mail real: a"
            " recuperação de senha chega nele."
        ),
    )
    password1 = forms.CharField(
        label=gettext_lazy("Senha"),
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
    )
    password2 = forms.CharField(
        label=gettext_lazy("Confirme a senha"),
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy("Digite a mesma senha de novo."),
    )
    organization_name = forms.CharField(
        label=gettext_lazy("Nome da primeira organização"),
        max_length=200,
        help_text=gettext_lazy(
            "Nome de exibição da empresa, órgão ou equipe. Pode ser alterado depois nas configurações da"
            " organização. Exemplo: Prefeitura de Exemplo."
        ),
    )
    organization_slug = forms.SlugField(
        label=gettext_lazy("Identificador da organização"),
        max_length=80,
        help_text=gettext_lazy(
            "Apelido técnico, usado em integrações: só letras minúsculas, números e hífen, sem espaços nem acentos."
            " Exemplo: prefeitura-exemplo."
        ),
    )

    def __init__(self, *args, **kwargs):
        from django.contrib.auth.password_validation import password_validators_help_text_html

        super().__init__(*args, **kwargs)
        # The real rules of the configured validators (length, common passwords...), as escaped HTML.
        self.fields["password1"].help_text = password_validators_help_text_html()

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
