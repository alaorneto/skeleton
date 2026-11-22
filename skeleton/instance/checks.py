"""Checks of the instance files and of cluster mode.

- skeleton.W036: the instance SQLite and its master key must be readable by their owner only (#342).
- skeleton.E048: in cluster mode (#673) the attachments live in storage every node shares.
- skeleton.W048: in cluster mode the settings stored in the instance SQLite are ignored.
"""

import os
import stat

from django.conf import settings
from django.core.checks import Error, Tags, Warning, register

from .resolve import CLUSTER, instance_paths
from .store import InstanceStore, InstanceStoreError


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


def cluster_problems():
    if getattr(settings, "SKELETON_INSTANCE_MODE", "standalone") != CLUSTER:
        return []
    problems = []
    backend = (getattr(settings, "STORAGES", {}) or {}).get("skeleton_files", {}).get("BACKEND", "")
    if backend.endswith("FileSystemStorage") and not getattr(settings, "SKELETON_FILES_SHARED", False):
        problems.append(
            Error(
                "Modo cluster com anexos numa pasta local: cada nó veria só os próprios arquivos.",
                hint="Use SKELETON_FILES_BACKEND=s3, ou declare SKELETON_FILES_SHARED=true com SKELETON_FILES_ROOT "
                "num volume compartilhado por todos os nós.",
                id="skeleton.E048",
            )
        )
    database, _ = instance_paths(settings.BASE_DIR)
    try:
        stored = InstanceStore(database).settings() if database.is_file() else {}
    except (InstanceStoreError, OSError):
        stored = {}
    if stored:
        problems.append(
            Warning(
                f"Modo cluster: as {len(stored)} chaves gravadas no SQLite da instância ({database}) são ignoradas.",
                hint="Leve-as ao ambiente com `manage.py instance export-env` numa cópia standalone e apague o arquivo.",
                id="skeleton.W048",
            )
        )
    return problems


@register(Tags.security, deploy=True)
def instance_file_checks(app_configs=None, **kwargs):
    return permission_problems() + cluster_problems()
