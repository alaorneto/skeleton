"""W046 (#640): the derived project overrides a template of the core shell.

Without a theme path, projects used to apply their brand by copying ``skeleton/web/base.html``
(and its siblings) into their own ``templates/``, which comes first in ``TEMPLATES["DIRS"]``. The
copy silently loses what the core adds to the shell in every version (the theme link, the instance
logo and icon, the manifest, maintenance and update notices). The brand has its own path now:
the instance appearance and theme packages (``docs/appearance.md``, ``docs/themes.md``).

Only a warning (silence it with ``SILENCED_SYSTEM_CHECKS`` when there is a reason). The error
pages (400, 403, 403_csrf, 404, 500), which the core allows a project to override, are not watched.
The message names the template, never the path of the file on the server.
"""

from pathlib import Path

from django.core.checks import Warning, register
from django.template import TemplateDoesNotExist
from django.template.loader import get_template

SHELL_TEMPLATES = (
    "skeleton/web/base.html",
    "skeleton/web/error_base.html",
    "skeleton/web/platform/base.html",
    "skeleton/portal/base.html",
    "skeleton/email/base.html",
    "account/base_entrance.html",
    "account/base_manage.html",
)


def _core_root() -> Path:
    import skeleton

    return Path(skeleton.__file__).resolve().parent


def overridden_shell_templates() -> list[str]:
    """The shell templates whose winning file lives outside the core package."""
    root = _core_root()
    overridden = []
    for name in SHELL_TEMPLATES:
        try:
            origin = get_template(name).origin.name
        except TemplateDoesNotExist:
            continue
        if not origin or not Path(origin).resolve().is_relative_to(root):
            overridden.append(name)
    return overridden


@register()
def shell_override_checks(app_configs=None, **kwargs):
    return [
        Warning(
            f"O projeto sobrescreve o template do núcleo {name}.",
            hint=(
                "Aplique a marca pela aparência da instância (docs/appearance.md): nome, logo, ícone e tema em "
                "/platform/appearance/ ou por um pacote de tema (docs/themes.md). Sobrescrever o shell perde o "
                "que o núcleo acrescenta a cada versão."
            ),
            obj=name,
            id="skeleton.W046",
        )
        for name in overridden_shell_templates()
    ]
