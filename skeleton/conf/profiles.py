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
    DATABASES = {"default": env.db("DATABASE_URL")}
    if DATABASES["default"]["ENGINE"] != "django.db.backends.postgresql":
        raise ImproperlyConfigured("Produção exige PostgreSQL; SQLite é apenas para setup/desenvolvimento.")
    DATABASES["default"].setdefault("OPTIONS", {})["options"] = "-c search_path=skeleton,public"
    DATABASES["default"]["ATOMIC_REQUESTS"] = False
    DATABASES["default"]["CONN_MAX_AGE"] = 60
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
    STORAGES = {"default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
                "staticfiles": {"BACKEND": "whitenoise.storage.CompressedManifestStaticFilesStorage"}}
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
    DATABASES = {"default": env.db("MIGRATION_DATABASE_URL", default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}")}
    DATABASES["default"]["ATOMIC_REQUESTS"] = False
    if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
        DATABASES["default"].setdefault("OPTIONS", {})["options"] = "-c search_path=skeleton,public"
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
    DATABASES = {"default": env.db("TEST_DATABASE_URL", default="sqlite:///:memory:")}
    if DATABASES["default"]["ENGINE"] == "django.db.backends.postgresql":
        DATABASES["default"].setdefault("OPTIONS", {})["options"] = "-c search_path=skeleton,public"
    EMAIL_BACKEND = "django.core.mail.backends.locmem.EmailBackend"
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.PBKDF2PasswordHasher"]
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
    }
    SOCIALACCOUNT_PROVIDERS = {}
    result = {key: value for key, value in locals().items() if key.isupper()}
    return result
