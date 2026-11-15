"""Setup steps (#146): plain forms with CSRF, no JavaScript, one step at a time.

Every step after the token requires the signed setup session. Values go to the instance
SQLite through the catalog (secrets encrypted); the owner credential of the schema step only
exists while that request runs. ``/setup/`` answers 404 once the setup is complete.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from functools import wraps

from cryptography.fernet import Fernet
from django.conf import settings
from django.contrib import messages
from django.core.cache import cache
from django.http import Http404
from django.shortcuts import redirect, render
from django.utils.translation import gettext, gettext_lazy
from django.views.decorators.http import require_http_methods

from skeleton.common.logging import security_event
from skeleton.instance import crypto
from skeleton.instance.writer import open_store, write_setting

from . import database, forms, restart, tokens
from .mode import COMPLETED


@dataclass(frozen=True)
class Step:
    """One setup step: ``summary`` is its line on the overview, ``intro`` the paragraph atop it (#463)."""

    name: str
    label: str
    summary: str
    intro: str
    required: bool = True


@dataclass(frozen=True)
class Progress:
    """A step as the templates show it: position, state and texts."""

    name: str
    label: str
    summary: str
    required: bool
    done: bool
    number: int


STEPS = (
    Step(
        "instance",
        gettext_lazy("Instância"),
        gettext_lazy("Endereço pelo qual o sistema é acessado."),
        gettext_lazy(
            "Diga por qual endereço as pessoas vão acessar o sistema. As chaves secretas internas são geradas"
            " sozinhas nesta etapa e nunca são exibidas."
        ),
    ),
    Step(
        "database",
        gettext_lazy("Banco de dados"),
        gettext_lazy("Conexão com o PostgreSQL usada no dia a dia."),
        gettext_lazy(
            "Informe a URL da role runtime, a role que o sistema usa no dia a dia. Ela não pode ser superusuário,"
            " ter BYPASSRLS nem ser dona do schema skeleton."
        ),
    ),
    Step(
        "schema",
        gettext_lazy("Tabelas do banco"),
        gettext_lazy("Cria as tabelas, com a credencial dona do schema."),
        gettext_lazy(
            "A credencial de migração (dona do schema skeleton) é usada só nesta requisição para rodar migrate,"
            " sync_table_catalog e o check do schema. Ela não é gravada, não vai para o log nem para a sessão."
        ),
    ),
    Step(
        "email",
        gettext_lazy("E-mail"),
        gettext_lazy("Servidor que envia convites e recuperação de senha."),
        gettext_lazy(
            "O sistema envia convites, recuperação de senha e notificações por e-mail. Uma mensagem de teste é"
            " enviada agora; a etapa só é salva se o servidor aceitar."
        ),
    ),
    Step(
        "storage",
        gettext_lazy("Cache e arquivos"),
        gettext_lazy("Redis e o lugar onde ficam os anexos."),
        gettext_lazy("Defina o cache compartilhado (Redis) e onde os arquivos anexados aos registros ficam guardados."),
    ),
    Step(
        "admin",
        gettext_lazy("Administrador e organização"),
        gettext_lazy("Primeira conta e primeira organização."),
        gettext_lazy(
            "Cria a primeira conta, com acesso total à administração da plataforma, e a primeira organização."
        ),
    ),
)
BY_NAME = {step.name: step for step in STEPS}
SESSION_KEY = "skeleton_setup"
logger = logging.getLogger("skeleton.setup")
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
    return [
        Progress(
            name=step.name,
            label=step.label,
            summary=step.summary,
            required=step.required,
            done=state.get(f"step:{step.name}") == "done",
            number=number,
        )
        for number, step in enumerate(STEPS, 1)
    ]


def _strict() -> bool:
    """Whether the process starts with the production rules after the setup (#457, #463)."""
    return forms.production_target()


def _saved(request, name: str) -> None:
    messages.success(request, gettext("Etapa “%(label)s” salva.") % {"label": BY_NAME[name].label})


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
    base = {"steps": _progress(), "strict": _strict(), "total": len(STEPS)}
    return render(request, f"skeleton/setup/{template}", {**base, **context}, status=status)


def _client(request) -> str:
    return request.META.get("REMOTE_ADDR", "unknown")


@require_http_methods(["GET", "POST"])
def start(request):
    """Token form; with a valid setup session, the overview of the steps."""
    _not_completed()
    if request.session.get(SESSION_KEY):
        steps = _progress()
        pending = next((step for step in steps if not step.done), None)
        context = {
            "title": gettext("Configuração da instância"),
            "steps": steps,
            "next_step": pending,
            "started": any(step.done for step in steps),
        }
        return _render(request, "overview.html", context)
    form = forms.TokenForm(request.POST or None)
    status = 200
    if request.method == "POST":
        key = f"setup-token-failures:{_client(request)}"
        # The token is checked before the limit (#290): behind a proxy every client shares one
        # address, and wrong guesses from anyone would otherwise lock out the operator who has
        # the right token. The token has 192 random bits; the limit is a signal, not the defence.
        if form.is_valid() and tokens.consume_token(_store()[0], form.cleaned_data["token"]):
            cache.delete(key)
            request.session.cycle_key()
            request.session[SESSION_KEY] = True
            return redirect("setup:start")
        failures = cache.get(key, 0)
        limited = failures >= MAX_FAILURES
        if not limited:
            cache.set(key, failures + 1, FAILURE_WINDOW)
        status = 429 if limited else 403
        security_event(
            "setup.token_rejected",
            level=logging.WARNING,
            status=status,
            outcome="denied",
            reason_code="rate_limited" if limited else "invalid_token",
            source="setup",
        )
        return _render(
            request,
            "token.html",
            {
                "form": forms.TokenForm(),
                "error": gettext("Muitas tentativas. Aguarde 15 minutos.")
                if limited
                else gettext("Token inválido, já usado ou expirado."),
                "title": gettext("Primeiro acesso"),
                "steps": (),
            },
            status=status,
        )
    # No step list before the token: every link would lead back here.
    return _render(request, "token.html", {"form": form, "title": gettext("Primeiro acesso"), "steps": ()})


def _step(request, name, form, *, status=200, **context):
    index = [step.name for step in STEPS].index(name)
    previous = f"setup:{STEPS[index - 1].name}" if index else "setup:start"
    page = {
        "form": form,
        "step": name,
        "title": BY_NAME[name].label,
        "intro": BY_NAME[name].intro,
        "number": index + 1,
        "previous": previous,
    }
    return _render(request, "step.html", {**page, **context}, status)


@setup_page
@require_http_methods(["GET", "POST"])
def instance(request):
    initial = {
        # The address the operator is using right now, without the port.
        "allowed_hosts": request.get_host().split(":")[0],
        "trust_proxy": "HTTP_X_FORWARDED_PROTO" in request.META,
    }
    form = forms.InstanceForm(request.POST or None, initial=initial)
    if request.method == "POST" and form.is_valid():
        data = form.cleaned_data
        # Without explicit origins the public URL is the origin of the forms: the most common CSRF
        # failure after the setup is a proxy origin nobody listed (#463).
        origins = data["csrf_trusted_origins"] or data.get("public_url") or ""
        try:
            write_setting(settings.BASE_DIR, "allowed_hosts", data["allowed_hosts"], source="setup")
            if origins:
                write_setting(settings.BASE_DIR, "csrf_trusted_origins", origins, source="setup")
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
            _saved(request, "instance")
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
            _saved(request, "database")
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
            # The log keeps the traceback for the operator; no form value is passed to it (#290).
            logger.error("setup.step_failed step=%s error_class=%s", "schema", type(error).__name__, exc_info=True)
            form.add_error(
                None,
                gettext("A migração falhou (%(__name__)s). Corrija e tente de novo.")
                % {"__name__": type(error).__name__},
            )
        finally:
            del owner_dsn  # Kept only in this request's memory; never stored, logged or in the session.
        if not form.errors and not problems:
            _mark("schema")
            _saved(request, "schema")
            return redirect("setup:email")
    status = 400 if request.method == "POST" else 200
    return _step(request, "schema", form, problems=problems, status=status)


def _email_connection(url: str):
    """A connection to the URL being configured, outside MAILERS (it is not saved yet)."""
    from skeleton.conf.mailers import connection_for

    return connection_for(url, timeout=10)


@setup_page
@require_http_methods(["GET", "POST"])
def email(request):
    form = forms.EmailForm(request.POST or None)
    if request.method == "POST" and form.is_valid():
        from django.core.exceptions import ImproperlyConfigured
        from django.core.mail import EmailMessage

        data = form.cleaned_data
        try:
            from skeleton.instance.schema import spec_for

            spec_for("email_url").clean(data["email_url"])
            message = EmailMessage(
                subject=gettext("Teste do Skeleton"),
                body=gettext("O servidor de email da instância está configurado."),
                from_email=data["default_from_email"],
                to=[data["test_recipient"]],
            )
            # The URL is not in MAILERS yet: send through its own backend instance (#127).
            if _email_connection(data["email_url"]).send_messages([message]) != 1:
                raise RuntimeError("not accepted")
        except (ValueError, ImproperlyConfigured) as error:
            form.add_error("email_url", str(error))  # Scheme or parameter names only, never the password.
        except Exception as error:  # noqa: BLE001 - class only: the URL carries a password
            logger.error("setup.step_failed step=%s error_class=%s", "email", type(error).__name__, exc_info=True)
            form.add_error(
                None, gettext("O envio de teste falhou (%(__name__)s).") % {"__name__": type(error).__name__}
            )
        else:
            write_setting(settings.BASE_DIR, "email_url", data["email_url"], source="setup")
            write_setting(settings.BASE_DIR, "default_from_email", data["default_from_email"], source="setup")
            _mark("email")
            _saved(request, "email")
            return redirect("setup:storage")
    return _step(request, "email", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def storage(request):
    form = forms.StorageForm(
        request.POST or None,
        initial={"files_root": str(settings.BASE_DIR / "var" / "files"), "files_backend": "local"},
        strict=_strict(),
    )
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
                problems.append(gettext("A pasta não é gravável (%(__name__)s).") % {"__name__": type(error).__name__})
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
                _saved(request, "storage")
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
        except Exception as error:  # noqa: BLE001 - class only on the page; the traceback goes to the log
            logger.error("setup.step_failed step=%s error_class=%s", "admin", type(error).__name__, exc_info=True)
            form.add_error(
                None,
                gettext("Não foi possível criar o administrador (%(__name__)s).") % {"__name__": type(error).__name__},
            )
        else:
            _mark("admin")
            _saved(request, "admin")
            return redirect("setup:finish")
    return _step(request, "admin", form, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def finish(request):
    pending = [step for step in _progress() if not step.done]
    if request.method == "POST" and not pending:
        store, _ = _store()
        store.put_setup_state("status", COMPLETED)
        request.session.flush()
        how = restart.request_restart()
        return _render(request, "done.html", {"title": gettext("Configuração concluída"), "restart": how, "steps": ()})
    return _render(request, "finish.html", {"title": gettext("Concluir"), "pending": pending, "finishing": True})
