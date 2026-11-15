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
    skipped: bool = False


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
            "Informe como o sistema se conecta ao PostgreSQL no dia a dia. Use a role de aplicação"
            " (skeleton_runtime): ela não pode ser superusuário nem dona das tabelas. O banco precisa ter sido"
            " preparado antes com deploy/postgres/bootstrap.sql."
        ),
    ),
    Step(
        "schema",
        gettext_lazy("Tabelas do banco"),
        gettext_lazy("Cria as tabelas, com a credencial dona do schema."),
        gettext_lazy(
            "Agora o setup cria as tabelas. Para isso ele precisa, só nesta tela, da role dona do schema"
            " (skeleton_owner). A senha é usada nesta requisição e não é gravada, não vai para o log nem para a"
            " sessão. Pode levar alguns minutos: não feche nem recarregue a página. Se a página expirar, é seguro"
            " enviar de novo."
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
SKIPPED = "skipped"
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
    state, strict = _state(), _strict()
    return [
        Progress(
            name=step.name,
            label=step.label,
            summary=step.summary,
            # Email may wait outside production; production refuses to start without it (#465).
            required=strict if step.name == "email" else step.required,
            # A skipped step counts as concluded for "Concluir", and is shown as "depois".
            done=state.get(f"step:{step.name}") in {"done", SKIPPED},
            number=number,
            skipped=state.get(f"step:{step.name}") == SKIPPED,
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
            messages.warning(
                request,
                gettext(
                    "Sua sessão do setup expirou. Informe um token para continuar. O que já foi salvo continua"
                    " guardado."
                ),
            )
            return redirect("setup:start")
        return view(request, *args, **kwargs)

    return wrapper


# Steps that need others first; the remaining steps can be done in any order (#466).
PREREQUISITES = {"schema": ("database",), "admin": ("database", "schema")}


def _missing_prerequisite(step: str) -> str | None:
    state = _state()
    for name in PREREQUISITES.get(step, ()):
        if state.get(f"step:{name}") != "done":
            return name
    return None


def _go_to_prerequisite(request, step: str):
    """A redirect to the first step this one needs, with the reason; ``None`` when nothing is missing."""
    missing = _missing_prerequisite(step)
    if missing is None:
        return None
    messages.info(request, gettext("Conclua antes a etapa “%(label)s”.") % {"label": BY_NAME[missing].label})
    return redirect(f"setup:{missing}")


def _secret_of(key: str) -> str | None:
    """The password inside a stored URL, for a blank password field to keep it; never rendered."""
    from urllib.parse import unquote, urlsplit

    try:
        value = _stored(key)
        password = urlsplit(value).password if value else None
    except Exception:  # noqa: BLE001 - an unreadable value only means nothing to keep
        return None
    return unquote(password) if password is not None else None


def _initial(step: str) -> dict:
    """Stored values of a step without any secret, to correct one field without retyping (#466).

    Any failure to read or decrypt a stored value means no initial value: the page never fails
    because of it.
    """
    from urllib.parse import unquote, urlsplit

    try:
        if step == "instance":
            values = {name: _stored(name) for name in ("allowed_hosts", "csrf_trusted_origins", "public_url")}
            trust = _stored("trust_proxy")
            if trust is not None:
                values["trust_proxy"] = trust.lower() in {"1", "true", "yes", "on"}
            return {key: value for key, value in values.items() if value is not None}
        if step == "database":
            url = _stored("database_url")
            if not url:
                return {}
            config = database.parse(url)
            return {
                "db_host": config.get("HOST") or "",
                "db_port": config.get("PORT") or database.DEFAULT_PORT,
                "db_name": config.get("NAME") or "",
                "db_user": config.get("USER") or "",
            }
        if step == "email":
            values = {}
            url = _stored("email_url")
            if url:
                parts = urlsplit(url)
                security = {"smtp+tls": "starttls", "smtp+ssl": "ssl", "smtps": "ssl", "smtp": "none"}
                if parts.scheme in security:
                    values = {
                        "smtp_host": parts.hostname or "",
                        "smtp_port": parts.port or 587,
                        "smtp_user": unquote(parts.username or ""),
                        "smtp_security": security[parts.scheme],
                    }
            sender = _stored("default_from_email")
            if sender:
                values["default_from_email"] = sender
            return values
        if step == "storage":
            values = {}
            url = _stored("cache_url")
            if url:
                parts = urlsplit(url)
                host = parts.netloc.rsplit("@", 1)[-1]  # Without user:password@.
                values["cache_location"] = parts._replace(netloc=host).geturl()
            for name in ("files_backend", "files_root", "files_s3_bucket", "files_s3_region", "files_s3_endpoint"):
                value = _stored(name)
                if value is not None:
                    values[name] = value
            return values
    except Exception:  # noqa: BLE001 - see the docstring; the class alone goes to the log
        logger.warning("setup.initial_unreadable step=%s", step)
    return {}


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
                else gettext("Token inválido, já usado ou expirado. Veja abaixo como gerar um novo."),
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
    following = f"setup:{STEPS[index + 1].name}" if index + 1 < len(STEPS) else "setup:finish"
    page = {
        "form": form,
        "step": name,
        "title": BY_NAME[name].label,
        "intro": BY_NAME[name].intro,
        "number": index + 1,
        "previous": previous,
        "following": following,
        "already_done": _state().get(f"step:{name}") == "done",
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
    initial.update(_initial("instance"))  # A stored value wins over the suggestion.
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


def _check_error(form, error, *, prefix: str = "db_") -> None:
    """A refusal next to the field it is about; the whole form when the field is not on this form."""
    field = error.field
    if field and prefix != "db_" and field.startswith("db_"):
        field = prefix + field[3:]
    form.add_error(field if field in form.fields else None, str(error))


@setup_page
@require_http_methods(["GET", "POST"])
def database_step(request):
    form = forms.DatabaseForm(
        request.POST or None, initial=_initial("database"), kept_password=_secret_of("database_url")
    )
    if request.method == "POST" and form.is_valid():
        dsn = form.cleaned_data["dsn"]
        try:
            role = database.validate_runtime(dsn)
            write_setting(settings.BASE_DIR, "database_url", dsn, source="setup")
        except database.DatabaseCheckError as error:
            _check_error(form, error)
        except ValueError as error:
            form.add_error(None, str(error))
        else:
            store, _ = _store()
            store.put_setup_state("database_role", role.name)
            _mark("database")
            _saved(request, "database")
            return redirect("setup:schema")
    connected = None
    if _state().get("step:database") == "done":
        connected = {"role": _state().get("database_role", ""), "name": _initial("database").get("db_name", "")}
    return _step(request, "database", form, connected=connected, status=400 if request.method == "POST" else 200)


def _runtime_role(dsn: str):
    """The stored runtime role; its refusals belong to the previous step, not to the owner fields."""
    try:
        return database.validate_runtime(dsn)
    except database.DatabaseCheckError as error:
        raise database.DatabaseCheckError(str(error), code=error.code) from None


@setup_page
@require_http_methods(["GET", "POST"])
def schema(request):
    runtime_dsn = _stored("database_url")
    waiting = _go_to_prerequisite(request, "schema")
    if waiting is not None or runtime_dsn is None:
        return waiting or redirect("setup:database")
    form = forms.SchemaForm(request.POST or None)
    problems = []
    try:
        runtime = database.parse(runtime_dsn)
    except database.DatabaseCheckError:
        runtime = {}
    target = {"name": runtime.get("NAME", ""), "host": runtime.get("HOST", ""), "port": runtime.get("PORT") or 5432}
    if request.method == "POST" and form.is_valid():
        owner_password = form.cleaned_data.pop("owner_password", "")
        owner_dsn = form.cleaned_data.pop("owner_url") or database.build_dsn(
            host=target["host"],
            port=target["port"],
            name=target["name"],
            user=form.cleaned_data["owner_user"],
            password=owner_password,
        )
        try:
            database.validate_owner(owner_dsn, _runtime_role(runtime_dsn))
            problems = database.migrate_schema(owner_dsn)
        except database.DatabaseCheckError as error:
            _check_error(form, error, prefix="owner_")
        except Exception as error:  # noqa: BLE001 - shown without detail; the DSN never reaches a message
            # The log keeps the traceback for the operator; no form value is passed to it (#290).
            logger.error("setup.step_failed step=%s error_class=%s", "schema", type(error).__name__, exc_info=True)
            form.add_error(
                None,
                gettext("A migração falhou (%(__name__)s). Corrija e tente de novo.")
                % {"__name__": type(error).__name__},
            )
        finally:
            # Kept only in this request's memory; never stored, logged or in the session.
            del owner_dsn, owner_password
        if not form.errors and not problems:
            _mark("schema")
            _saved(request, "schema")
            return redirect("setup:email")
    status = 400 if request.method == "POST" else 200
    return _step(request, "schema", form, problems=problems, target=target, status=status)


def _email_failure(error: BaseException) -> tuple[str | None, str]:
    """The field and the message for a failed test email; the server reply is never shown (#465).

    The reply of an SMTP server can repeat the user, so only the class of the error decides.
    """
    import smtplib
    import socket
    import ssl

    table = (
        (
            smtplib.SMTPAuthenticationError,
            "smtp_password",
            gettext(
                "O servidor recusou o usuário ou a senha. Em contas com verificação em duas etapas, use uma senha"
                " de aplicativo."
            ),
        ),
        (
            smtplib.SMTPSenderRefused,
            "default_from_email",
            gettext("O servidor recusou o remetente. Use um endereço que essa conta tem permissão para enviar."),
        ),
        (smtplib.SMTPRecipientsRefused, "test_recipient", gettext("O servidor recusou o destinatário do teste.")),
        (
            smtplib.SMTPNotSupportedError,
            "smtp_security",
            gettext("O servidor não aceita o tipo de segurança escolhido. Tente a outra opção."),
        ),
        (
            ssl.SSLError,
            "smtp_security",
            gettext(
                "Falha ao negociar a criptografia. Confira se a porta combina com o tipo de segurança: 587 usa"
                " STARTTLS e 465 usa SSL/TLS."
            ),
        ),
        (socket.gaierror, "smtp_host", gettext("O servidor não foi encontrado. Confira o endereço.")),
        (
            ConnectionRefusedError,
            "smtp_port",
            gettext("Conexão recusada nessa porta. Confira a porta e se o servidor aceita conexões desta rede."),
        ),
        (
            TimeoutError,
            "smtp_port",
            gettext(
                "O servidor não respondeu em 10 segundos. Muitos provedores de hospedagem bloqueiam a porta 25: use"
                " 587 ou 465."
            ),
        ),
        (
            smtplib.SMTPServerDisconnected,
            "smtp_security",
            gettext("O servidor fechou a conexão. Em geral a porta e o tipo de segurança não combinam."),
        ),
    )
    for kind, field, message in table:
        if isinstance(error, kind):
            return field, message
    if isinstance(error, smtplib.SMTPException):
        return None, gettext("O servidor SMTP recusou o envio (%(error_class)s). Veja o log skeleton.setup.") % {
            "error_class": type(error).__name__
        }
    return None, gettext("O envio de teste falhou (%(__name__)s).") % {"__name__": type(error).__name__}


def _email_connection(url: str):
    """A connection to the URL being configured, outside MAILERS (it is not saved yet)."""
    from skeleton.conf.mailers import connection_for

    return connection_for(url, timeout=10)


@setup_page
@require_http_methods(["GET", "POST"])
def email(request):
    strict = _strict()
    form = forms.EmailForm(
        request.POST or None, initial=_initial("email"), strict=strict, kept_password=_secret_of("email_url")
    )
    if request.method == "POST" and form.is_valid():
        import smtplib

        from django.core.exceptions import ImproperlyConfigured
        from django.core.mail import EmailMessage

        data = form.cleaned_data
        if data["email_mode"] == "later":
            store, _ = _store()
            store.put_setup_state("step:email", SKIPPED)
            messages.warning(
                request,
                gettext(
                    "E-mail ficou para depois. Convites, recuperação de senha e notificações por e-mail não serão"
                    " enviados até você configurar."
                ),
            )
            return redirect("setup:storage")
        url = data["url"]
        try:
            from skeleton.instance.schema import spec_for

            spec_for("email_url").clean(url)
            message = EmailMessage(
                subject=gettext("Teste do Skeleton"),
                body=gettext("O servidor de email da instância está configurado."),
                from_email=data["default_from_email"],
                to=[data["test_recipient"]],
            )
            # The URL is not in MAILERS yet: send through its own backend instance (#127).
            if _email_connection(url).send_messages([message]) != 1:
                raise RuntimeError("not accepted")
        except (ValueError, ImproperlyConfigured) as error:
            # Scheme or parameter names only, never the password.
            form.add_error("email_url" if data.get("email_url") else None, str(error))
        except Exception as error:  # noqa: BLE001 - class only: the URL carries a password
            field, text = _email_failure(error)
            # A classified failure keeps no traceback: the SMTP reply in it can repeat the user (#465).
            classified = isinstance(error, (smtplib.SMTPException, OSError))
            logger.error(
                "setup.step_failed step=%s error_class=%s", "email", type(error).__name__, exc_info=not classified
            )
            if data.get("email_url") and field is not None:
                field = None  # The separate fields were ignored: the message belongs to the whole form.
            form.add_error(field, text)
        else:
            write_setting(settings.BASE_DIR, "email_url", url, source="setup")
            write_setting(settings.BASE_DIR, "default_from_email", data["default_from_email"], source="setup")
            _mark("email")
            messages.success(
                request,
                gettext("Mensagem de teste enviada para %(email)s. Confira a caixa de entrada (e o spam).")
                % {"email": data["test_recipient"]},
            )
            return redirect("setup:storage")
    intro = (
        gettext(
            "O sistema envia convites, recuperação de senha e notificações por e-mail. Em produção o servidor SMTP é"
            " obrigatório. Uma mensagem de teste é enviada agora; a etapa só é salva se o servidor aceitar."
        )
        if strict
        else gettext(
            "O sistema envia convites, recuperação de senha e notificações por e-mail. Neste modo você pode deixar"
            " para depois. Uma mensagem de teste é enviada agora; a etapa só é salva se o servidor aceitar."
        )
    )
    return _step(request, "email", form, intro=intro, status=400 if request.method == "POST" else 200)


@setup_page
@require_http_methods(["GET", "POST"])
def storage(request):
    initial = {"files_root": str(settings.BASE_DIR / "var" / "files"), "files_backend": "local"}
    initial.update(_initial("storage"))
    form = forms.StorageForm(
        request.POST or None, initial=initial, strict=_strict(), kept_password=_secret_of("cache_url")
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
    waiting = _go_to_prerequisite(request, "admin")
    if waiting is not None or runtime_dsn is None:
        return waiting or redirect("setup:database")
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
    email_skipped = _state().get("step:email") == SKIPPED
    context = {"title": gettext("Concluir"), "pending": pending, "finishing": True, "email_skipped": email_skipped}
    return _render(request, "finish.html", context)
