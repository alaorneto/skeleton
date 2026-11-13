"""Environment checks: PostgreSQL only (E025) and a shared cache in production (W029)."""

from django.core.checks import Error, Tags, Warning, register

POSTGRESQL_ENGINE = "django.db.backends.postgresql"


@register()
def postgresql_only_checks(app_configs=None, **kwargs):
    """skeleton.E025: every DATABASES alias is PostgreSQL (#144); data never lives in SQLite.

    The instance configuration store is a separate SQLite file read before Django, never an alias.
    """
    from django.conf import settings

    dummy = "django.db.backends.dummy"  # What Django makes of an empty DATABASES.
    setup = getattr(settings, "SKELETON_SETUP_MODE", False)
    errors = [
        Error(
            f"DATABASES[{alias!r}] uses {config.get('ENGINE', '')!r}: the data layer is PostgreSQL only.",
            hint="Point DATABASE_URL at PostgreSQL (postgres://...); SQLite is only the instance configuration store.",
            id="skeleton.E025",
        )
        for alias, config in settings.DATABASES.items()
        if config.get("ENGINE") not in (POSTGRESQL_ENGINE, dummy)
    ]
    configured = [config for config in settings.DATABASES.values() if config.get("ENGINE") != dummy]
    if not configured and not setup:
        errors.append(
            Error(
                "No database is configured: DATABASE_URL (PostgreSQL) is required outside the setup.",
                hint="Run the first-access setup, `manage.py instance set database_url ...` or set DATABASE_URL.",
                id="skeleton.E025",
            )
        )
    return errors


@register(Tags.security, deploy=True)
def shared_cache_checks(app_configs=None, **kwargs):
    """skeleton.W029: rate limits (#48) need a cache shared by every worker."""
    from django.conf import settings

    backend = settings.CACHES.get("default", {}).get("BACKEND", "")
    if not settings.DEBUG and backend.endswith(("locmem.LocMemCache", "dummy.DummyCache")):
        return [
            Warning(
                "The default cache is local to each process: token rate limits are per worker, not global.",
                hint="Set CACHE_URL to a shared Redis.",
                id="skeleton.W029",
            )
        ]
    return []
