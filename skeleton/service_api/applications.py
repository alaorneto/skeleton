"""ApplicationService: availability and activation of apps, and the caller app's own configuration (#105)."""

from __future__ import annotations

import inspect
from dataclasses import dataclass

from django.contrib.auth.base_user import AbstractBaseUser
from django.core.exceptions import PermissionDenied

from .base import ServiceAPI, service_method
from .errors import Forbidden, InvalidInput, Unavailable

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


def _manifest_for(app_id: str):
    from skeleton.system.app_registry import all_apps

    if not isinstance(app_id, str) or not app_id:
        raise InvalidInput({"app_id": ["Informe o app_id ou o app_label."]})
    return next((item for item in all_apps() if app_id in (item.app_id, item.app_label)), None)


def calling_app_label() -> str | None:
    """The registered app whose module called the service, from the call stack.

    The first frame outside the service API, the test kit and Django decides. Only
    apps registered with a manifest qualify; core modules and scripts return None.
    """
    from django.apps import apps

    from skeleton.system.app_registry import all_apps

    registered = {item.app_label for item in all_apps()}
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame else None
        while frame is not None:
            module = frame.f_globals.get("__name__", "")
            if not module.startswith(_SKIPPED_PREFIXES):
                config = apps.get_containing_app_config(module)
                return config.label if config is not None and config.label in registered else None
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

        active_membership(actor)
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

        active_membership(actor)
        if calling_app_label() != app_label:
            raise Forbidden("Uma app só lê a própria configuração.")
        if not _enabled(app_label):
            raise Unavailable()
        state = application_state(app_label)
        values = tuple(ConfigurationValue(name, value) for name, value in sorted(state["configuration"].items()))
        return AppConfiguration(app_label=app_label, values=values)
