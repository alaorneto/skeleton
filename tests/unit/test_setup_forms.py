"""The setup only completes with values the target profile accepts (#457, #463)."""

import pytest
from django.test import override_settings

from skeleton.setup import forms

STORAGE = {"files_backend": "local", "files_root": "/instance/files"}
EMAIL = {"default_from_email": "noreply@example.test", "test_recipient": "ops@example.test"}


@override_settings(SKELETON_SETUP_PRODUCTION=True)
def test_production_target_requires_a_shared_redis_cache(subtests):
    for value in ("", "locmemcache://", "dummycache://", "not a url"):
        with subtests.test(cache_location=value):
            form = forms.StorageForm({**STORAGE, "cache_location": value})
            assert not form.is_valid() and "cache_location" in form.errors
    for value in ("redis://redis:6379/0", "rediss://redis:6380/0"):
        with subtests.test(cache_location=value):
            form = forms.StorageForm({**STORAGE, "cache_location": value, "cache_password": "secret"})
            assert form.is_valid() and form.cleaned_data["cache_url"].startswith(value.split("//")[0])


@override_settings(SKELETON_SETUP_PRODUCTION=False)
def test_development_target_keeps_the_local_cache():
    form = forms.StorageForm({**STORAGE, "cache_location": ""})
    assert form.is_valid() and form.cleaned_data["cache_url"] == ""
    assert not form.fields["cache_location"].required


def test_strict_is_an_argument_of_the_storage_form():
    assert not forms.StorageForm({**STORAGE}, strict=True).is_valid()
    assert forms.StorageForm({**STORAGE}, strict=False).is_valid()
    assert forms.StorageForm(strict=True).fields["cache_location"].required


def test_redis_password_is_percent_encoded_and_reads_back(subtests):
    from urllib.parse import unquote, urlsplit

    import environ

    for password in ("p@ss:w/rd", "a b#c?d%e", "simple"):
        with subtests.test(password=password):
            form = forms.StorageForm(
                {**STORAGE, "cache_location": "redis://redis:6379/0", "cache_password": password}, strict=True
            )
            assert form.is_valid(), form.errors
            location = environ.Env.cache_url_config(form.cleaned_data["cache_url"])["LOCATION"]
            parts = urlsplit(location)
            assert unquote(parts.password) == password and parts.hostname == "redis" and parts.port == 6379


def test_redis_address_with_credentials_is_refused():
    form = forms.StorageForm({**STORAGE, "cache_location": "redis://:secret-value@redis:6379/0"}, strict=True)
    assert not form.is_valid()
    assert form.errors["cache_location"] == ["Informe a senha no campo ao lado, não dentro do endereço."]
    assert "secret-value" not in str(form.errors)


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
    form = forms.StorageForm({**STORAGE, "cache_location": "locmemcache://secret-value"})
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


def test_smtp_url_from_separate_fields(subtests):
    from skeleton.conf.mailers import mailer_from_url

    url = forms.smtp_url(host="smtp.exemplo.com", port=587, security="starttls", user="a@b.com", password="p@ss:w/rd")
    assert url == "smtp+tls://a%40b.com:p%40ss%3Aw%2Frd@smtp.exemplo.com:587"
    options = mailer_from_url(url)["OPTIONS"]
    assert (options["username"], options["password"], options["use_tls"]) == ("a@b.com", "p@ss:w/rd", True)
    for security, scheme in (("ssl", "smtp+ssl"), ("none", "smtp")):
        with subtests.test(security=security):
            assert forms.smtp_url(host="h", port=465, security=security) == f"{scheme}://h:465"
    assert forms.smtp_url(host="h", port=25, security="none", user="u") == "smtp://u@h:25"


SMTP = {"smtp_host": "smtp.exemplo.com", "smtp_port": "587", "smtp_security": "starttls", **EMAIL}


def test_email_form_modes(subtests):
    with subtests.test("smtp"):
        form = forms.EmailForm({**SMTP, "smtp_user": "u", "smtp_password": "x y"}, strict=True)
        assert form.is_valid(), form.errors
        assert form.cleaned_data["url"] == "smtp+tls://u:x%20y@smtp.exemplo.com:587"
    with subtests.test("smtp without the server"):
        form = forms.EmailForm({"email_mode": "smtp"}, strict=False)
        assert not form.is_valid() and set(form.errors) == {"smtp_host", "default_from_email", "test_recipient"}
    with subtests.test("later, outside production"):
        form = forms.EmailForm({"email_mode": "later"}, strict=False)
        assert form.is_valid() and form.cleaned_data["url"] == ""
    with subtests.test("later is not offered in production"):
        form = forms.EmailForm({"email_mode": "later"}, strict=True)
        assert [value for value, _ in form.fields["email_mode"].choices] == ["smtp"]
        assert form.fields["email_mode"].widget.is_hidden
        assert not form.is_valid() and form.cleaned_data["email_mode"] == "smtp"
    with subtests.test("advanced URL outside SMTP in production"):
        form = forms.EmailForm({**EMAIL, "email_url": "consolemail://"}, strict=True)
        assert not form.is_valid() and form.errors["email_url"] == ["Em produção só um servidor SMTP é aceito."]


def test_email_failures_are_classified(subtests):
    import smtplib
    import socket
    import ssl

    from skeleton.setup.views import _email_failure

    cases = (
        (smtplib.SMTPAuthenticationError(535, b"user secret-user rejected"), "smtp_password", "usuário ou a senha"),
        (smtplib.SMTPSenderRefused(550, b"no", "a@b"), "default_from_email", "recusou o remetente"),
        (smtplib.SMTPRecipientsRefused({"x@y": (550, b"no")}), "test_recipient", "destinatário do teste"),
        (smtplib.SMTPNotSupportedError("STARTTLS"), "smtp_security", "tipo de segurança escolhido"),
        (ssl.SSLError("wrong version"), "smtp_security", "negociar a criptografia"),
        (socket.gaierror(-2, "unknown"), "smtp_host", "não foi encontrado"),
        (ConnectionRefusedError(), "smtp_port", "Conexão recusada"),
        (TimeoutError(), "smtp_port", "10 segundos"),
        (TimeoutError(), "smtp_port", "10 segundos"),
        (smtplib.SMTPServerDisconnected("closed"), "smtp_security", "fechou a conexão"),
        (smtplib.SMTPDataError(554, b"secret-user spam"), None, "SMTPDataError"),
        (RuntimeError("secret-user"), None, "RuntimeError"),
    )
    for error, field, text in cases:
        with subtests.test(error=type(error).__name__):
            got_field, message = _email_failure(error)
            assert got_field == field and text in message and "secret-user" not in message
