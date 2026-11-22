"""Environment checks: PostgreSQL only (E025), a shared cache in production (W029) and its prefix (W049/E049)."""

from django.core.checks import Error, Tags, Warning, register

POSTGRESQL_ENGINE = "django.db.backends.postgresql"


@register()
def postgresql_only_checks(app_configs=None, **kwargs):
    """skeleton.E025: every DATABASES alias is PostgreSQL (#144); data never lives in SQLite.

    The instance configuration store is a separate SQLite file read before Django, never an alias.
    """
    from django.conf import settings

    dummy = "django.db.backends.dummy"  # What Django makes of an empty DATABASES.
    # The setup and the build profile run without a database on purpose; every other profile needs one.
    setup = getattr(settings, "SKELETON_SETUP_MODE", False) or getattr(settings, "SKELETON_BUILD_PROFILE", False)
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


@register(Tags.security, deploy=True)
def cache_prefix_checks(app_configs=None, **kwargs):
    """skeleton.W049/E049: a Redis without KEY_PREFIX collides with any other instance on it (#675)."""
    from django.conf import settings

    cache = settings.CACHES.get("default", {})
    if not cache.get("BACKEND", "").endswith("redis.RedisCache") or cache.get("KEY_PREFIX"):
        return []
    hint = "Set SKELETON_CACHE_KEY_PREFIX (or ?key_prefix= in CACHE_URL) to a name of this instance."
    if getattr(settings, "SKELETON_INSTANCE_MODE", "standalone") == "cluster":
        return [Error("The shared cache has no key prefix in cluster mode.", hint=hint, id="skeleton.E049")]
    return [
        Warning(
            "The shared cache has no key prefix: another instance on it would share keys.",
            hint=hint,
            id="skeleton.W049",
        )
    ]


def multi_host_problems(databases) -> list:
    """skeleton.W050: several hosts without ``target_session_attrs`` may land on a read-only replica (#676)."""
    problems = []
    for alias, config in databases.items():
        if "," in str(config.get("HOST") or "") and not (config.get("OPTIONS") or {}).get("target_session_attrs"):
            problems.append(
                Warning(
                    f"DATABASES[{alias!r}] lists several hosts without target_session_attrs.",
                    hint="Add ?target_session_attrs=read-write to the URL, or a connection may reach a read-only replica.",
                    id="skeleton.W050",
                )
            )
    return problems


@register(deploy=True)
def multi_host_checks(app_configs=None, **kwargs):
    from django.conf import settings

    return multi_host_problems(settings.DATABASES)


def pooler_search_path_problems(alias, cursor) -> list:
    cursor.execute("SHOW search_path")
    path = [item.strip().strip('"') for item in cursor.fetchone()[0].split(",")]
    if path and path[0] == "skeleton":
        return []
    return [
        Error(
            f"DATABASES[{alias!r}] behind a transaction pooler: the role's search_path does not start with skeleton.",
            hint="ALTER ROLE skeleton_runtime SET search_path TO skeleton, public (deploy/postgres/bootstrap.sql).",
            id="skeleton.E050",
        )
    ]


@register("database")
def pooler_checks(app_configs=None, databases=None, **kwargs):
    """skeleton.E050: behind a transaction pooler no startup options go out, so the role needs its search_path."""
    from django.conf import settings
    from django.db import connections

    problems = []
    for alias in databases or ():
        config = settings.DATABASES.get(alias, {})
        if config.get("ENGINE") != POSTGRESQL_ENGINE or not config.get("DISABLE_SERVER_SIDE_CURSORS"):
            continue
        with connections[alias].cursor() as cursor:
            problems.extend(pooler_search_path_problems(alias, cursor))
    return problems
