from pathlib import Path
import environ

from skeleton.conf.paths import project_root
BASE_DIR = project_root()
env = environ.Env()
environ.Env.read_env(BASE_DIR / ".env", overwrite=False)
SECRET_KEY = env("DJANGO_SECRET_KEY", default="development-only-not-for-deployment")
DEBUG = False
ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1", "testserver"])
INSTALLED_APPS = [
    "skeleton.accounts.admin_config.PlatformAdminConfig", "django.contrib.auth", "django.contrib.contenttypes",
    "django.contrib.sessions", "django.contrib.messages", "django.contrib.staticfiles",
    "allauth", "allauth.account", "allauth.socialaccount", "allauth.mfa",
    "allauth.socialaccount.providers.openid_connect", "allauth.socialaccount.providers.github",
    "allauth.socialaccount.providers.saml", "rest_framework",
    "skeleton.accounts.apps.AccountsConfig", "skeleton.tenancy.apps.TenancyConfig",
    "skeleton.system.apps.SystemConfig", "skeleton.access.apps.AccessConfig",
    "skeleton.audit.apps.AuditConfig", "skeleton.web", "skeleton.api",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware", "whitenoise.middleware.WhiteNoiseMiddleware",
    "skeleton.common.http_logging.RequestCompletionMiddleware",
    "skeleton.api.health.HealthProbeMiddleware",
    "skeleton.deployment.MaintenanceMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware", "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware", "django.contrib.auth.middleware.AuthenticationMiddleware",
    "skeleton.accounts.middleware.AuthVersionMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware", "skeleton.tenancy.middleware.TenantContextMiddleware",
    "allauth.account.middleware.AccountMiddleware",
]
ROOT_URLCONF = "config.urls"
WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"
TEMPLATES = [{"BACKEND": "django.template.backends.django.DjangoTemplates", "DIRS": [BASE_DIR / "templates", Path(__file__).resolve().parents[1] / "web" / "templates"], "APP_DIRS": True,
              "OPTIONS": {"context_processors": ["django.template.context_processors.request",
                    "django.contrib.auth.context_processors.auth", "django.contrib.messages.context_processors.messages",
                    "skeleton.system.applications.application_navigation"]}}]
DATABASES = {"default": env.db("DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
DATABASES["default"]["ATOMIC_REQUESTS"] = False
if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
    DATABASES["default"].setdefault("OPTIONS", {})["options"] = "-c search_path=skeleton,public"
    DATABASES["default"]["CONN_MAX_AGE"] = 60
CACHES = {"default": env.cache("CACHE_URL", default="locmemcache://skeleton")}
EMAIL_CONFIG = env.email("EMAIL_URL", default="consolemail://")
vars().update(EMAIL_CONFIG)
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", default="skeleton@localhost")
AUTH_USER_MODEL = "sk_accounts.User"
AUTHENTICATION_BACKENDS = ["skeleton.accounts.backends.SkeletonAuthenticationBackend"]
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]
LANGUAGE_CODE = "pt-br"
LANGUAGES = [("pt-br", "Português"), ("en", "English")]
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True
STATIC_URL = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
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
SKELETON_REQUIRE_ADMIN_MFA = env.bool("SKELETON_REQUIRE_ADMIN_MFA", default=False)
MFA_ENCRYPTION_KEYS = env.list("MFA_ENCRYPTION_KEYS", default=[])
MFA_ADAPTER = "skeleton.accounts.adapters.EncryptedMFAAdapter"
MFA_SUPPORTED_TYPES = ["totp", "recovery_codes"]
MFA_ALLOW_UNVERIFIED_EMAIL = False
MFA_RECOVERY_CODES_SHOW_ONCE = True
MFA_TRUST_ENABLED = False
X_FRAME_OPTIONS = "DENY"
SKELETON_JSON_MAX_BYTES = env.int("SKELETON_JSON_MAX_BYTES", default=1048576)
if SKELETON_JSON_MAX_BYTES < 1:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured("SKELETON_JSON_MAX_BYTES must be positive.")
REST_FRAMEWORK = {
    "EXCEPTION_HANDLER": "skeleton.api.errors.exception_handler",
    "DEFAULT_AUTHENTICATION_CLASSES": ["rest_framework.authentication.SessionAuthentication"],
    "DEFAULT_PERMISSION_CLASSES": ["rest_framework.permissions.IsAuthenticated"],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_PARSER_CLASSES": ["skeleton.api.parsers.BoundedJSONParser"],
}
LOGGING = {"version": 1, "disable_existing_loggers": False,
           "formatters": {"json": {"()": "skeleton.common.logging.SecurityJSONFormatter"}},
           "handlers": {"console": {"class": "logging.StreamHandler", "formatter": "json"}},
           "root": {"handlers": ["console"], "level": "INFO"},
           "loggers": {
               "django": {"handlers": ["console"], "level": "INFO", "propagate": False},
               "django.server": {"handlers": ["console"], "level": "INFO", "propagate": False},
           }}

SOCIALACCOUNT_FORMS = {"signup": "skeleton.accounts.forms.InvitationSocialSignupForm"}
from skeleton.deployment import load_deployment_inventory
SKELETON_DEPLOYMENT_INVENTORY = env("SKELETON_DEPLOYMENT_INVENTORY", default=None)
SKELETON_DEPLOYMENT_STATE = load_deployment_inventory(SKELETON_DEPLOYMENT_INVENTORY)
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
INSTALLED_APPS += list(SKELETON_DEPLOYMENT_STATE.installed_apps)
