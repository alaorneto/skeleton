from pathlib import Path

import environ

from skeleton.conf.mailers import mailer_from_url
from skeleton.conf.paths import project_root
from skeleton.instance.resolve import instance_env

BASE_DIR = project_root()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)
# Catalog keys resolve environment > instance SQLite > default (#143); other names read the environment.
env = instance_env(BASE_DIR)
SECRET_KEY = env("DJANGO_SECRET_KEY", default="development-only-not-for-deployment")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "testserver"])
INSTALLED_APPS = [
    # Technical dependency only: LogEntry and the historical migrations that grant on
    # django_admin_log. No admin site, autodiscovery or route exists; platform
    # administration lives in skeleton.web.platform (/platform/).
    "django.contrib.admin.apps.SimpleAdminConfig",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "allauth",
    "allauth.account",
    "allauth.socialaccount",
    "allauth.mfa",
    "allauth.socialaccount.providers.openid_connect",
    "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.saml",
    "rest_framework",
    "crispy_forms",
    "skeleton.accounts.apps.AccountsConfig",
    "skeleton.tenancy.apps.TenancyConfig",
    "skeleton.system.apps.SystemConfig",
    "skeleton.access.apps.AccessConfig",
    "skeleton.audit.apps.AuditConfig",
    "skeleton.jobs.apps.JobsConfig",
    "skeleton.events.apps.EventsConfig",
    "skeleton.notifications.apps.NotificationsConfig",
    "skeleton.files.apps.FilesConfig",
    "skeleton.integrations.apps.IntegrationsConfig",
    "skeleton.sequences.apps.SequencesConfig",
    "skeleton.transfer.apps.TransferConfig",
    "skeleton.flows.apps.FlowsConfig",
    "skeleton.comments.apps.CommentsConfig",
    "skeleton.billing.apps.BillingConfig",
    "skeleton.instance.apps.InstanceConfig",
    "skeleton.observability.apps.ObservabilityConfig",
    "skeleton.web",
    "skeleton.api",
]
CRISPY_TEMPLATE_PACK = "skeleton_crud"
CRISPY_ALLOWED_TEMPLATE_PACKS = ("skeleton_crud",)
CRISPY_FAIL_SILENTLY = False
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.csp.ContentSecurityPolicyMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "skeleton.common.http_logging.RequestCompletionMiddleware",
    # Serves /metrics before session, auth and tenant; removes itself without SKELETON_METRICS_TOKEN (#175).
    "skeleton.observability.metrics.MetricsMiddleware",
    "skeleton.api.health.HealthProbeMiddleware",
    "skeleton.deployment.MaintenanceMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "skeleton.accounts.middleware.AuthVersionMiddleware",
    "skeleton.integrations.tokens.middleware.ApiTokenMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "skeleton.tenancy.middleware.TenantContextMiddleware",
    "allauth.account.middleware.AccountMiddleware",
    # After the tenant context: reads the organization defaults under RLS (#170).
    "skeleton.common.locale.LocaleMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates", Path(__file__).resolve().parents[1] / "web" / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.csp",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "skeleton.system.applications.application_navigation",
                "skeleton.web.navigation.organization_shell",
                "skeleton.accounts.platform.platform_admin_context",
            ]
        },
    }
]


def database_config(env, variable, *, conn_max_age=60):
    """``DATABASES`` entry of a PostgreSQL URL; empty when the variable is unset (setup, #146).

    The data layer is PostgreSQL only (#144): another engine stays in the config only so that
    check ``skeleton.E025`` can refuse it with a clear message. SQLite is never a fallback.
    """
    if not env.str(variable, default=""):
        return None
    config = env.db(variable)
    config["ATOMIC_REQUESTS"] = False
    if config["ENGINE"] == "django.db.backends.postgresql":
        config.setdefault("OPTIONS", {})["options"] = "-c search_path=skeleton,public"
        config["CONN_MAX_AGE"] = conn_max_age
        # A persistent connection may have been closed by the server: probe it before reuse (#333).
        config["CONN_HEALTH_CHECKS"] = conn_max_age != 0
    return config


_database = database_config(env, "DATABASE_URL")
DATABASES = {"default": _database} if _database else {}
CACHES = {"default": env.cache("CACHE_URL", default="locmemcache://skeleton")}
# EMAIL_URL stays the operation interface; it becomes MAILERS["default"] (#127), never the
# per-connection EMAIL_* settings Django 6.1 deprecated. EMAIL_TIMEOUT (seconds an SMTP
# connection may block, so the notification jobs never hang on a mute server, #297) is read
# from the environment into the SMTP OPTIONS.
MAILERS = {
    "default": mailer_from_url(env("EMAIL_URL", default="consolemail://"), timeout=env.int("EMAIL_TIMEOUT", default=30))
}
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="skeleton@localhost")
AUTH_USER_MODEL = "sk_accounts.User"
AUTHENTICATION_BACKENDS = ["skeleton.accounts.backends.SkeletonAuthenticationBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
# The core ships pt-BR only (msgids are pt-BR); derived projects add languages with their
# catalogs in LOCALE_PATHS (#85). Defaults apply to people and organizations without a choice.
SKELETON_DEFAULT_LANGUAGE = env("SKELETON_DEFAULT_LANGUAGE", default="pt-br")
SKELETON_DEFAULT_TIMEZONE = env("SKELETON_DEFAULT_TIMEZONE", default="UTC")
LANGUAGE_CODE = SKELETON_DEFAULT_LANGUAGE
LANGUAGES = [("pt-br", "Português")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"


def private_files_storage(env, base_dir):
    """STORAGES["skeleton_files"]: local (default) or S3 with the optional extra ``[s3]``; never public."""
    if env("SKELETON_FILES_BACKEND", default="local") == "s3":
        return {
            "BACKEND": "storages.backends.s3.S3Storage",
            "OPTIONS": {
                "bucket_name": env("SKELETON_FILES_S3_BUCKET"),
                "region_name": env("SKELETON_FILES_S3_REGION", default=None),
                "endpoint_url": env("SKELETON_FILES_S3_ENDPOINT", default=None),
                "default_acl": "private",
                "querystring_auth": True,
                "querystring_expire": 60,
                "file_overwrite": False,
            },
        }
    return {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
        "OPTIONS": {"location": env("SKELETON_FILES_ROOT", default=str(base_dir / "var" / "files")), "base_url": None},
    }


STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage"},
    "skeleton_files": private_files_storage(env, BASE_DIR),
}
SKELETON_UPLOAD_MAX_BYTES = env.int("SKELETON_UPLOAD_MAX_BYTES", default=10 * 1024 * 1024)
# The bounded handler comes first: no multipart body passes SKELETON_UPLOAD_MAX_BYTES into memory
# or the temporary directory (#242); the package upload swaps it for its own larger bound.
FILE_UPLOAD_HANDLERS = [
    "skeleton.common.uploads.BoundedUploadHandler",
    "django.core.files.uploadhandler.MemoryFileUploadHandler",
    "django.core.files.uploadhandler.TemporaryFileUploadHandler",
]
# Default quota of live attachment bytes per organization (empty: unlimited); the platform overrides it.
SKELETON_FILES_ORG_QUOTA_BYTES = env.int("SKELETON_FILES_ORG_QUOTA_BYTES", default=0) or None
# Deleted attachments keep their bytes this long, then files.purge removes them (#192).
SKELETON_FILES_DELETED_RETENTION_DAYS = env.int("SKELETON_FILES_DELETED_RETENTION_DAYS", default=90)
# Form data of closed flow instances is emptied after this many days; 0 keeps it (#422).
SKELETON_FLOWS_FORM_RETENTION_DAYS = env.int("SKELETON_FLOWS_FORM_RETENTION_DAYS", default=0)
# Antivirus (#62): "none" (files are available at once) or "clamav" (clamd INSTREAM).
SKELETON_FILES_SCANNER = env("SKELETON_FILES_SCANNER", default="none")
SKELETON_FILES_CLAMAV_ADDRESS = env("SKELETON_FILES_CLAMAV_ADDRESS", default="")
SKELETON_FILES_SCAN = SKELETON_FILES_SCANNER != "none"
# Subscriptions and payments of the platform sold as SaaS (#394); off: every gate allows everything.
SKELETON_BILLING_ENABLED = env.bool("SKELETON_BILLING_ENABLED", default=False)
# Text before the year of invoice numbers ("ACME-" gives ACME-2026-000001).
SKELETON_BILLING_INVOICE_PREFIX = env("SKELETON_BILLING_INVOICE_PREFIX", default="")
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
SESSION_ENGINE = "django.contrib.sessions.backends.db"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
CSRF_COOKIE_SAMESITE = "Lax"
LOGIN_URL = "/accounts/login/"
LOGIN_REDIRECT_URL = "/"
ACCOUNT_LOGOUT_REDIRECT_URL = "/accounts/login/"
ACCOUNT_USER_MODEL_USERNAME_FIELD = None
ACCOUNT_USER_MODEL_EMAIL_FIELD = "email"
ACCOUNT_LOGIN_METHODS = {"email"}
ACCOUNT_SIGNUP_FIELDS = ["email*", "password1*", "password2*"]
ACCOUNT_EMAIL_VERIFICATION = "mandatory"
SOCIALACCOUNT_EMAIL_VERIFICATION = "mandatory"
ACCOUNT_UNIQUE_EMAIL = True
ACCOUNT_PREVENT_ENUMERATION = True
ACCOUNT_EMAIL_UNKNOWN_ACCOUNTS = False
ACCOUNT_LOGOUT_ON_GET = False
ACCOUNT_CONFIRM_EMAIL_ON_GET = False
SOCIALACCOUNT_LOGIN_ON_GET = False
ACCOUNT_LOGIN_ON_PASSWORD_RESET = False
ACCOUNT_REAUTHENTICATION_REQUIRED = True
ACCOUNT_ADAPTER = "skeleton.accounts.adapters.AccountAdapter"
SOCIALACCOUNT_ADAPTER = "skeleton.accounts.adapters.SocialAccountAdapter"
ACCOUNT_FORMS = {
    "signup": "skeleton.accounts.forms.InvitationSignupForm",
    "reset_password": "skeleton.accounts.forms.PolicyResetPasswordForm",
    "reset_password_from_key": "skeleton.accounts.forms.PolicyResetPasswordKeyForm",
    "change_password": "skeleton.accounts.forms.PolicyChangePasswordForm",
    "set_password": "skeleton.accounts.forms.PolicySetPasswordForm",
}
SOCIALACCOUNT_EMAIL_AUTHENTICATION = False
SOCIALACCOUNT_EMAIL_AUTHENTICATION_AUTO_CONNECT = False
SOCIALACCOUNT_STORE_TOKENS = False
from skeleton.api.proxy import proxy_settings

globals().update(proxy_settings(env))
SKELETON_PUBLIC_SIGNUP = env.bool("SKELETON_PUBLIC_SIGNUP", default=False)
SKELETON_EVENT_RETENTION_DAYS = env.int("SKELETON_EVENT_RETENTION_DAYS", default=30)
# CSV export and import (#182, #183).
SKELETON_EXPORT_MAX_ROWS = env.int("SKELETON_EXPORT_MAX_ROWS", default=100_000)
SKELETON_IMPORT_MAX_ROWS = env.int("SKELETON_IMPORT_MAX_ROWS", default=10_000)
SKELETON_TRANSFER_RETENTION_DAYS = env.int("SKELETON_TRANSFER_RETENTION_DAYS", default=7)
SKELETON_WEBHOOK_RETENTION_DAYS = env.int("SKELETON_WEBHOOK_RETENTION_DAYS", default=30)
# API tokens (#45): maximum validity in days of a newly issued token.
SKELETON_API_TOKEN_MAX_DAYS = env.int("SKELETON_API_TOKEN_MAX_DAYS", default=365)
# Live tokens per person or service account; expired ones count until revoked (#327).
SKELETON_API_TOKEN_MAX_PER_OWNER = env.int("SKELETON_API_TOKEN_MAX_PER_OWNER", default=50)
# Rate limits (#48): requests per token per minute and failed token authentications per IP per minute.
SKELETON_API_TOKEN_RATE_PER_MINUTE = env.int("SKELETON_API_TOKEN_RATE_PER_MINUTE", default=600)
SKELETON_API_TOKEN_FAILURES_PER_MINUTE = env.int("SKELETON_API_TOKEN_FAILURES_PER_MINUTE", default=20)
# Job retention (#40): finished and dead jobs are physically deleted after these windows.
SKELETON_JOB_SUCCEEDED_RETENTION_DAYS = env.int("SKELETON_JOB_SUCCEEDED_RETENTION_DAYS", default=7)
SKELETON_JOB_DEAD_RETENTION_DAYS = env.int("SKELETON_JOB_DEAD_RETENTION_DAYS", default=30)
# Origin of links in notification emails and pushes (https://app.example.com); jobs have no request.
SKELETON_PUBLIC_URL = env("SKELETON_PUBLIC_URL", default="")

# Observability (#87): each piece stays off until its setting is present and its extra installed.
# Bearer token of /metrics (extra [metrics], #175); without it the endpoint does not exist.
SKELETON_METRICS_TOKEN = env("SKELETON_METRICS_TOKEN", default="")
# Seconds one process reuses the queue figures between scrapes; 0 reads the database on every scrape (#275).
SKELETON_METRICS_QUEUE_TTL_S = 15
# Sentry DSN (extra [sentry], #177); without it Sentry is never initialized. Tracing (#176) has no
# setting here: the SDK reads the standard OTEL_* variables from the environment.
SKELETON_SENTRY_DSN = env("SKELETON_SENTRY_DSN", default="")

# Notification retention (#57): read/archived rows, unread rows and finished deliveries.
SKELETON_NOTIFICATION_READ_RETENTION_DAYS = env.int("SKELETON_NOTIFICATION_READ_RETENTION_DAYS", default=90)
SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS = env.int("SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS", default=180)
SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS = env.int("SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS", default=30)
# Web push (#56): off until a P-256 key file (manage.py generate_vapid_keys) and a subject exist.
WEBPUSH_VAPID_PRIVATE_KEY_FILE = env("WEBPUSH_VAPID_PRIVATE_KEY_FILE", default="")
WEBPUSH_VAPID_SUBJECT = env("WEBPUSH_VAPID_SUBJECT", default="")
WEBPUSH_ALLOWED_HOSTS = env.list(
    "WEBPUSH_ALLOWED_HOSTS",
    default=[
        "fcm.googleapis.com",
        "updates.push.services.mozilla.com",
        "*.notify.windows.com",
        "web.push.apple.com",
        "*.push.apple.com",
    ],
)
SKELETON_REQUIRE_ADMIN_MFA = env.bool("SKELETON_REQUIRE_ADMIN_MFA", default=False)
MFA_ENCRYPTION_KEYS = env.list("MFA_ENCRYPTION_KEYS", default=[])
# Separate keyring for identity provider secrets (client secrets, SAML keys, LDAP passwords):
# the first key encrypts, all decrypt; never derived from SECRET_KEY.
IDENTITY_PROVIDER_ENCRYPTION_KEYS = env.list("IDENTITY_PROVIDER_ENCRYPTION_KEYS", default=[])
# Separate keyring for integration secrets (webhook signing secrets, #63); never derived from SECRET_KEY.
INTEGRATION_ENCRYPTION_KEYS = env.list("INTEGRATION_ENCRYPTION_KEYS", default=[])
# Separate keyring for payment provider secrets (#394, #400); never derived from SECRET_KEY.
BILLING_ENCRYPTION_KEYS = env.list("BILLING_ENCRYPTION_KEYS", default=[])
MFA_ADAPTER = "skeleton.accounts.adapters.EncryptedMFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_ALLOW_UNVERIFIED_EMAIL = False
MFA_RECOVERY_CODES_SHOW_ONCE = True
MFA_TRUST_ENABLED = False
X_FRAME_OPTIONS = "DENY"
from django.utils.csp import CSP as _CSP  # noqa: E402  Underscore: the overlay's star import must not export it.

# Same-origin only (#266). No inline script exists in the core templates (json_script blocks
# are data); the only inline style is the organization's brand palette, allowed by nonce.
# No form-action: SSO starts with a POST to this origin that redirects to the identity
# provider, and Chromium applies form-action to the redirect target too.
SKELETON_CSP = {
    "default-src": [_CSP.SELF],
    "script-src": [_CSP.SELF],
    "style-src": [_CSP.SELF, _CSP.NONCE],
    "img-src": [_CSP.SELF, "data:"],  # allauth renders the TOTP QR code as a data: image.
    "font-src": [_CSP.SELF],
    "connect-src": [_CSP.SELF],
    "object-src": [_CSP.NONE],
    "base-uri": [_CSP.SELF],
    "frame-ancestors": [_CSP.NONE],
}
SECURE_CSP = SKELETON_CSP
SKELETON_JSON_MAX_BYTES = env.int("SKELETON_JSON_MAX_BYTES", default=1048576)
if SKELETON_JSON_MAX_BYTES < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SKELETON_JSON_MAX_BYTES must be positive.")
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "skeleton.api.errors.exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    # JSON only: the DRF browsable API (HTML) is never served (docs/api.md, #230).
    "DEFAULT_RENDERER_CLASSES": ["rest_framework.renderers.JSONRenderer"],
    "DEFAULT_PARSER_CLASSES": ["skeleton.api.parsers.BoundedJSONParser"],
}
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {"json": {"()": "skeleton.common.logging.SecurityJSONFormatter"}},
    "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
    "root": {"handlers": ["console"], "level": "INFO"},
    "loggers": {
        "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
        "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
    },
}

SOCIALACCOUNT_FORMS = {"signup": "skeleton.accounts.forms.InvitationSocialSignupForm"}
from skeleton.deployment import load_deployment_inventory

# Read before the inventory: a managed app may depend on an installed first-party app (#254).
SKELETON_BUNDLED_APPS = tuple(env.list("SKELETON_BUNDLED_APPS", default=[]))
SKELETON_DEPLOYMENT_INVENTORY = env("SKELETON_DEPLOYMENT_INVENTORY", default=None)
SKELETON_DEPLOYMENT_STATE = load_deployment_inventory(SKELETON_DEPLOYMENT_INVENTORY, bundled_apps=SKELETON_BUNDLED_APPS)
SKELETON_RETAINED_SCHEMA_PATHS = SKELETON_DEPLOYMENT_STATE.retained_schema_paths
SKELETON_MIGRATION_ARCHIVES = SKELETON_DEPLOYMENT_STATE.migration_archives
SKELETON_FROZEN_INFRASTRUCTURE = SKELETON_DEPLOYMENT_STATE.frozen_infrastructure
SKELETON_MAINTENANCE_MARKER = env("SKELETON_MAINTENANCE_MARKER", default=None)
SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE = env("SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE", default=None)
SKELETON_DEPLOYMENT_SMOKE_PATHS = tuple(env.list("SKELETON_DEPLOYMENT_SMOKE_PATHS", default=[]))
if any(not path.startswith("/") or "?" in path or "#" in path for path in SKELETON_DEPLOYMENT_SMOKE_PATHS):
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("Deployment smoke paths must be exact URL paths.")
SKELETON_AGENT_SOCKET = env("SKELETON_AGENT_SOCKET", default=None)
SKELETON_AGENT_TOKEN_FILE = env("SKELETON_AGENT_TOKEN_FILE", default=None)
SKELETON_PACKAGE_MAX_BYTES = env.int("SKELETON_PACKAGE_MAX_BYTES", default=128 * 1024 * 1024)
if SKELETON_PACKAGE_MAX_BYTES < 1:
    from django.core.exceptions import ImproperlyConfigured

    raise ImproperlyConfigured("SKELETON_PACKAGE_MAX_BYTES must be positive.")
# First-party apps of the core wheel (#66): opt-in by name from the fixed map, never by import path.
# They come before the managed apps: a managed app may depend on them, never the reverse (#254).
from skeleton.conf.bundled import bundled_app_configs

INSTALLED_APPS += bundled_app_configs(SKELETON_BUNDLED_APPS)
INSTALLED_APPS += list(SKELETON_DEPLOYMENT_STATE.installed_apps)
# Opted out after migrating (#67): the tables stay, checked against the snapshot in the wheel.
from skeleton.conf.bundled import retained_snapshot_paths

SKELETON_BUNDLED_RETAINED = tuple(env.list("SKELETON_BUNDLED_RETAINED", default=[]))
SKELETON_RETAINED_SCHEMA_PATHS = (
    *SKELETON_RETAINED_SCHEMA_PATHS,
    *retained_snapshot_paths(SKELETON_BUNDLED_RETAINED, installed=SKELETON_BUNDLED_APPS),
)
