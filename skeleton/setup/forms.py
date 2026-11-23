"""Setup forms; credentials use password inputs that never render their value back."""

import re

from django import forms
from django.contrib.auth.password_validation import validate_password
from django.utils.translation import gettext, gettext_lazy


class TokenForm(forms.Form):
    token = forms.CharField(label=gettext_lazy("Token de primeiro acesso"), max_length=200, strip=True)


DEFAULT_TIMEZONE = "America/Sao_Paulo"
# A name as ALLOWED_HOSTS takes it (``.dominio`` covers the subdomains) or a bracketed IPv6; no port.
HOST = re.compile(r"\.?[a-z0-9-]+(?:\.[a-z0-9-]+)*\.?|\[[0-9a-f:.]+\]")


def is_origin(value: str) -> bool:
    """``http(s)://host[:port]`` without path, query or fragment, as CSRF_TRUSTED_ORIGINS and links need."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(value)
        parts.port  # noqa: B018 - raises on a port that is not a number
    except ValueError:
        return False
    return (
        parts.scheme in {"http", "https"}
        and bool(parts.netloc)
        and "@" not in parts.netloc
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment
    )


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

    default_timezone = forms.CharField(
        label=gettext_lazy("Fuso horário padrão"),
        max_length=64,
        initial=DEFAULT_TIMEZONE,
        help_text=gettext_lazy(
            "Usado para mostrar datas e horas a quem não escolheu um fuso próprio. America/Sao_Paulo é o horário"
            " de Brasília. Outros: America/Manaus, America/Cuiaba, America/Belem, America/Fortaleza,"
            " America/Rio_Branco."
        ),
    )

    def __init__(self, *args, strict: bool | None = None, **kwargs):
        import zoneinfo

        from skeleton.instance.schema import CATALOG

        super().__init__(*args, **kwargs)
        self.strict = production_target() if strict is None else strict
        if "public_url" not in CATALOG:  # The key arrives with the notifications epic (#54).
            del self.fields["public_url"]
        zones = sorted(zoneinfo.available_timezones())
        if zones:  # Without the tz database the catalog validation still refuses an unknown zone.
            field = self.fields["default_timezone"]
            self.fields["default_timezone"] = forms.ChoiceField(
                label=field.label,
                initial=field.initial,
                help_text=field.help_text,
                choices=[(zone, zone) for zone in zones],
                required=False,
            )
        # Always submitted by the page (a value is selected); a scripted POST without it gets the default.
        self.fields["default_timezone"].required = False
        self.fields["default_timezone"].marked_required = True

    def clean_default_timezone(self):
        return self.cleaned_data["default_timezone"] or DEFAULT_TIMEZONE

    def clean_allowed_hosts(self):
        """Host names only (#732): a scheme, port or path here leaves every request refused after the setup."""
        items = [item.strip().lower() for item in self.cleaned_data["allowed_hosts"].split(",")]
        if not all(items):
            raise forms.ValidationError(gettext("Separe os endereços com vírgula, sem itens vazios."))
        for item in items:
            if item == "*":
                if self.strict:
                    raise forms.ValidationError(
                        gettext("Em produção, liste os endereços aceitos: * aceitaria qualquer um.")
                    )
                continue
            if "://" in item or "/" in item:
                raise forms.ValidationError(gettext("Informe só o nome, sem http:// nem caminho: app.exemplo.com.br."))
            if not HOST.fullmatch(item):
                raise forms.ValidationError(
                    gettext("Informe só o nome, sem porta: app.exemplo.com.br, localhost ou 127.0.0.1.")
                )
        return ",".join(items)

    def clean_public_url(self):
        value = self.cleaned_data["public_url"].strip()
        if value and not is_origin(value):
            raise forms.ValidationError(
                gettext("Use o endereço completo, com https:// e sem caminho: https://app.exemplo.com.br.")
            )
        return value.rstrip("/")

    def clean_csrf_trusted_origins(self):
        """Full origins (#732): the Django check refuses an origin without scheme when the instance starts."""
        value = self.cleaned_data["csrf_trusted_origins"].strip()
        if not value:
            return value
        origins = [item.strip() for item in value.split(",")]
        if not all(is_origin(origin) for origin in origins):
            raise forms.ValidationError(
                gettext(
                    "Cada origem precisa de https:// e não leva caminho: https://app.exemplo.com.br. Separe várias"
                    " com vírgula."
                )
            )
        return ",".join(origin.rstrip("/") for origin in origins)

    def clean(self):
        from urllib.parse import urlsplit

        from django.http.request import validate_host

        data = super().clean()
        public_url, hosts = data.get("public_url"), data.get("allowed_hosts")
        if public_url and hosts and "public_url" not in self.errors:
            host = (urlsplit(public_url).hostname or "").lower()
            if host and not validate_host(host, hosts.split(",")):
                self.add_error(
                    "public_url",
                    gettext(
                        "O endereço público precisa estar entre os endereços aceitos acima; senão os links enviados"
                        " por e-mail levam a uma página recusada."
                    ),
                )
        return data


FILL_IN = gettext_lazy("Informe este campo.")
KEEP_SECRET = gettext_lazy("Já definida. Deixe em branco para manter a atual.")


def keep_secret(form, name: str, kept: str | None) -> None:
    """A stored secret makes its field optional; the value itself never reaches the page (#466)."""
    form.kept_secret = kept
    if kept is not None:
        field = form.fields[name]
        field.required = False
        field.marked_required = False
        field.help_text = KEEP_SECRET


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

    def __init__(self, *args, kept_password: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        for name in self.separate_required:
            self.fields[name].marked_required = True
        keep_secret(self, "db_password", kept_password)

    def clean(self):
        from .database import DEFAULT_PORT, build_dsn

        data = super().clean()
        if data.get("database_url"):
            data["dsn"] = data["database_url"]
            return data
        if not data.get("db_password") and self.kept_secret is not None:
            data["db_password"] = self.kept_secret
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


class AgentSchemaForm(forms.Form):
    """The schema step when the deployment agent migrated already (#652): nothing to type, only confirm."""


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


SMTP_SCHEMES = {"smtp", "smtps", "smtp+tls", "smtp+ssl"}
SECURITY_SCHEMES = {"starttls": "smtp+tls", "ssl": "smtp+ssl", "none": "smtp"}


def smtp_url(*, host: str, port: int | str, security: str, user: str = "", password: str = "") -> str:
    """The email URL from separate fields; user and password are percent-encoded (#465)."""
    from urllib.parse import quote

    credentials = ""
    if user or password:
        credentials = quote(user, safe="") + (":" + quote(password, safe="") if password else "") + "@"
    return f"{SECURITY_SCHEMES[security]}://{credentials}{host.strip()}:{int(port)}"


class EmailForm(forms.Form):
    """SMTP by separate fields; "Configurar depois" only when the target is not production (#465)."""

    advanced_fields = ("email_url",)

    email_mode = forms.ChoiceField(
        label=gettext_lazy("Como enviar e-mails"),
        choices=(("smtp", gettext_lazy("Usar um servidor SMTP")), ("later", gettext_lazy("Configurar depois"))),
        initial="smtp",
        required=False,
        widget=forms.RadioSelect,
    )
    smtp_host = forms.CharField(
        label=gettext_lazy("Servidor SMTP"),
        max_length=255,
        required=False,
        help_text=gettext_lazy(
            "Endereço do servidor de envio. Exemplos: smtp.gmail.com, smtp.office365.com,"
            " email-smtp.sa-east-1.amazonaws.com."
        ),
    )
    smtp_port = forms.IntegerField(
        label=gettext_lazy("Porta"),
        min_value=1,
        max_value=65535,
        initial=587,
        required=False,
        help_text=gettext_lazy("587 para STARTTLS (o mais comum) ou 465 para SSL/TLS."),
    )
    smtp_security = forms.ChoiceField(
        label=gettext_lazy("Segurança da conexão"),
        choices=(
            ("starttls", gettext_lazy("STARTTLS, normalmente na porta 587")),
            ("ssl", gettext_lazy("SSL/TLS direto, normalmente na porta 465")),
            ("none", gettext_lazy("Sem criptografia, só em rede interna ou teste")),
        ),
        initial="starttls",
        required=False,
        widget=forms.RadioSelect,
    )
    smtp_user = forms.CharField(
        label=gettext_lazy("Usuário"),
        max_length=255,
        required=False,
        help_text=gettext_lazy(
            "Em geral o endereço de e-mail completo ou a chave de acesso do provedor. Em branco se o servidor não"
            " pede autenticação."
        ),
    )
    smtp_password = forms.CharField(
        label=gettext_lazy("Senha"),
        max_length=500,
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text=gettext_lazy(
            "Em provedores com verificação em duas etapas (Gmail, Microsoft 365), use uma senha de aplicativo."
        ),
    )
    default_from_email = forms.EmailField(
        label=gettext_lazy("Remetente padrão"),
        required=False,
        help_text=gettext_lazy(
            "Endereço que aparece como remetente das mensagens do sistema. Precisa ser um endereço que o servidor"
            " SMTP aceita enviar. Exemplo: nao-responda@exemplo.com.br."
        ),
    )
    test_recipient = forms.EmailField(
        label=gettext_lazy("Enviar o teste para"),
        required=False,
        help_text=gettext_lazy(
            "Uma mensagem de teste é enviada agora para este endereço. Use um e-mail que você possa conferir."
        ),
    )
    email_url = forms.CharField(
        label=gettext_lazy("URL completa (avançado)"),
        max_length=500,
        required=False,
        widget=forms.PasswordInput(render_value=False),
        help_text=gettext_lazy(
            "Quando preenchida, os campos de servidor acima são ignorados. Formato:"
            " smtp+tls://usuário:senha@servidor:587."
        ),
    )
    # Marked as required on the page when SMTP is used; the HTML attribute stays off ("later").
    smtp_required = ("smtp_host", "default_from_email", "test_recipient")

    def __init__(self, *args, strict: bool | None = None, kept_password: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.strict = production_target() if strict is None else strict
        keep_secret(self, "smtp_password", kept_password)
        if self.strict:
            mode = self.fields["email_mode"]
            mode.choices = [("smtp", gettext_lazy("Usar um servidor SMTP"))]
            mode.widget = forms.HiddenInput()
        for name in (*self.smtp_required, "smtp_port", "smtp_security"):
            self.fields[name].marked_required = True

    def clean_email_url(self):
        # Messages never echo the value: the URL carries the SMTP password. Other parser refusals
        # (unknown scheme or parameter) stay with the view, which reports names only.
        from urllib.parse import urlsplit

        value = self.cleaned_data["email_url"]
        if value and self.strict and urlsplit(value).scheme.lower() not in SMTP_SCHEMES:
            raise forms.ValidationError(gettext("Em produção só um servidor SMTP é aceito."))
        return value

    def clean(self):
        data = super().clean()
        data["email_mode"] = "smtp" if self.strict else (data.get("email_mode") or "smtp")
        data["url"] = ""
        if data["email_mode"] == "later":
            return data
        required = self.smtp_required if not data.get("email_url") else ("default_from_email", "test_recipient")
        for name in required:
            if not data.get(name) and name not in self.errors:
                self.add_error(name, FILL_IN)
        if self.errors:
            return data
        if data.get("email_url"):
            data["url"] = data["email_url"]
        else:
            data["url"] = smtp_url(
                host=data["smtp_host"],
                port=data.get("smtp_port") or 587,
                security=data.get("smtp_security") or "starttls",
                user=data.get("smtp_user") or "",
                password=data.get("smtp_password") or self.kept_secret or "",
            )
        return data


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
    cache_key_prefix = forms.CharField(
        label=gettext_lazy("Prefixo das chaves no cache"),
        max_length=50,
        required=False,
        help_text=gettext_lazy(
            "Opcional. Um nome desta instância (a-z, 0-9, _ e -), para várias instâncias poderem usar o mesmo"
            " Redis sem misturar chaves."
        ),
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

    def __init__(self, *args, strict: bool | None = None, kept_password: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self.strict = production_target() if strict is None else strict
        keep_secret(self, "cache_password", kept_password)
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

    def clean_cache_key_prefix(self):
        from skeleton.instance.schema import CATALOG

        try:
            return CATALOG["cache_key_prefix"].clean(self.cleaned_data["cache_key_prefix"])
        except ValueError as error:
            raise forms.ValidationError(str(error)) from None

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
            url = redis_url(location, data.get("cache_password") or self.kept_secret or "")
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
        max_length=63,
        help_text=gettext_lazy(
            "Apelido técnico, usado no endereço da organização e em integrações: de 1 a 63 caracteres, só letras"
            " minúsculas, números e hífen, sem espaços nem acentos. Exemplo: prefeitura-exemplo."
        ),
    )

    def __init__(self, *args, **kwargs):
        from django.contrib.auth.password_validation import password_validators_help_text_html

        super().__init__(*args, **kwargs)
        # The real rules of the configured validators (length, common passwords...), as escaped HTML.
        self.fields["password1"].help_text = password_validators_help_text_html()

    def clean_organization_slug(self):
        from skeleton.common.hostnames import slug_problem

        value = self.cleaned_data["organization_slug"]
        problem = slug_problem(value)
        if problem == "format":
            raise forms.ValidationError(
                gettext(
                    "Use de 1 a 63 caracteres: letras minúsculas, números e hífen, sem começar nem terminar com hífen."
                )
            )
        if problem == "reserved":
            raise forms.ValidationError(gettext("Este identificador é reservado. Escolha outro."))
        return value

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
