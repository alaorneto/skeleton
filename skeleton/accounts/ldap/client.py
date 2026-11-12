"""The only place the core talks LDAP (#32, #33): ``ldap3`` behind a narrow, TLS-only interface.

Every connection is ``ldaps://`` or StartTLS with certificate verification (``CERT_REQUIRED``,
optional CA bundle) and explicit timeouts; plain ``ldap://`` without StartTLS is refused when
the provider is configured. Lookups run as the configured service account. The person's
password is used once, for a bind with the DN the search found, and never stored or logged.
Filter values are escaped; empty usernames or passwords never reach a bind (an empty
password would be an anonymous bind on many servers).
"""

from __future__ import annotations

import re
import ssl
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlsplit

DEFAULT_TIMEOUT_S = 5
ID_ATTRIBUTES = ("entryUUID", "objectGUID")
PAGE_SIZE = 500


class DirectoryError(Exception):
    """The directory could not be reached or refused the service account (``code`` names it)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class InvalidCredentials(Exception):
    """Neutral: unknown user, wrong password, several matches or outside the required group."""


@dataclass(frozen=True)
class DirectoryUser:
    uid: str
    dn: str
    email: str
    first_name: str
    last_name: str


@dataclass(frozen=True)
class DirectoryOptions:
    url: str  # scheme://host[:port], without the base DN
    base_dn: str
    bind_dn: str
    user_filter: str  # Contains "{username}"; the value is escaped before substitution.
    id_attribute: str = "entryUUID"
    email_attribute: str = "mail"
    first_name_attribute: str = "givenName"
    last_name_attribute: str = "sn"
    required_group_dn: str = ""
    start_tls: bool = False
    ca_bundle: str = ""
    timeout_s: int = DEFAULT_TIMEOUT_S


def split_directory(directory: str) -> tuple[str, str]:
    """``ldaps://host:636/dc=acme,dc=test`` (RFC 4516) → (``ldaps://host:636``, ``dc=acme,dc=test``)."""
    parts = urlsplit(directory)
    if parts.scheme not in ("ldap", "ldaps") or not parts.hostname or parts.username or parts.password:
        raise ValueError("Use ldaps://servidor/base ou ldap://servidor/base com StartTLS.")
    if parts.query or parts.fragment:
        raise ValueError("A URL do diretório não aceita parâmetros.")
    base_dn = unquote(parts.path.lstrip("/"))
    if not base_dn or "=" not in base_dn:
        raise ValueError("Informe a base de busca na URL, como ldaps://servidor/dc=empresa,dc=com.")
    return f"{parts.scheme}://{parts.netloc}", base_dn


def options_from(directory: str, settings: dict[str, Any]) -> DirectoryOptions:
    url, base_dn = split_directory(directory)
    allowed = {
        "bind_dn",
        "user_filter",
        "id_attribute",
        "email_attribute",
        "first_name_attribute",
        "last_name_attribute",
        "required_group_dn",
        "start_tls",
        "ca_bundle",
        "timeout_s",
    }
    unknown = set(settings) - allowed
    if unknown:
        raise ValueError(f"Opções não permitidas: {', '.join(sorted(unknown))}.")
    options = DirectoryOptions(
        url=url,
        base_dn=base_dn,
        bind_dn=str(settings.get("bind_dn") or ""),
        user_filter=str(settings.get("user_filter") or "(uid={username})"),
        id_attribute=str(settings.get("id_attribute") or "entryUUID"),
        email_attribute=str(settings.get("email_attribute") or "mail"),
        first_name_attribute=str(settings.get("first_name_attribute") or "givenName"),
        last_name_attribute=str(settings.get("last_name_attribute") or "sn"),
        required_group_dn=str(settings.get("required_group_dn") or ""),
        start_tls=settings.get("start_tls", False) is True,
        ca_bundle=str(settings.get("ca_bundle") or ""),
        timeout_s=int(settings.get("timeout_s") or DEFAULT_TIMEOUT_S),
    )
    validate(options)
    return options


def validate(options: DirectoryOptions) -> None:
    if options.url.startswith("ldap://") and not options.start_tls:
        raise ValueError("ldap:// exige StartTLS; sem TLS verificado o diretório é recusado.")
    if not options.bind_dn:
        raise ValueError("Informe o DN da service account.")
    if options.user_filter.count("{username}") != 1 or not re.fullmatch(r"\(.*\)", options.user_filter):
        raise ValueError("O filtro precisa ter parênteses e exatamente um {username}.")
    if options.id_attribute not in ID_ATTRIBUTES:
        raise ValueError("Use entryUUID ou objectGUID como identificador.")
    if not 1 <= options.timeout_s <= 30:
        raise ValueError("Use um tempo limite de 1 a 30 segundos.")


def escape_filter_value(value: str) -> str:
    from ldap3.utils.conv import escape_filter_chars

    return str(escape_filter_chars(value))


def _tls(options: DirectoryOptions) -> Any:
    from ldap3 import Tls

    return Tls(
        validate=ssl.CERT_REQUIRED,
        version=ssl.PROTOCOL_TLS_CLIENT,
        ca_certs_file=options.ca_bundle or None,
    )


def default_connect(options: DirectoryOptions, user: str, password: str) -> Any:
    """An open, TLS-protected, not yet bound connection (``ldap3``)."""
    from ldap3 import Connection, Server
    from ldap3.core.exceptions import LDAPException

    server = Server(
        options.url,
        use_ssl=options.url.startswith("ldaps://"),
        tls=_tls(options),
        connect_timeout=options.timeout_s,
        get_info=None,
    )
    try:
        connection = Connection(
            server,
            user=user,
            password=password,
            receive_timeout=options.timeout_s,
            raise_exceptions=False,
            read_only=True,
        )
        connection.open()
        if options.url.startswith("ldap://") and not connection.start_tls():
            raise DirectoryError("start_tls_failed")
    except LDAPException as exc:
        raise DirectoryError("unreachable") from exc
    return connection


# Tests replace the connection factory with ldap3's MOCK_SYNC strategy.
Connect = Callable[[DirectoryOptions, str, str], Any]
_connect: Connect = default_connect


def set_connection_factory(factory: Connect | None) -> None:
    global _connect
    _connect = factory or default_connect


def _service(options: DirectoryOptions, bind_password: str) -> Any:
    connection = _connect(options, options.bind_dn, bind_password)
    if not connection.bind():
        raise DirectoryError("service_bind_failed")
    return connection


def _value(entry: Any, attribute: str) -> Any:
    values = entry.get("attributes", {}).get(attribute)
    if isinstance(values, list):
        return values[0] if values else None
    return values


def _uid(raw: Any, attribute: str) -> str:
    if attribute == "objectGUID" and isinstance(raw, bytes | bytearray):
        return str(uuid.UUID(bytes_le=bytes(raw)))
    if isinstance(raw, bytes | bytearray):
        raw = bytes(raw).decode("utf-8", "replace")
    value = str(raw or "").strip()
    try:
        return str(uuid.UUID(value.strip("{}")))
    except ValueError:
        raise DirectoryError("invalid_identifier") from None


def _user(entry: Any, options: DirectoryOptions) -> DirectoryUser:
    return DirectoryUser(
        uid=_uid(_value(entry, options.id_attribute), options.id_attribute),
        dn=str(entry.get("dn") or ""),
        email=str(_value(entry, options.email_attribute) or "").strip().lower(),
        first_name=str(_value(entry, options.first_name_attribute) or "")[:150],
        last_name=str(_value(entry, options.last_name_attribute) or "")[:150],
    )


def _attributes(options: DirectoryOptions) -> list[str]:
    attributes = [
        options.id_attribute,
        options.email_attribute,
        options.first_name_attribute,
        options.last_name_attribute,
    ]
    if options.required_group_dn:
        attributes.append("memberOf")
    return attributes


def _in_group(entry: Any, options: DirectoryOptions) -> bool:
    if not options.required_group_dn:
        return True
    groups = entry.get("attributes", {}).get("memberOf") or []
    wanted = options.required_group_dn.lower()
    return any(str(group).lower() == wanted for group in groups)


def authenticate(options: DirectoryOptions, bind_password: str, username: str, password: str) -> DirectoryUser:
    """Search the person with the service account, then bind as them; ``InvalidCredentials`` otherwise."""
    from ldap3 import SUBTREE

    if not isinstance(username, str) or not isinstance(password, str) or not username.strip() or not password:
        raise InvalidCredentials
    service = _service(options, bind_password)
    try:
        query = options.user_filter.replace("{username}", escape_filter_value(username.strip()))
        if not service.search(options.base_dn, query, SUBTREE, attributes=_attributes(options), size_limit=2):
            raise InvalidCredentials
        entries = [entry for entry in service.response or [] if entry.get("type") == "searchResEntry"]
    finally:
        service.unbind()
    if len(entries) != 1 or not _in_group(entries[0], options):
        raise InvalidCredentials
    person = _user(entries[0], options)
    connection = _connect(options, person.dn, password)
    try:
        if not connection.bind():
            raise InvalidCredentials
    finally:
        connection.unbind()
    return person


def check(options: DirectoryOptions, bind_password: str) -> str:
    """Bind the service account and read the base entry; ``ok`` or an error code."""
    from ldap3 import BASE

    try:
        service = _service(options, bind_password)
    except DirectoryError as exc:
        return exc.code
    try:
        return (
            "ok" if service.search(options.base_dn, "(objectClass=*)", BASE, attributes=["1.1"]) else "base_not_found"
        )
    finally:
        service.unbind()


def iterate_users(options: DirectoryOptions, bind_password: str) -> Iterator[DirectoryUser]:
    """Every person the filter matches (paged), for the directory sync (#33)."""
    from ldap3 import SUBTREE

    query = options.user_filter.replace("{username}", "*")
    service = _service(options, bind_password)
    try:
        cookie = None
        while True:
            ok = service.search(
                options.base_dn,
                query,
                SUBTREE,
                attributes=_attributes(options),
                paged_size=PAGE_SIZE,
                paged_cookie=cookie,
            )
            if not ok and service.result.get("result") not in (0, None):
                raise DirectoryError("search_failed")
            for entry in service.response or []:
                if entry.get("type") == "searchResEntry" and _in_group(entry, options):
                    yield _user(entry, options)
            controls = service.result.get("controls", {}) if isinstance(service.result, dict) else {}
            cookie = controls.get("1.2.840.113556.1.4.319", {}).get("value", {}).get("cookie")
            if not cookie:
                return
    finally:
        service.unbind()
