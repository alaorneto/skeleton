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


def test_instance_form_takes_only_values_the_instance_starts_with(subtests):
    base = {"default_timezone": "America/Sao_Paulo"}
    accepted = (
        {"allowed_hosts": "App.Example.test, .example.test,localhost,127.0.0.1,[::1]"},
        {"allowed_hosts": "app.example.test", "public_url": "https://app.example.test/"},
        {"allowed_hosts": ".example.test", "public_url": "https://app.example.test"},
        {
            "allowed_hosts": "app.example.test",
            "csrf_trusted_origins": "https://app.example.test,https://*.example.test",
        },
        {"allowed_hosts": "*"},
    )
    for data in accepted:
        with subtests.test(accepted=data):
            form = forms.InstanceForm({**base, **data}, strict=False)
            assert form.is_valid(), form.errors
    form = forms.InstanceForm({**base, **accepted[0]}, strict=False)
    assert form.is_valid()
    assert form.cleaned_data["allowed_hosts"] == "app.example.test,.example.test,localhost,127.0.0.1,[::1]"
    refused = (
        ({"allowed_hosts": "https://app.example.test"}, "allowed_hosts", "sem http://"),
        ({"allowed_hosts": "app.example.test/painel"}, "allowed_hosts", "sem http://"),
        ({"allowed_hosts": "app.example.test:8000"}, "allowed_hosts", "sem porta"),
        ({"allowed_hosts": "app.example.test,,localhost"}, "allowed_hosts", "sem itens vazios"),
        ({"allowed_hosts": "app example"}, "allowed_hosts", "sem porta"),
        ({"allowed_hosts": "a.test", "csrf_trusted_origins": "a.test"}, "csrf_trusted_origins", "https://"),
        ({"allowed_hosts": "a.test", "csrf_trusted_origins": "https://a.test/x"}, "csrf_trusted_origins", "caminho"),
        ({"allowed_hosts": "a.test", "public_url": "a.test"}, "public_url", "https://"),
        ({"allowed_hosts": "a.test", "public_url": "https://b.test"}, "public_url", "endereços aceitos"),
    )
    for data, field, text in refused:
        with subtests.test(refused=data):
            form = forms.InstanceForm({**base, **data}, strict=False)
            assert not form.is_valid() and list(form.errors) == [field] and text in form.errors[field][0]
    with subtests.test("* in production"):
        form = forms.InstanceForm({**base, "allowed_hosts": "app.example.test,*"}, strict=True)
        assert not form.is_valid() and "liste os endereços" in form.errors["allowed_hosts"][0]


def test_a_step_writes_nothing_when_one_value_is_refused(monkeypatch):
    from skeleton.setup import views

    written = []
    monkeypatch.setattr(views, "write_setting", lambda base_dir, key, value, source: written.append(key))
    form = forms.InstanceForm({"allowed_hosts": "a.test"}, strict=False)
    assert form.is_valid()
    values = [("allowed_hosts", "a.test", "allowed_hosts"), ("trust_proxy", "talvez", "trust_proxy")]
    assert views._cleaned(form, values) is None
    assert form.errors["trust_proxy"] == ["Use true ou false."] and written == []
    cleaned = views._cleaned(form, values[:1])
    assert views._write_all(form, cleaned) and written == ["allowed_hosts"]


def _caused(outer: BaseException, inner: BaseException) -> BaseException:
    """``outer`` raised while handling ``inner``, the way redis-py wraps the socket error."""
    outer.__context__ = inner
    return outer


def test_redis_failures_are_classified_without_the_address(subtests):
    import socket
    import ssl

    import redis

    from skeleton.setup.views import _redis_failure

    secret = "redis://:secret-pass@secret-host:6379/0"
    cases = (
        (redis.AuthenticationError(f"WRONGPASS {secret}"), "cache_password", "recusou a senha"),
        (_caused(redis.ConnectionError(secret), socket.gaierror(-2, "x")), "cache_location", "não foi encontrado"),
        (_caused(redis.ConnectionError(secret), ConnectionRefusedError()), "cache_location", "Conexão recusada"),
        (redis.TimeoutError(secret), "cache_location", "5 segundos"),
        (_caused(redis.ConnectionError(secret), ssl.SSLError("bad")), "cache_location", "TLS"),
        (redis.ResponseError(f"ERR DB index {secret}"), "cache_location", "número do banco"),
        (redis.ConnectionError(secret), "cache_location", "ConnectionError"),
        (RuntimeError(secret), "cache_location", "RuntimeError"),
    )
    for error, field, text in cases:
        with subtests.test(error=type(error).__name__, text=text):
            got_field, message = _redis_failure(error)
            assert got_field == field and text in message
            assert "secret" not in message


def test_s3_failures_are_classified_without_the_reply(subtests):
    from skeleton.setup.views import _s3_failure

    def botocore(name, code=None):
        error = type(name, (Exception,), {})("secret-bucket secret-key")
        if code:
            error.response = {"Error": {"Code": code, "Message": "secret-bucket"}}
        return error

    cases = (
        (ImportError("storages"), "files_backend", "não está instalado"),
        (botocore("NoCredentialsError"), None, "AWS_ACCESS_KEY_ID"),
        (botocore("ClientError", "NoSuchBucket"), "files_s3_bucket", "não existe"),
        (botocore("ClientError", "AccessDenied"), "files_s3_bucket", "Acesso negado"),
        (botocore("ClientError", "InvalidAccessKeyId"), None, "InvalidAccessKeyId"),
        (botocore("ClientError", "PermanentRedirect"), "files_s3_region", "outra região"),
        (botocore("EndpointConnectionError"), "files_s3_endpoint", "não respondeu"),
        (botocore("NoRegionError"), "files_s3_region", "Região inválida"),
        (ValueError("Invalid endpoint: secret-bucket"), "files_s3_endpoint", "Endpoint inválido"),
        (botocore("ClientError", "SlowDown"), None, "SlowDown"),
        (RuntimeError("secret-bucket"), None, "RuntimeError"),
    )
    for error, field, text in cases:
        with subtests.test(error=type(error).__name__, text=text):
            got_field, message = _s3_failure(error)
            assert got_field == field and text in message and "secret" not in message


class _FakeRedis:
    """A RESP server on an ephemeral port, enough for AUTH and PING (no Redis in the unit suite)."""

    def __init__(self, password=None):
        import socket
        import threading

        self.password = password
        self.server = socket.create_server(("127.0.0.1", 0))
        self.port = self.server.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        import contextlib

        with contextlib.suppress(OSError):
            while True:
                connection, _ = self.server.accept()
                with connection, connection.makefile("rb") as reader:
                    authenticated = self.password is None
                    while command := self._read(reader):
                        name = command[0].upper()
                        sends_password = name == b"AUTH" or (name == b"HELLO" and b"AUTH" in command)
                        if sends_password:
                            authenticated = command[-1].decode() == self.password
                        if sends_password and not authenticated:
                            reply = b"-WRONGPASS invalid username-password pair\r\n"
                        elif name == b"HELLO":  # RESP3, the default of redis-py 8: HELLO 3 [AUTH user pass]
                            reply = b"%1\r\n$5\r\nproto\r\n:3\r\n" if authenticated else b"-NOAUTH HELLO first\r\n"
                        elif name == b"AUTH":
                            reply = b"+OK\r\n"
                        elif name == b"PING":
                            reply = b"+PONG\r\n" if authenticated else b"-NOAUTH Authentication required.\r\n"
                        else:
                            reply = b"+OK\r\n"
                        connection.sendall(reply)

    @staticmethod
    def _read(reader):
        header = reader.readline()
        if not header.startswith(b"*"):
            return None
        parts = []
        for _ in range(int(header[1:])):
            size = int(reader.readline()[1:])
            parts.append(reader.read(size + 2)[:-2])
        return parts

    def close(self):
        self.server.close()


def test_redis_ping_talks_to_the_server_with_the_password(subtests):
    import redis

    from skeleton.setup.views import _redis_failure, _redis_ping

    server = _FakeRedis(password="p@ss:w/rd")
    try:
        location = f"redis://127.0.0.1:{server.port}/0"
        with subtests.test("right password"):
            _redis_ping(forms.redis_url(location, "p@ss:w/rd"))
        for password in ("wrong", ""):
            with subtests.test(password=password):
                with pytest.raises(redis.AuthenticationError) as caught:
                    _redis_ping(forms.redis_url(location, password))
                assert _redis_failure(caught.value)[0] == "cache_password"
        with subtests.test("local cache outside production"):
            _redis_ping("locmemcache://")  # Nothing to reach, nothing raised.
    finally:
        server.close()


def test_storage_step_renders_both_file_sections_with_the_css_hooks(subtests):
    """Both sections stay in the HTML; site.css hides the one not chosen (#729)."""
    import re
    from pathlib import Path

    from django.template.loader import render_to_string

    import skeleton.web

    html = render_to_string("skeleton/setup/storage_fields.html", {"form": forms.StorageForm(initial=STORAGE)})
    group = html.split('<div class="form-stack setup-files">', 1)[1]
    for value in ("local", "s3"):
        with subtests.test(section=value):
            assert re.search(rf'<input type="radio" name="files_backend" value="{value}"', group)
            assert f'data-files-section="{value}"' in group
    assert group.index("data-files-section") > group.index('name="files_backend"')
    for name in ("files_root", "files_s3_bucket"):
        with subtests.test(required=name):
            label = html.split(f'<label for="id_{name}">', 1)[1].split("</label>", 1)[0]
            assert '<span class="required" aria-hidden="true"> *</span>' in label and "(opcional)" not in label
    assert "Obrigatório se escolheu" not in html
    css = (Path(skeleton.web.__file__).parent / "static/skeleton/web/site.css").read_text(encoding="utf-8")
    for chosen, hidden in (("s3", "local"), ("local", "s3")):
        with subtests.test(css=chosen):
            assert f".setup-files:has(input[type=radio][value={chosen}]:checked) [data-files-section={hidden}]" in css
