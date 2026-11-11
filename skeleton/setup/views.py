"""Setup steps (#146): plain forms with CSRF, no JavaScript, one step at a time.

Every step after the token requires the signed setup session. Values go to the instance
SQLite through the catalog (secrets encrypted); the owner credential of the schema step only
exists while that request runs. ``/setup/`` answers 404 once the setup is complete.
"""

from __future__ import annotations

import secrets
from functools import wraps

from cryptography.fernet import Fernet
from django.conf import settings
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect, render
from django.views.decorators.http import require_http_methods

from skeleton.instance import crypto
from skeleton.instance.writer import open_store, write_setting

from . import database, forms, restart, tokens
from .mode import COMPLETED

STEPS = (
    ("instance", "Instância"),
    ("database", "Banco de dados (role runtime)"),
    ("schema", "Schema e catálogo"),
    ("email", "Email"),
    ("storage", "Cache e arquivos"),
    ("admin", "Administrador e organização"),
)
SESSION_KEY = "skeleton_setup"
MAX_FAILURES = 5
FAILURE_WINDOW = 15 * 60
KEYRINGS = ("mfa_encryption_keys", "identity_provider_encryption_keys", "integration_encryption_keys")


def _store():
    return open_store(settings.BASE_DIR)


def _state():
    store, _ = _store()
    return store.setup_state()


def _stored(key: str) -> str | None:
    store, key_path = _store()
    row = store.settings().get(key)
    if row is None:
        return None
    value, secret = row
    return crypto.decrypt(crypto.load_key(key_path), value) if secret else value


def _mark(step: str) -> None:
    store, _ = _store()
    store.put_setup_state(f"step:{step}", "done")


def _progress():
    state = _state()
    return [(name, label, state.get(f"step:{name}") == "done") for name, label in STEPS]


def _not_completed():
    if _state().get("status") == COMPLETED:
        raise Http404


def setup_page(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        _not_completed()
        if not request.session.get(SESSION_KEY):
            return redirect("setup:start")
        return view(request, *args, **kwargs)

    return wrapper


def _render(request, template, context, status=200):
    return render(request, f"skeleton/setup/{template}", {"steps": _progress(), **context}, status=status)


def _client(request) -> str:
    return request.META.get("REMOTE_ADDR", "unknown")


@require_http_methods(["GET", "POST"])
def start(request):
    """Token form; with a valid setup session, the overview of the steps."""
    _not_completed()
    if request.session.get(SESSION_KEY):
        return _render(request, "overview.html", {"title": "Configuração da instância"})
    form = forms.TokenForm(request.POST or None)
    status = 200
    if request.method == "POST":
        key = f"setup-token-failures:{_client(request)}"
        failures = cache.get(key, 0)
        if failures >= MAX_FAILURES:
            return _render(
                request,
                "token.html",
                {"form": form, "error": "Muitas tentativas. Aguarde 15 minutos.", "title": "Primeiro acesso"},
                status=429,
            )
        if form.is_valid() and tokens.consume_token(_store()[0], form.cleaned_data["token"]):
            request.session.cycle_key()
            request.session[SESSION_KEY] = True
            return redirect("setup:start")
        cache.set(key, failures + 1, FAILURE_WINDOW)
        form = forms.TokenForm()
        status = 403
        return _render(
            request,
            "token.html",
            {"form": form, "error": "Token inválido, já usado ou expirado.", "title": "Primeiro acesso"},
            status=status,
        )
    return _render(request, "token.html", {"form": form, "title": "Primeiro acesso"})


def _step(request, name, form, *, status=200, **context):
    label = dict(STEPS)[name]
    return _render(request, "step.html", {"form": form, "step": name, "title": label, **context}, status)


@setup_page
@require_http_methods(["GET", "POST"])
def instance(request):
    form = forms.InstanceForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        try:
            write_setting(settings.BASE_DIR, "allowed_hosts", data["allowed_hosts"], source="setup")
            if data["csrf_trusted_origins"]:
                write_setting(settings.BASE_DIR, "csrf_trusted_origins", data["csrf_trusted_origins"], source="setup")
            if data.get("public_url"):
                write_setting(settings.BASE_DIR, "public_url", data["public_url"], source="setup")
            write_setting(settings.BASE_DIR, "trust_proxy", "true" if data["trust_proxy"] else "false", source="setup")
        except ValueError as error:
            form.add_error(None, str(error))
        else:
            # Secrets generated here are written once and never shown.
            if _stored("secret_key") is None:
                write_setting(settings.BASE_DIR, "secret_key", secrets.token_urlsafe(50), source="setup:generated")
            for keyring in KEYRINGS:
                if _stored(keyring) is None:
                    write_setting(settings.BASE_DIR, keyring, Fernet.generate_key().decode(), source="setup:generated")
            _mark("instance")
            return redirect("setup:database")
    return _step(request, "instance", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def database_step(request):
    form = forms.DatabaseForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        try:
            role = database.validate_runtime(form.cleaned_data["database_url"])
            write_setting(settings.BASE_DIR, "database_url", form.cleaned_data["database_url"], source="setup")
        except (database.DatabaseCheckError, ValueError) as error:
            form.add_error("database_url", str(error))
        else:
            store, _ = _store()
            store.put_setup_state("database_role", role.name)
            _mark("database")
            return redirect("setup:schema")
    return _step(request, "database", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def schema(request):
    runtime_dsn = _stored("database_url")
    if runtime_dsn is None:
        return redirect("setup:database")
    form = forms.SchemaForm(request.POST or None)
    problems = []
    if request.method == "POST" and form.is_valid():
        owner_dsn = form.cleaned_data.pop("owner_url")
        try:
            database.validate_owner(owner_dsn, database.validate_runtime(runtime_dsn))
            problems = database.migrate_schema(owner_dsn)
        except database.DatabaseCheckError as error:
            form.add_error("owner_url", str(error))
        except Exception as error:  # noqa: BLE001 - shown without detail; the DSN never reaches a message
            form.add_error(None, f"A migração falhou ({type(error).__name__}). Corrija e tente de novo.")
        finally:
            del owner_dsn  # Kept only in this request's memory; never stored, logged or in the session.
        if not form.errors and not problems:
            _mark("schema")
            return redirect("setup:email")
    status = 400 if request.method == "POST" else 200
    return _step(request, "schema", form, problems=problems, status=status)


def _email_connection(url: str):
    import environ  # type: ignore[import-untyped]
    from django.core.mail import get_connection

    config = environ.Env.email_url_config(url)
    return get_connection(
        backend=config["EMAIL_BACKEND"],
        host=config.get("EMAIL_HOST"),
        port=config.get("EMAIL_PORT"),
        username=config.get("EMAIL_HOST_USER"),
        password=config.get("EMAIL_HOST_PASSWORD"),
        use_tls=config.get("EMAIL_USE_TLS", False),
        use_ssl=config.get("EMAIL_USE_SSL", False),
        timeout=10,
        fail_silently=False,
    )


@setup_page
@require_http_methods(["GET", "POST"])
def email(request):
    form = forms.EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from django.core.mail import EmailMessage

        data = form.cleaned_data
        try:
            from skeleton.instance.schema import spec_for

            spec_for("email_url").clean(data["email_url"])
            message = EmailMessage(
                subject="Teste do Skeleton",
                body="O servidor de email da instância está configurado.",
                from_email=data["default_from_email"],
                to=[data["test_recipient"]],
                connection=_email_connection(data["email_url"]),
            )
            if message.send() != 1:
                raise RuntimeError("not accepted")
        except ValueError as error:
            form.add_error("email_url", str(error))
        except Exception as error:  # noqa: BLE001 - class only: the URL carries a password
            form.add_error(None, f"O envio de teste falhou ({type(error).__name__}).")
        else:
            write_setting(settings.BASE_DIR, "email_url", data["email_url"], source="setup")
            write_setting(settings.BASE_DIR, "default_from_email", data["default_from_email"], source="setup")
            _mark("email")
            return redirect("setup:storage")
    return _step(request, "email", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def storage(request):
    form = forms.StorageForm(request.POST or None, initial={"files_root": str(settings.BASE_DIR / "var" / "files")})
    if request.method == "POST" and form.is_valid():
        from pathlib import Path

        from skeleton.files.storage import storage_problems

        data = form.cleaned_data
        if data["files_backend"] == "local":
            config = {
                "BACKEND": "django.core.files.storage.FileSystemStorage",
                "OPTIONS": {"location": data["files_root"], "base_url": None},
            }
        else:
            config = {"BACKEND": "storages.backends.s3.S3Storage", "OPTIONS": {"default_acl": "private"}}
        problems = storage_problems(config, static_root=settings.STATIC_ROOT)
        if data["files_backend"] == "local" and not problems:
            root = Path(data["files_root"])
            try:
                root.mkdir(parents=True, exist_ok=True)
                probe = root / ".setup-write-test"
                probe.write_bytes(b"ok")
                probe.unlink()
            except OSError as error:
                problems.append(f"A pasta não é gravável ({type(error).__name__}).")
        for problem in problems:
            form.add_error(None, problem)
        if not problems:
            try:
                if data["cache_url"]:
                    write_setting(settings.BASE_DIR, "cache_url", data["cache_url"], source="setup")
                write_setting(settings.BASE_DIR, "files_backend", data["files_backend"], source="setup")
                names = (
                    ("files_root",)
                    if data["files_backend"] == "local"
                    else (
                        "files_s3_bucket",
                        "files_s3_region",
                        "files_s3_endpoint",
                    )
                )
                for name in names:
                    if data[name]:
                        write_setting(settings.BASE_DIR, name, data[name], source="setup")
            except ValueError as error:
                form.add_error(None, str(error))
            else:
                _mark("storage")
                return redirect("setup:admin")
    return _step(request, "storage", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def admin(request):
    runtime_dsn = _stored("database_url")
    if runtime_dsn is None or _state().get("step:schema") != "done":
        return redirect("setup:schema")
    form = forms.AdminForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from django.core.exceptions import ValidationError

        from skeleton.accounts.services import bootstrap_identity

        data = form.cleaned_data
        try:
            with database.django_alias("default", runtime_dsn) as alias:
                bootstrap_identity(
                    email=data["email"],
                    organization=data["organization_slug"],
                    name=data["organization_name"],
                    password=data["password1"],
                    using=alias,
                )
        except ValidationError as error:
            for message in error.messages:
                form.add_error(None, message)
        except Exception as error:  # noqa: BLE001
            form.add_error(None, f"Não foi possível criar o administrador ({type(error).__name__}).")
        else:
            _mark("admin")
            return redirect("setup:finish")
    return _step(request, "admin", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def finish(request):
    pending = [label for name, label, done in _progress() if not done]
    if request.method == "POST" and not pending:
        store, _ = _store()
        store.put_setup_state("status", COMPLETED)
        request.session.flush()
        how = restart.request_restart()
        return _render(request, "done.html", {"title": "Configuração concluída", "restart": how, "steps": ()})
    return _render(request, "finish.html", {"title": "Concluir", "pending": pending})
