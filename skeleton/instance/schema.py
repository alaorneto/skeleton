"""The closed catalog of instance settings: name, type, validation, secrecy and env variable.

A key outside this catalog is refused. Bootstrap variables that locate the store or the
deployment (``DJANGO_SETTINGS_MODULE``, ``SKELETON_INSTANCE_DB``, ``SKELETON_INSTANCE_KEY_FILE``,
agent paths, maintenance marker) and the transient migration owner credential stay in the
environment only and are deliberately absent here.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace

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


def _fernet_keys(value: str) -> str:
    """An ordered keyring: every item must be a Fernet key, or the first use after restart fails (#323)."""
    from cryptography.fernet import Fernet

    cleaned = _list(value)
    for item in cleaned.split(","):
        try:
            Fernet(item.encode())
        except (ValueError, TypeError):
            raise ValueError(
                "Cada item precisa ser uma chave Fernet (32 bytes em base64 url-safe); gere com: "
                'python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"'
            ) from None
    return cleaned


def _secret_key(value: str) -> str:
    """The criterion of Django's ``security.W009``: long and varied (#323)."""
    if len(_required_text(value)) < 50 or len(set(value)) < 5:
        raise ValueError("Use ao menos 50 caracteres, com 5 ou mais caracteres diferentes.")
    return value


def _url(parser: str) -> Callable[[str], str]:
    def validate(value: str) -> str:
        from urllib.parse import urlsplit

        import environ  # type: ignore[import-untyped]

        try:
            if not urlsplit(_required_text(value)).scheme or "://" not in value:
                raise ValueError
            getattr(environ.Env, parser)(value)
        except Exception:  # noqa: BLE001 - any parser failure is the same invalid-URL refusal
            raise ValueError("URL inválida para este campo.") from None
        return value

    return validate


def _cache_prefix(value: str) -> str:
    """The key prefix of the shared cache (#675): one per instance on the same Redis."""
    import re

    cleaned = _text(value).strip()
    if cleaned and not re.fullmatch(r"[a-z0-9_-]{1,50}", cleaned):
        raise ValueError("Use de 1 a 50 caracteres entre a-z, 0-9, _ e -.")
    return cleaned


def _origin(value: str) -> str:
    from urllib.parse import urlsplit

    parts = urlsplit(_required_text(value).strip())
    if parts.scheme not in {"http", "https"} or not parts.netloc or parts.query or parts.fragment:
        raise ValueError("Use uma origem http(s), como https://app.example.com.")
    if parts.path not in {"", "/"}:
        raise ValueError("Use só a origem, sem caminho.")
    return value.strip().rstrip("/")


_DNS_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"  # The rule of skeleton.common.hostnames.LABEL (#478).


def _base_domain(value: str) -> str:
    """The base domain of the organization addresses (``skeleton.com``); empty turns them off (#478)."""
    import re

    cleaned = _text(value).strip().rstrip(".").lower()
    if not cleaned:
        return ""
    message = "Informe só o domínio, como skeleton.com."
    if any(mark in cleaned for mark in ("://", "/", ":", "*", "@", " ")):
        raise ValueError(message)
    if len(cleaned) > 253 or not all(re.fullmatch(_DNS_LABEL, label) for label in cleaned.split(".")):
        raise ValueError(message)
    return cleaned


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


def _github_repository(value: str) -> str:
    """Empty (off) or ``owner/name``; the same pattern as GITHUB_REPOSITORY of skeleton.system.releases."""
    import re

    value = _text(value).strip()
    if value and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?/[A-Za-z0-9._-]{1,100}", value):
        raise ValueError("Use dono/nome, sem https://github.com/.")
    return value


def _choice(*choices: str) -> Callable[[str], str]:
    def validate(value: str) -> str:
        if value not in choices:
            raise ValueError(f"Use um de: {', '.join(choices)}.")
        return value

    return validate


def _language(value: str) -> str:
    import re

    if not re.fullmatch(r"[a-z]{2,3}(-[a-z0-9]{2,8})?", value):
        raise ValueError("Use um código de idioma como pt-br.")
    return value


def _timezone(value: str) -> str:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError):
        raise ValueError("Use um fuso IANA, como America/Sao_Paulo.") from None
    return value


def _currency(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Z]{3}", value):
        raise ValueError("Use um código de moeda ISO 4217, como BRL.")
    return value


def _day_list(value: str) -> str:
    items = [item.strip() for item in _text(value).split(",") if item.strip()]
    if not items or not all(item.isdigit() and 0 < int(item) <= 365 for item in items):
        raise ValueError("Use dias de 1 a 365 separados por vírgula, como 1,3,5.")
    return ",".join(str(int(item)) for item in items)


def _invoice_prefix(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9-]{0,16}", value):
        raise ValueError("Use até 16 letras, dígitos ou hífen.")
    return value


@dataclass(frozen=True)
class SettingSpec:
    key: str
    env: str
    kind: str
    description: str
    validate: Callable[[str], str]
    secret: bool = False
    required_for_setup: bool = False
    # Section of /platform/instance/ (#779): presentation only, one of SETTING_GROUPS.
    group: str = ""

    def clean(self, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("O valor precisa ser texto.")
        return self.validate(value)


_SPECS = (
    SettingSpec("secret_key", "DJANGO_SECRET_KEY", "secret", "Chave secreta do Django", _secret_key, True, True),
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
    SettingSpec(
        "database_transaction_pooler",
        "DATABASE_TRANSACTION_POOLER",
        "bool",
        "Banco atrás de um pooler em modo transação (PgBouncer)",
        _bool,
    ),
    SettingSpec(
        "database_conn_max_age", "DATABASE_CONN_MAX_AGE", "int", "Segundos de reuso de uma conexão ao banco", _int(0)
    ),
    SettingSpec("cache_url", "CACHE_URL", "url", "Cache compartilhado", _url("cache_url_config"), True),
    SettingSpec(
        "cache_key_prefix",
        "SKELETON_CACHE_KEY_PREFIX",
        "str",
        "Prefixo das chaves no cache compartilhado",
        _cache_prefix,
    ),
    SettingSpec("email_url", "EMAIL_URL", "url", "Servidor de email", _url("email_url_config"), True),
    SettingSpec("default_from_email", "DEFAULT_FROM_EMAIL", "str", "Remetente padrão dos emails", _required_text),
    SettingSpec("public_url", "SKELETON_PUBLIC_URL", "url", "Origem pública dos links de notificação", _origin),
    SettingSpec(
        "organization_base_domain",
        "SKELETON_ORGANIZATION_BASE_DOMAIN",
        "str",
        "Domínio base dos endereços das organizações",
        _base_domain,
    ),
    SettingSpec(
        "organization_custom_domains",
        "SKELETON_ORGANIZATION_CUSTOM_DOMAINS",
        "bool",
        "Domínios próprios de organizações",
        _bool,
    ),
    SettingSpec(
        "webpush_vapid_private_key_file",
        "WEBPUSH_VAPID_PRIVATE_KEY_FILE",
        "path",
        "Arquivo da chave privada VAPID (web push)",
        _required_text,
    ),
    SettingSpec("webpush_vapid_subject", "WEBPUSH_VAPID_SUBJECT", "str", "Contato VAPID (mailto: ou https:)", _subject),
    SettingSpec("webpush_allowed_hosts", "WEBPUSH_ALLOWED_HOSTS", "list", "Serviços de push permitidos", _list),
    SettingSpec("mfa_encryption_keys", "MFA_ENCRYPTION_KEYS", "list", "Chaves de cifra do MFA", _fernet_keys, True),
    SettingSpec(
        "identity_provider_encryption_keys",
        "IDENTITY_PROVIDER_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos provedores de identidade",
        _fernet_keys,
        True,
    ),
    SettingSpec(
        "integration_encryption_keys",
        "INTEGRATION_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos segredos de integração (webhooks)",
        _fernet_keys,
        True,
    ),
    SettingSpec(
        "billing_encryption_keys",
        "BILLING_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos segredos dos provedores de pagamento",
        _fernet_keys,
        True,
    ),
    SettingSpec(
        "remote_instance_encryption_keys",
        "REMOTE_INSTANCE_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra dos tokens das conexões com instâncias remotas",
        _fernet_keys,
        True,
    ),
    SettingSpec(
        "files_storage_encryption_keys",
        "FILES_STORAGE_ENCRYPTION_KEYS",
        "list",
        "Chaves de cifra das credenciais dos locais de armazenamento",
        _fernet_keys,
        True,
    ),
    SettingSpec(
        "insights_enabled",
        "SKELETON_INSIGHTS_ENABLED",
        "bool",
        "Números de gestão da instância (resumo por organização e métricas diárias)",
        _bool,
    ),
    SettingSpec("sso_provider_config_path", "SSO_PROVIDER_CONFIG_PATH", "path", "Arquivo de provedores SSO", _text),
    SettingSpec("public_signup", "SKELETON_PUBLIC_SIGNUP", "bool", "Cadastro público aberto", _bool),
    SettingSpec(
        "organization_signup_enabled",
        "SKELETON_ORGANIZATION_SIGNUP_ENABLED",
        "bool",
        "Cadastro de organização pelo cliente",
        _bool,
    ),
    SettingSpec(
        "organization_signup_max_per_person",
        "SKELETON_ORGANIZATION_SIGNUP_MAX_PER_PERSON",
        "int",
        "Organizações que uma pessoa cria pelo cadastro",
        _int(1),
    ),
    SettingSpec("require_admin_mfa", "SKELETON_REQUIRE_ADMIN_MFA", "bool", "MFA obrigatório para administração", _bool),
    SettingSpec("require_mfa", "SKELETON_REQUIRE_MFA", "bool", "MFA obrigatório para todas as pessoas", _bool),
    SettingSpec("json_max_bytes", "SKELETON_JSON_MAX_BYTES", "int", "Tamanho máximo de JSON", _int(1)),
    SettingSpec("package_max_bytes", "SKELETON_PACKAGE_MAX_BYTES", "int", "Tamanho máximo de pacote", _int(1)),
    SettingSpec(
        "update_repository",
        "SKELETON_UPDATE_REPOSITORY",
        "str",
        "Repositório do GitHub consultado para avisar de versão nova (dono/nome)",
        _github_repository,
    ),
    SettingSpec("upload_max_bytes", "SKELETON_UPLOAD_MAX_BYTES", "int", "Tamanho máximo de anexo", _int(1)),
    SettingSpec(
        "files_org_quota_bytes", "SKELETON_FILES_ORG_QUOTA_BYTES", "int", "Cota de anexos por organização", _int(0)
    ),
    SettingSpec(
        "organization_member_limit",
        "SKELETON_ORGANIZATION_MEMBER_LIMIT",
        "int",
        "Limite padrão de membros por organização",
        _int(0),
    ),
    SettingSpec(
        "files_deleted_retention_days",
        "SKELETON_FILES_DELETED_RETENTION_DAYS",
        "int",
        "Dias até expurgar o conteúdo de anexos excluídos",
        _int(1),
    ),
    SettingSpec(
        "flows_form_retention_days",
        "SKELETON_FLOWS_FORM_RETENTION_DAYS",
        "int",
        "Dias até esvaziar os formulários de fluxos encerrados (0 mantém)",
        _int(0),
    ),
    SettingSpec("billing_enabled", "SKELETON_BILLING_ENABLED", "bool", "Cobrança de assinaturas ligada", _bool),
    SettingSpec(
        "billing_invoice_prefix",
        "SKELETON_BILLING_INVOICE_PREFIX",
        "str",
        "Prefixo dos números de fatura",
        _invoice_prefix,
    ),
    SettingSpec(
        "billing_default_currency",
        "SKELETON_BILLING_DEFAULT_CURRENCY",
        "str",
        "Moeda das novas contas de cobrança",
        _currency,
    ),
    SettingSpec(
        "billing_payment_terms_days",
        "SKELETON_BILLING_PAYMENT_TERMS_DAYS",
        "int",
        "Dias para pagar uma fatura de renovação",
        _int(0),
    ),
    SettingSpec(
        "billing_usage_retention_days",
        "SKELETON_BILLING_USAGE_RETENTION_DAYS",
        "int",
        "Dias de histórico diário de armazenamento",
        _int(31),
    ),
    SettingSpec(
        "billing_grace_days", "SKELETON_BILLING_GRACE_DAYS", "int", "Dias de atraso até somente leitura", _int(0)
    ),
    SettingSpec(
        "billing_suspend_after_days",
        "SKELETON_BILLING_SUSPEND_AFTER_DAYS",
        "int",
        "Dias de atraso até a suspensão",
        _int(0),
    ),
    SettingSpec(
        "billing_cancel_after_days",
        "SKELETON_BILLING_CANCEL_AFTER_DAYS",
        "int",
        "Dias de atraso até o cancelamento",
        _int(1),
    ),
    SettingSpec(
        "billing_retry_schedule_days",
        "SKELETON_BILLING_RETRY_SCHEDULE_DAYS",
        "str",
        "Dias após o vencimento das novas tentativas de cobrança",
        _day_list,
    ),
    SettingSpec(
        "billing_pending_payment_hold_days",
        "SKELETON_BILLING_PENDING_PAYMENT_HOLD_DAYS",
        "int",
        "Dias em que um pagamento pendente segura o bloqueio",
        _int(0),
    ),
    SettingSpec(
        "billing_trial_requires_payment_method",
        "SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD",
        "bool",
        "Trial só com meio de pagamento cadastrado",
        _bool,
    ),
    SettingSpec("default_language", "SKELETON_DEFAULT_LANGUAGE", "str", "Idioma padrão da instância", _language),
    SettingSpec("default_timezone", "SKELETON_DEFAULT_TIMEZONE", "str", "Fuso horário padrão da instância", _timezone),
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
        "api_token_max_per_owner",
        "SKELETON_API_TOKEN_MAX_PER_OWNER",
        "int",
        "Máximo de tokens de API vivos por pessoa ou conta de serviço",
        _int(1),
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
    SettingSpec(
        "files_shared", "SKELETON_FILES_SHARED", "bool", "Pasta dos anexos compartilhada por todos os nós", _bool
    ),
    SettingSpec("files_s3_bucket", "SKELETON_FILES_S3_BUCKET", "str", "Bucket S3 dos anexos", _required_text),
    SettingSpec("files_s3_region", "SKELETON_FILES_S3_REGION", "str", "Região S3", _required_text),
    SettingSpec("files_s3_endpoint", "SKELETON_FILES_S3_ENDPOINT", "str", "Endpoint S3", _required_text),
)

# Sections of the instance configuration page, in order (#779). Every key is in exactly one.
SETTING_GROUPS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "security",
        "Segurança e acesso",
        (
            "secret_key",
            "allowed_hosts",
            "csrf_trusted_origins",
            "health_host",
            "trust_proxy",
            "trusted_proxy_count",
            "trusted_client_ip_header",
            "require_admin_mfa",
            "require_mfa",
            "public_signup",
            "organization_signup_enabled",
            "organization_signup_max_per_person",
            "sso_provider_config_path",
        ),
    ),
    (
        "encryption",
        "Chaves de cifra",
        (
            "mfa_encryption_keys",
            "identity_provider_encryption_keys",
            "integration_encryption_keys",
            "billing_encryption_keys",
            "remote_instance_encryption_keys",
            "files_storage_encryption_keys",
        ),
    ),
    (
        "database",
        "Banco e cache",
        ("database_url", "database_transaction_pooler", "database_conn_max_age", "cache_url", "cache_key_prefix"),
    ),
    (
        "addresses",
        "Endereços, idioma e fuso",
        (
            "public_url",
            "organization_base_domain",
            "organization_custom_domains",
            "default_language",
            "default_timezone",
        ),
    ),
    (
        "messaging",
        "Email e push",
        (
            "email_url",
            "default_from_email",
            "webpush_vapid_private_key_file",
            "webpush_vapid_subject",
            "webpush_allowed_hosts",
        ),
    ),
    (
        "files",
        "Arquivos",
        (
            "files_backend",
            "files_root",
            "files_shared",
            "files_s3_bucket",
            "files_s3_region",
            "files_s3_endpoint",
            "files_scanner",
            "files_clamav_address",
            "upload_max_bytes",
            "files_org_quota_bytes",
            "files_deleted_retention_days",
        ),
    ),
    (
        "limits",
        "Limites e retenção",
        (
            "organization_member_limit",
            "json_max_bytes",
            "package_max_bytes",
            "export_max_rows",
            "import_max_rows",
            "transfer_retention_days",
            "event_retention_days",
            "flows_form_retention_days",
            "job_succeeded_retention_days",
            "job_dead_retention_days",
            "webhook_retention_days",
            "notification_read_retention_days",
            "notification_unread_retention_days",
            "notification_delivery_retention_days",
        ),
    ),
    (
        "api",
        "Tokens de API",
        ("api_token_max_days", "api_token_max_per_owner", "api_token_rate_per_minute", "api_token_failures_per_minute"),
    ),
    (
        "billing",
        "Cobrança",
        (
            "billing_enabled",
            "billing_default_currency",
            "billing_invoice_prefix",
            "billing_payment_terms_days",
            "billing_grace_days",
            "billing_suspend_after_days",
            "billing_cancel_after_days",
            "billing_retry_schedule_days",
            "billing_pending_payment_hold_days",
            "billing_trial_requires_payment_method",
            "billing_usage_retention_days",
        ),
    ),
    ("apps", "Aplicativos e atualizações", ("bundled_apps", "bundled_retained", "update_repository")),
    ("observability", "Observabilidade", ("insights_enabled", "metrics_token", "sentry_dsn")),
)
_GROUP_OF = {key: group for group, _, keys in SETTING_GROUPS for key in keys}
_GROUPED = tuple(replace(spec, group=_GROUP_OF.get(spec.key, "")) for spec in _SPECS)
CATALOG: dict[str, SettingSpec] = {spec.key: spec for spec in _GROUPED}
BY_ENV: dict[str, SettingSpec] = {spec.env: spec for spec in _GROUPED}

# Never stored in the instance SQLite: they locate it, or must not persist (owner credential).
BOOTSTRAP_ENV = (
    "DJANGO_SETTINGS_MODULE",
    "SKELETON_INSTANCE_DB",
    "SKELETON_INSTANCE_KEY_FILE",
    "SKELETON_INSTANCE_MODE",
    "SKELETON_PROJECT_ROOT",
    "SKELETON_DEPLOYMENT_INVENTORY",
    "SKELETON_MAINTENANCE_MARKER",
    "SKELETON_DRAIN_MARKER",
    "SKELETON_NODE_ID",
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
