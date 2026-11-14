"""The setup only completes with values the target profile accepts (#457)."""

import pytest
from django.test import override_settings

from skeleton.setup import forms

STORAGE = {"files_backend": "local", "files_root": "/instance/files"}
EMAIL = {"default_from_email": "noreply@example.test", "test_recipient": "ops@example.test"}


@override_settings(SKELETON_SETUP_PRODUCTION=True)
def test_production_target_requires_a_shared_redis_cache(subtests):
    for value in ("", "locmemcache://", "dummycache://", "not a url"):
        with subtests.test(cache_url=value):
            form = forms.StorageForm({**STORAGE, "cache_url": value})
            assert not form.is_valid() and "cache_url" in form.errors
    for value in ("redis://:secret@redis:6379/0", "rediss://:secret@redis:6380/0"):
        with subtests.test(cache_url=value):
            assert forms.StorageForm({**STORAGE, "cache_url": value}).is_valid()


@override_settings(SKELETON_SETUP_PRODUCTION=False)
def test_development_target_keeps_the_local_cache():
    assert forms.StorageForm({**STORAGE, "cache_url": ""}).is_valid()


@override_settings(SKELETON_SETUP_PRODUCTION=True)
def test_production_target_requires_smtp(subtests):
    for value in ("consolemail://", "memorymail://", "filemail:///tmp/mail", "dummymail://"):
        with subtests.test(email_url=value):
            form = forms.EmailForm({**EMAIL, "email_url": value})
            assert not form.is_valid() and "email_url" in form.errors
    for value in ("smtp+tls://user:secret@smtp.example.test:587", "smtp+ssl://user:secret@smtp.example.test:465"):
        with subtests.test(email_url=value):
            assert forms.EmailForm({**EMAIL, "email_url": value}).is_valid()


@override_settings(SKELETON_SETUP_PRODUCTION=True)
def test_refusals_never_echo_the_secret():
    form = forms.StorageForm({**STORAGE, "cache_url": "locmemcache://secret-value"})
    email = forms.EmailForm({**EMAIL, "email_url": "consolemail://user:secret-value@host"})
    assert not form.is_valid() and not email.is_valid()
    assert "secret-value" not in str(form.errors) + str(email.errors)


@override_settings(SKELETON_SETUP_PRODUCTION=False)
def test_development_target_keeps_the_development_mailers():
    assert forms.EmailForm({**EMAIL, "email_url": "consolemail://"}).is_valid()


@pytest.mark.parametrize(
    ("default", "production"), [("config.settings.production", True), ("config.settings.development", False)]
)
def test_the_setup_knows_the_profile_it_replaces(tmp_path, monkeypatch, default, production):
    import environ

    from skeleton.conf.profiles import setup_settings
    from skeleton.setup import mode

    for name in ("DJANGO_SETTINGS_MODULE", "SKELETON_SETUP_TARGET", "DJANGO_SECRET_KEY", "DATABASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("SKELETON_INSTANCE_DB", str(tmp_path / "instance" / "instance.sqlite3"))
    monkeypatch.setenv("SKELETON_INSTANCE_KEY_FILE", str(tmp_path / "keys" / "master.key"))
    assert mode.select_settings_module(default, base_dir=tmp_path) == mode.SETUP_SETTINGS
    profile = setup_settings({"env": environ.Env(), "BASE_DIR": tmp_path})
    assert profile["SKELETON_SETUP_TARGET"] == default
    assert profile["SKELETON_SETUP_PRODUCTION"] is production
