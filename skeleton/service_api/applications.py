"""ApplicationService: availability and activation of apps, and the caller app's own configuration (#105)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass
from typing import Any

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied

from .base import ServiceAPI, service_method
from .errors import Forbidden, InvalidInput, Unavailable
from .errors import NotFound as NotFoundError
from .identity import _user

_SKIPPED_PREFIXES = ("skeleton.service_api", "skeleton.testing", "django.", "functools", "contextlib")


@dataclass(frozen=True, slots=True)
class ConfigurationValue:
    name: str
    value: str | int | bool


@dataclass(frozen=True, slots=True)
class AppConfiguration:
    """The organization's configuration of one app, with every declared field resolved to a value."""

    app_label: str
    values: tuple[ConfigurationValue, ...]

    def get(self, name: str) -> str | int | bool:
        for item in self.values:
            if item.name == name:
                return item.value
        raise KeyError(name)


def _manifest_for(app_id: str) -> Any:
    from skeleton.system.app_registry import all_apps

    if not isinstance(app_id, str) or not app_id:
        raise InvalidInput({"app_id": ["Informe o app_id ou o app_label."]})
    return next((item for item in all_apps() if app_id in (item.app_id, item.app_label)), None)


def calling_app_label() -> str | None:
    """The registered app whose module called the service, from the call stack.

    The first frame outside the service API, the test kit and Django decides. Only
    apps registered with a manifest qualify; core modules and scripts return None.
    """
    from skeleton.system.app_registry import registered_app_of_module

    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if not module.startswith(_SKIPPED_PREFIXES):
                label: str | None = registered_app_of_module(module)
                return label
            frame = frame.f_back
    finally:
        del frame
    return None


def _enabled(app_label: str) -> bool:
    from skeleton.system.applications import require_application_enabled

    try:
        require_application_enabled(app_label)
    except PermissionDenied:
        return False
    return True


class ApplicationService(ServiceAPI):
    """Whether an app can be used in the organization; configuration only for the app itself."""

    @service_method(read_only=True, since="0.5.0")
    def is_enabled(self, *, actor: AbstractBaseUser, app_id: str) -> bool:
        """Whether an app is installed, available to the organization and enabled, with its dependencies.

        Autorização: vínculo ativo do ator; não revela configuração.
        Erros: InvalidInput para identificador vazio. App desconhecida responde ``False``.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership

        active_membership(_user(actor))
        manifest = _manifest_for(app_id)
        return manifest is not None and _enabled(manifest.app_label)

    @service_method(read_only=True, since="0.5.0")
    def require_enabled(self, *, actor: AbstractBaseUser, app_id: str) -> None:
        """Raise the standard ``Unavailable`` unless the app can be used in the organization.

        Autorização: vínculo ativo do ator.
        Erros: Unavailable se a app não estiver instalada, disponível ou ativa (inclusive dependências).
        Efeitos: nenhum.
        """
        if not self.is_enabled(actor=actor, app_id=app_id):
            raise Unavailable()

    @service_method(read_only=True, since="0.5.0")
    def my_configuration(self, *, actor: AbstractBaseUser, app_label: str) -> AppConfiguration:
        """Return the organization's configuration of the calling app.

        Autorização: só a própria app: o módulo que chama precisa pertencer a ``app_label``.
        Uma app nunca lê a configuração de outra.
        Erros: Forbidden se a app chamadora for outra (ou não for uma app registrada);
        Unavailable se a app estiver desativada ou indisponível.
        Efeitos: nenhum.
        """
        from skeleton.access.policies import active_membership
        from skeleton.system.applications import application_state

        active_membership(_user(actor))
        if calling_app_label() != app_label:
            raise Forbidden("Uma app só lê a própria configuração.")
        if not _enabled(app_label):
            raise Unavailable()
        state = application_state(app_label)
        values = tuple(ConfigurationValue(name, value) for name, value in sorted(state["configuration"].items()))
        return AppConfiguration(app_label=app_label, values=values)


@dataclass(frozen=True, slots=True)
class ApplicationState:
    """An installed app as the organization administers it."""

    app_label: str
    app_id: str
    label: str
    description: str
    version: str
    available: bool
    enabled: bool
    configuration: tuple[ConfigurationValue, ...]
    revision: int
    sysid: str | None


def _state(app_label: str) -> ApplicationState:
    from skeleton.system.applications import application_state

    state = application_state(app_label)
    return ApplicationState(
        app_label=state["app_label"],
        app_id=state["app_id"],
        label=state["label"],
        description=state["description"],
        version=state["version"],
        available=state["available"],
        enabled=state["enabled"],
        configuration=tuple(ConfigurationValue(name, value) for name, value in state["configuration"].items()),
        revision=state["revision"],
        sysid=state["sysid"],
    )


def _require_administrator(actor: Any) -> None:
    """The app manager (``apps.manage``) or the administrator; everyone else sees NotFound (#24)."""
    from skeleton.access.capabilities import APPS_MANAGE, capabilities_for

    if APPS_MANAGE not in capabilities_for(_user(actor)):
        raise NotFoundError()


def _visible(state: ApplicationState) -> bool:
    """Available apps, and enabled ones the platform withdrew (the organization may still close them)."""
    return state.available or state.enabled


class ApplicationAdministrationService(ServiceAPI):
    """Activation and configuration of apps by the app manager (surfaces of /o/<org>/apps/)."""

    @service_method(read_only=True, since="0.5.0")
    def applications(self, *, actor: AbstractBaseUser) -> tuple[ApplicationState, ...]:
        """The apps the organization can choose from, with availability, activation and configuration.

        Lista as apps disponíveis para a organização e as ativas que a plataforma retirou
        (``available=False``, para que possam ser desativadas); as demais não aparecem.
        Autorização: capacidade ``apps.manage`` (gestor de apps ou administrador).
        Erros: NotFound para quem não tem ``apps.manage``.
        Efeitos: nenhum.
        """
        from skeleton.system.app_registry import all_apps

        _require_administrator(actor)
        states = (_state(manifest.app_label) for manifest in all_apps())
        return tuple(state for state in states if _visible(state))

    @service_method(read_only=True, since="0.5.0")
    def application(self, *, actor: AbstractBaseUser, app_label: str) -> ApplicationState:
        """One app the organization can choose.

        Autorização: capacidade ``apps.manage``.
        Erros: NotFound para app não instalada ou indisponível (e não ativa) e para quem não
        tem ``apps.manage``.
        Efeitos: nenhum.
        """
        from skeleton.system.app_registry import get_app

        _require_administrator(actor)
        try:
            get_app(app_label)
        except KeyError:
            raise NotFoundError() from None
        state = _state(app_label)
        if not _visible(state):
            raise NotFoundError()
        return state

    @service_method(read_only=False, since="0.5.0")
    def configure(
        self,
        *,
        actor: AbstractBaseUser,
        app_label: str,
        enabled: bool,
        configuration: tuple[tuple[str, str | int | bool], ...],
        expected_revision: int,
    ) -> ApplicationState:
        """Enable, disable or configure an app in the organization with CAS (virtual revision 0).

        Autorização: capacidade ``apps.manage``; a app precisa estar disponível para ativar ou
        configurar (indisponível, só a desativação de uma app ativa é aceita).
        Erros: NotFound para app não instalada; InvalidInput para configuração, dependência ou
        app indisponível (``app_unavailable``); Conflict("stale_revision").
        Efeitos: grava ``AppInstallation`` (auditado) na transação do chamador.
        """
        from skeleton.system.app_registry import get_app
        from skeleton.system.applications import configure_application

        _require_administrator(actor)
        try:
            get_app(app_label)
        except KeyError:
            raise NotFoundError() from None
        current = _state(app_label)
        if not current.available and (enabled is not False or not current.enabled):
            raise InvalidInput({"enabled": ["Aplicativo indisponível para esta organização."]})
        values = dict(configuration) if isinstance(configuration, tuple) else configuration
        configure_application(app_label, user=actor, enabled=enabled, configuration=values, revision=expected_revision)
        return _state(app_label)
