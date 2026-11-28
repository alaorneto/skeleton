"""Host of the container healthcheck in setup and normal mode, without Django (#148)."""

from skeleton.api.healthcheck import health_host
from skeleton.instance.writer import write_setting


def test_health_host_follows_the_mode_and_the_instance_configuration(tmp_path, monkeypatch, settings):
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    for name in ("DJANGO_SECRET_KEY", "DJANGO_ALLOWED_HOSTS", "DATABASE_URL", "DJANGO_HEALTH_HOST", "SKELETON_SETUP"):
        monkeypatch.delenv(name, raising=False)
    assert health_host(tmp_path) == "localhost"  # Setup mode.
    write_setting(settings.BASE_DIR, "secret_key", "abcdefghij" * 5, source="test")
    write_setting(settings.BASE_DIR, "allowed_hosts", ".app.example.test,other.example.test", source="test")
    write_setting(settings.BASE_DIR, "database_url", "postgres://u:p@db/x", source="test")
    assert health_host(tmp_path) == "app.example.test"
    write_setting(settings.BASE_DIR, "health_host", "health.example.test", source="test")
    assert health_host(tmp_path) == "health.example.test"
    monkeypatch.setenv("DJANGO_HEALTH_HOST", "env.example.test")
    assert health_host(tmp_path) == "env.example.test"


def test_health_host_skips_wildcards_and_respects_cluster_mode(tmp_path, monkeypatch, settings):
    """#913: wildcard ALLOWED_HOSTS falls back to localhost and cluster mode ignores SQLite."""
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    for name in (
        "DJANGO_SECRET_KEY",
        "DJANGO_ALLOWED_HOSTS",
        "DATABASE_URL",
        "DJANGO_HEALTH_HOST",
        "SKELETON_SETUP",
        "SKELETON_INSTANCE_MODE",
    ):
        monkeypatch.delenv(name, raising=False)
    write_setting(settings.BASE_DIR, "secret_key", "abcdefghij" * 5, source="test")
    write_setting(settings.BASE_DIR, "allowed_hosts", "*", source="test")
    write_setting(settings.BASE_DIR, "database_url", "postgres://u:p@db/x", source="test")
    assert health_host(tmp_path) == "localhost"

    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "*, .second.example.test")
    assert health_host(tmp_path) == "second.example.test"

    write_setting(settings.BASE_DIR, "health_host", "stored.example.test", source="test")
    assert health_host(tmp_path) == "stored.example.test"
    monkeypatch.setenv("SKELETON_INSTANCE_MODE", "cluster")
    monkeypatch.setenv("DJANGO_ALLOWED_HOSTS", "cluster.example.test")
    assert health_host(tmp_path) == "cluster.example.test"
