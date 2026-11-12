"""The closed catalog of instance settings: name, type, validation, secrecy and env variable.

A key outside this catalog is refused. Bootstrap variables that locate the store or the
deployment (``DJANGO_SETTINGS_MODULE``, ``SKELETON_INSTANCE_DB``, ``SKELETON_INSTANCE_KEY_FILE``,
agent paths, maintenance marker) and the transient migration owner credential stay in the
environment only and are deliberately absent here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

BOOL_TRUE = {"true", "on", "ok", "y", "yes", "1"}
BOOL_FALSE = {"false", "off", "n", "no", "0", ""}


def _text(value: str) -> str:
    if "\n" in value or "\r" in value or "\x00" in value:
        raise ValueError("O valor não pode ter quebra de linha.")
    return value


def _required_text(value: str) -> str:
    if not _text(value).strip():
        raise ValueError("O valor não pode ser vazio.")
    return value


def _bool(value: str) -> str:
    if value.strip().lower() not in BOOL_TRUE | BOOL_FALSE:
        raise ValueError("Use true ou false.")
    return value.strip().lower()


def _int(minimum: int) -> Callable[[str], str]:
    def validate(value: str) -> str:
        try:
            number = int(value.strip())
        except ValueError:
            raise ValueError("Use um número inteiro.") from None
        if number < minimum:
            raise ValueError(f"Use um número inteiro maior ou igual a {minimum}.")
        return str(number)

    return validate


def _list(value: str) -> str:
    items = [item.strip() for item in _text(value).split(",")]
    if not all(items):
        raise ValueError("Use itens separados por vírgula, sem itens vazios.")
    return ",".join(items)


def _url(parser: str) -> Callable[[str], str]:
    def validate(value: str) -> str:
        from urllib.parse import urlsplit

        import environ  # type: ignore[import-untyped]

        try:
            if not urlsplit(_required_text(value)).scheme or "://" not in value:
                raise ValueError
            getattr(environ.Env, parser)(value)
        except Exception:
            raise ValueError("URL inválida para este campo.") from None
        return value

    return validate


def _origin(value: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(_required_text(value).strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("Use uma origem http(s), como https://app.example.com.")
    if parts.path not in {"", "/"}:
        raise ValueError("Use só a origem, sem caminho.")
    return value.strip().rstrip("/")


def _subject(value: str) -> str:
    value = _required_text(value).strip()
    if not (value.startswith("mailto:") or value.startswith("https://")):
        raise ValueError("Use mailto: ou uma URL https://.")
    return value


def _token(minimum: int) -> Callable[[str], str]:
    def validate(value: str) -> str:
        if len(value) < minimum or any(character.isspace() for character in value):
            raise ValueError(f"Use ao menos {minimum} caracteres, sem espaços.")
        return value

    return validate


def _sentry_dsn(value: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(_required_text(value).strip())
    if parts.scheme not in {"http", "https"} or not parts.username or not parts.hostname or parts.path in {"", "/"}:
        raise ValueError("Use o DSN do projeto, como https://<chave>@o0.ingest.sentry.io/<projeto>.")
    return value.strip()


def _choice(*choices: str) -> Callable[[str], str]:
    def validate(value: str) -> str:
        if value not in choices:
            raise ValueError(f"Use um de: {', '.join(choices)}.")
        return value

    return validate


@dataclass(frozen=True)
class SettingSpec:
    key: str
    env: str
    kind: str
    description: str
    validate: Callable[[str], str]
    secret: bool = False
    required_for_setup: bool = False

    def clean(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("O valor precisa ser texto.")
        return self.validate(value)


_SPECS = (
    SettingSpec("secret_key", "DJANGO_SECRET_KEY", "secret", "Chave secreta do Django", _required_text, True, True),
    SettingSpec(
        "allowed_hosts", "DJANGO_ALLOWED_HOSTS", "list", "Hosts aceitos, separados por vírgula", _list, False, True
    ),
    SettingSpec("health_host", "DJANGO_HEALTH_HOST", "str", "Host usado pelas sondas de saúde", _required_text),
    SettingSpec("csrf_trusted_origins", "DJANGO_CSRF_TRUSTED_ORIGINS", "list", "Origens confiáveis para CSRF", _list),
    SettingSpec("trust_proxy", "DJANGO_TRUST_PROXY", "bool", "Confiar no proxy reverso", _bool),
    SettingSpec("trusted_proxy_count", "DJANGO_TRUSTED_PROXY_COUNT", "int", "Proxies confiáveis à frente", _int(0)),
    SettingSpec(
        "trusted_client_ip_header", "DJANGO_TRUSTED_CLIENT_IP_HEADER", "str", "Cabeçalho do IP do cliente", _text
    ),
    SettingSpec(
        "database_url",
        "DATABASE_URL",
        "url",
        "Banco de dados da aplicação (role runtime)",
        _url("db_url_config"),
        True,
        True,
    ),
    SettingSpec("cache_url", "CACHE_URL", "url", "Cache compartilhado", _url("cache_url_config"), True),
    SettingSpec("email_url", "EMAIL_URL", "url", "Servidor de email", _url("email_url_config"), True),
    SettingSpec("default_from_email", "DEFAULT_FROM_EMAIL", "str", "Remetente padrão dos emails", _required_text),
    SettingSpec("public_url", "SKELETON_PUBLIC_URL", "url", "Origem pública dos links de notificação", _origin),
    SettingSpec(
        "webpush_vapid_private_key_file",
        "WEBPUSH_VAPID_PRIVATE_KEY_FILE",
        "path",
        "Arquivo da chave privada VAPID (web push)",
        _required_text,
    ),
    SettingSpec("webpush_vapid_subject", "WEBPUSH_VAPID_SUBJECT", "str", "Contato VAPID (mailto: ou https:)", _subject),
    SettingSpec("webpush_allowed_hosts", "WEBPUSH_ALLOWED_HOSTS", "list", "Serviços de push permitidos", _list),
    SettingSpec("mfa_encryption_keys", "MFA_ENCRYPTION_KEYS", "list", "Chaves de cifra do MFA", _list, True),
    SettingSpec(
        "identity_provider_encryption_keys",
        "IDENTITY_PROVIDER_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos provedores de identidade",
        _list,
        True,
    ),
    SettingSpec(
        "integration_encryption_keys",
        "INTEGRATION_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos segredos de integração (webhooks)",
        _list,
        True,
    ),
    SettingSpec("sso_provider_config_path", "SSO_PROVIDER_CONFIG_PATH", "path", "Arquivo de provedores SSO", _text),
    SettingSpec("public_signup", "SKELETON_PUBLIC_SIGNUP", "bool", "Cadastro público aberto", _bool),
    SettingSpec("require_admin_mfa", "SKELETON_REQUIRE_ADMIN_MFA", "bool", "MFA obrigatório para administração", _bool),
    SettingSpec("json_max_bytes", "SKELETON_JSON_MAX_BYTES", "int", "Tamanho máximo de JSON", _int(1)),
    SettingSpec("package_max_bytes", "SKELETON_PACKAGE_MAX_BYTES", "int", "Tamanho máximo de pacote", _int(1)),
    SettingSpec("upload_max_bytes", "SKELETON_UPLOAD_MAX_BYTES", "int", "Tamanho máximo de anexo", _int(1)),
    SettingSpec(
        "files_org_quota_bytes", "SKELETON_FILES_ORG_QUOTA_BYTES", "int", "Cota de anexos por organização", _int(0)
    ),
    SettingSpec("event_retention_days", "SKELETON_EVENT_RETENTION_DAYS", "int", "Retenção de eventos", _int(1)),
    SettingSpec("export_max_rows", "SKELETON_EXPORT_MAX_ROWS", "int", "Linhas máximas de uma exportação CSV", _int(1)),
    SettingSpec("import_max_rows", "SKELETON_IMPORT_MAX_ROWS", "int", "Linhas máximas de uma importação CSV", _int(1)),
    SettingSpec(
        "transfer_retention_days", "SKELETON_TRANSFER_RETENTION_DAYS", "int", "Retenção dos arquivos CSV", _int(1)
    ),
    SettingSpec(
        "bundled_apps", "SKELETON_BUNDLED_APPS", "list", "Apps first-party do núcleo instaladas (nomes)", _list
    ),
    SettingSpec(
        "bundled_retained",
        "SKELETON_BUNDLED_RETAINED",
        "list",
        "Apps first-party retiradas com dados preservados (nomes)",
        _list,
    ),
    SettingSpec(
        "api_token_max_days", "SKELETON_API_TOKEN_MAX_DAYS", "int", "Validade máxima dos tokens de API (dias)", _int(1)
    ),
    SettingSpec(
        "api_token_rate_per_minute",
        "SKELETON_API_TOKEN_RATE_PER_MINUTE",
        "int",
        "Requisições por minuto de cada token de API",
        _int(1),
    ),
    SettingSpec(
        "api_token_failures_per_minute",
        "SKELETON_API_TOKEN_FAILURES_PER_MINUTE",
        "int",
        "Falhas de autenticação por token por minuto e por IP",
        _int(1),
    ),
    SettingSpec(
        "job_succeeded_retention_days",
        "SKELETON_JOB_SUCCEEDED_RETENTION_DAYS",
        "int",
        "Retenção dos jobs concluídos ou cancelados",
        _int(1),
    ),
    SettingSpec(
        "job_dead_retention_days", "SKELETON_JOB_DEAD_RETENTION_DAYS", "int", "Retenção dos jobs esgotados", _int(1)
    ),
    SettingSpec(
        "webhook_retention_days", "SKELETON_WEBHOOK_RETENTION_DAYS", "int", "Retenção das entregas de webhook", _int(1)
    ),
    SettingSpec("files_scanner", "SKELETON_FILES_SCANNER", "str", "Antivírus dos anexos", _choice("none", "clamav")),
    SettingSpec(
        "files_clamav_address",
        "SKELETON_FILES_CLAMAV_ADDRESS",
        "str",
        "Endereço do clamd (tcp://host:3310 ou unix:///caminho)",
        _required_text,
    ),
    SettingSpec(
        "notification_read_retention_days",
        "SKELETON_NOTIFICATION_READ_RETENTION_DAYS",
        "int",
        "Retenção das notificações lidas ou arquivadas",
        _int(1),
    ),
    SettingSpec(
        "notification_unread_retention_days",
        "SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS",
        "int",
        "Retenção das notificações não lidas",
        _int(1),
    ),
    SettingSpec(
        "notification_delivery_retention_days",
        "SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS",
        "int",
        "Retenção das entregas de notificação",
        _int(1),
    ),
    SettingSpec(
        "metrics_token",
        "SKELETON_METRICS_TOKEN",
        "secret",
        "Token Bearer do endpoint /metrics (extra [metrics])",
        _token(32),
        True,
    ),
    SettingSpec(
        "sentry_dsn",
        "SKELETON_SENTRY_DSN",
        "url",
        "DSN do projeto Sentry (extra [sentry])",
        _sentry_dsn,
        True,
    ),
    SettingSpec("files_backend", "SKELETON_FILES_BACKEND", "str", "Armazenamento de anexos", _choice("local", "s3")),
    SettingSpec("files_root", "SKELETON_FILES_ROOT", "path", "Pasta local dos anexos", _required_text),
    SettingSpec("files_s3_bucket", "SKELETON_FILES_S3_BUCKET", "str", "Bucket S3 dos anexos", _required_text),
    SettingSpec("files_s3_region", "SKELETON_FILES_S3_REGION", "str", "Região S3", _required_text),
    SettingSpec("files_s3_endpoint", "SKELETON_FILES_S3_ENDPOINT", "str", "Endpoint S3", _required_text),
)

CATALOG: dict[str, SettingSpec] = {spec.key: spec for spec in _SPECS}
BY_ENV: dict[str, SettingSpec] = {spec.env: spec for spec in _SPECS}

# Never stored in the instance SQLite: they locate it, or must not persist (owner credential).
BOOTSTRAP_ENV = (
    "DJANGO_SETTINGS_MODULE",
    "SKELETON_INSTANCE_DB",
    "SKELETON_INSTANCE_KEY_FILE",
    "SKELETON_PROJECT_ROOT",
    "SKELETON_DEPLOYMENT_INVENTORY",
    "SKELETON_MAINTENANCE_MARKER",
    "SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE",
    "SKELETON_DEPLOYMENT_SMOKE_PATHS",
    "SKELETON_AGENT_SOCKET",
    "SKELETON_AGENT_TOKEN_FILE",
    "MIGRATION_DATABASE_URL",
)


def spec_for(name: str) -> SettingSpec:
    """The spec of a catalog key or of its environment variable; anything else is refused."""
    spec = CATALOG.get(name) or BY_ENV.get(name)
    if spec is None:
        raise KeyError(f"Chave desconhecida: {name}.")
    return spec
