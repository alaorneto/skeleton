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
