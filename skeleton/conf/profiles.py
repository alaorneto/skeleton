"""Apply environment profiles to a project overlay without copying defaults."""


def development_settings(base):
    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = base["DEBUG"]
    DEBUG = True
    from skeleton.accounts.providers import load_provider_config

    SKELETON_PROVIDER_CONFIGURATION = load_provider_config(env("SSO_PROVIDER_CONFIG_PATH", default=None), debug=DEBUG)
    SOCIALACCOUNT_PROVIDERS = SKELETON_PROVIDER_CONFIGURATION.settings
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result


def production_settings(base):
    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = base["DEBUG"]
    from django.core.exceptions import ImproperlyConfigured

    SECRET_KEY = env("DJANGO_SECRET_KEY")
    ALLOWED_HOSTS = env.list("DJANGO_ALLOWED_HOSTS")
    from skeleton.conf.base import database_config

    database = database_config(env, "DATABASE_URL")
    if database is None or database["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("Produção exige DATABASE_URL apontando para PostgreSQL.")
    DATABASES = {"default": database}
    CACHES = {"default": env.cache("CACHE_URL")}
    if CACHES["default"]["BACKEND"] != "django.core.cache.backends.redis.RedisCache":
        raise ImproperlyConfigured("Produção exige Redis compartilhado.")
    EMAIL_CONFIG = env.email("EMAIL_URL")
    if EMAIL_CONFIG["EMAIL_BACKEND"] != "django.core.mail.backends.smtp.EmailBackend":
        raise ImproperlyConfigured("Produção exige SMTP configurado.")
    DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL")
    MFA_ENCRYPTION_KEYS = env.list("MFA_ENCRYPTION_KEYS")
    if not MFA_ENCRYPTION_KEYS or not ALLOWED_HOSTS or "*" in ALLOWED_HOSTS:
        raise ImproperlyConfigured("Chaves MFA e hosts explícitos são obrigatórios.")
    # Required as soon as an identity provider stores a secret (check skeleton.E027 on the database).
    IDENTITY_PROVIDER_ENCRYPTION_KEYS = env.list("IDENTITY_PROVIDER_ENCRYPTION_KEYS", default=[])
    SKELETON_REQUIRE_ADMIN_MFA = env.bool("SKELETON_REQUIRE_ADMIN_MFA", default=True)
    SECURE_SSL_REDIRECT = True
    SECURE_REDIRECT_EXEMPT = [r"^health/(live|ready|release)/$"]
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_HSTS_SECONDS = 31536000
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_HSTS_PRELOAD = True
    SECURE_CONTENT_TYPE_NOSNIFF = True
    X_FRAME_OPTIONS = "DENY"
    CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
    from django.http.request import validate_host

    SKELETON_HEALTH_HOST = env("DJANGO_HEALTH_HOST", default=ALLOWED_HOSTS[0].lstrip("."))
    if not validate_host(SKELETON_HEALTH_HOST, ALLOWED_HOSTS):
        raise ImproperlyConfigured("Health Host must be an explicitly allowed host.")
    from skeleton.conf.base import private_files_storage

    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        "skeleton_files": private_files_storage(env, BASE_DIR),
    }
    if env.bool("DJANGO_TRUST_PROXY", default=False):
        SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    from skeleton.accounts.providers import load_provider_config

    SKELETON_PROVIDER_CONFIGURATION = load_provider_config(env("SSO_PROVIDER_CONFIG_PATH", default=None), debug=DEBUG)
    SOCIALACCOUNT_PROVIDERS = SKELETON_PROVIDER_CONFIGURATION.settings
    result = {key: value for key, value in locals().items() if key.isupper()}
    result.update(EMAIL_CONFIG)
    return result


def migrate_settings(base):
    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = base["DEBUG"]
    from skeleton.conf.base import database_config

    # The owner credential is transient: environment only, never the instance store.
    database = database_config(env, "MIGRATION_DATABASE_URL", conn_max_age=0)
    DATABASES = {"default": database} if database else {}
    # Migrations do not authenticate or contact IdPs; runtime settings own provider configuration.
    SOCIALACCOUNT_PROVIDERS = {}

    from skeleton.deployment import migration_archive_configs

    archive_configs, archive_modules = migration_archive_configs(base["SKELETON_DEPLOYMENT_STATE"])
    MIGRATION_MODULES = {**base.get("MIGRATION_MODULES", {}), **archive_modules}
    INSTALLED_APPS = [*base["INSTALLED_APPS"], *archive_configs]
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result


def test_settings(base):
    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = base["DEBUG"]
    DEBUG = True
    from skeleton.conf.base import database_config

    # The suite runs on PostgreSQL with the runtime role (#145); tools/dev_postgres.py provides one.
    database = database_config(env, "TEST_DATABASE_URL", conn_max_age=0)
    DATABASES = {"default": database} if database else {}
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    SKELETON_METRICS_QUEUE_TTL_S = 0  # The suite always reads the queue.
    import tempfile

    STORAGES = {
        **base["STORAGES"],
        "skeleton_files": {
            "BACKEND": "django.core.files.storage.FileSystemStorage",
            "OPTIONS": {"location": tempfile.mkdtemp(prefix="skeleton-files-"), "base_url": None},
        },
    }
    # Same algorithm and hash format as production with a token work factor: the production
    # iterations cost a third of a second per fixture password.
    PASSWORD_HASHERS = ["skeleton.testing.hashers.FastPBKDF2PasswordHasher"]
    from skeleton.accounts.providers import load_provider_config

    SKELETON_PROVIDER_CONFIGURATION = load_provider_config(env("SSO_PROVIDER_CONFIG_PATH", default=None), debug=DEBUG)
    SOCIALACCOUNT_PROVIDERS = SKELETON_PROVIDER_CONFIGURATION.settings
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result


def build_settings(base):
    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = base["DEBUG"]
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"},
        "skeleton_files": base["STORAGES"]["skeleton_files"],
    }
    SOCIALACCOUNT_PROVIDERS = {}
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result


def setup_settings(base):
    """First-access setup (#146): no database, signed-cookie session, only /setup/ and health.

    The session key is derived from the instance master key (stable across workers and
    restarts, never stored); the single-use token is issued here, at process start.
    """
    import hashlib
    import hmac

    from skeleton.instance import crypto
    from skeleton.instance.writer import open_store
    from skeleton.setup.tokens import issue_token

    env = base["env"]
    BASE_DIR = base["BASE_DIR"]
    DEBUG = env.bool("DJANGO_DEBUG", default=False)
    SKELETON_SETUP_MODE = True
    store, key_path = open_store(BASE_DIR)
    crypto.load_key(key_path, create=True)
    SECRET_KEY = hmac.new(key_path.read_bytes().strip(), b"skeleton-setup-session", hashlib.sha256).hexdigest()
    issue_token(store)
    DATABASES = {}
    ALLOWED_HOSTS = env.list(
        "SKELETON_SETUP_ALLOWED_HOSTS", default=env.list("DJANGO_ALLOWED_HOSTS", default=["localhost", "127.0.0.1"])
    )
    ROOT_URLCONF = "skeleton.setup.urls"
    SESSION_ENGINE = "django.contrib.sessions.backends.signed_cookies"
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Strict"
    SESSION_COOKIE_AGE = 3600
    SESSION_COOKIE_SECURE = env.bool("SKELETON_SETUP_SECURE_COOKIES", default=not DEBUG)
    CSRF_COOKIE_SECURE = SESSION_COOKIE_SECURE
    CSRF_TRUSTED_ORIGINS = env.list("DJANGO_CSRF_TRUSTED_ORIGINS", default=[])
    MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"
    CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "setup"}}
    MIDDLEWARE = [
        "django.middleware.security.SecurityMiddleware",
        "django.middleware.csp.ContentSecurityPolicyMiddleware",
        "whitenoise.middleware.WhiteNoiseMiddleware",
        "skeleton.api.health.HealthProbeMiddleware",
        "skeleton.setup.middleware.SetupOnlyMiddleware",
        "django.middleware.clickjacking.XFrameOptionsMiddleware",
        "django.contrib.sessions.middleware.SessionMiddleware",
        "django.middleware.common.CommonMiddleware",
        "django.middleware.csrf.CsrfViewMiddleware",
        # Lazy anonymous user (the setup session never holds one); allauth refuses to load without
        # its middleware, which then finds nothing to do.
        "django.contrib.auth.middleware.AuthenticationMiddleware",
        "django.contrib.messages.middleware.MessageMiddleware",
        "allauth.account.middleware.AccountMiddleware",
    ]
    X_FRAME_OPTIONS = "DENY"
    SECURE_CONTENT_TYPE_NOSNIFF = True
    SOCIALACCOUNT_PROVIDERS = {}
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result
