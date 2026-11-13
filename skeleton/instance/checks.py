"""skeleton.W036: the instance SQLite and its master key must be readable by their owner only (#342)."""

import os
import stat

from django.conf import settings
from django.core.checks import Tags, Warning, register

from .resolve import instance_paths


def permission_problems():
    if os.name != "posix":
        return []
    problems = []
    for label, path in zip(
        ("do SQLite da instância", "da chave mestra"), instance_paths(settings.BASE_DIR), strict=True
    ):
        if path.is_file() and stat.S_IMODE(path.stat().st_mode) & 0o077:
            problems.append(
                Warning(
                    f"O arquivo {label} ({path}) é acessível por outros usuários do sistema.",
                    hint=f"chmod 600 {path}",
                    id="skeleton.W036",
                )
            )
    return problems


@register(Tags.security, deploy=True)
def instance_file_checks(app_configs=None, **kwargs):
    return permission_problems()
