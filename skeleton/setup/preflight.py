"""Check, before the setup completes, that the instance starts with what was stored (#467).

The target settings are loaded in a child process, the way the restarted service will load them,
and ``manage.py check`` runs there. The child gets ``SKELETON_SETUP=off`` so it never falls back
into setup mode. What reaches the page and the log is redacted: any ``scheme://...`` becomes
``[URL]``, because a settings error can quote a URL with its password.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
from pathlib import Path

from django.utils.translation import gettext

logger = logging.getLogger("skeleton.setup")
TIMEOUT = 60
LIMIT = 300
URL = re.compile(r"\S+://\S+")
CODE = (
    "import os, sys\n"
    "os.environ['DJANGO_SETTINGS_MODULE'] = sys.argv[1]\n"
    "import django\n"
    "django.setup()\n"
    "from django.core.management import call_command\n"
    "call_command('check')\n"
)


class PreflightUnavailable(Exception):
    """The child process could not be started; the setup may complete with a warning."""


def redact(text: str) -> str:
    return URL.sub("[URL]", text)


def _environment() -> dict[str, str]:
    environment = {
        name: value
        for name, value in os.environ.items()
        if name not in {"DJANGO_SETTINGS_MODULE", "SKELETON_SETUP_TARGET", "SKELETON_SETUP_TARGET_SETTINGS"}
    }
    environment["SKELETON_SETUP"] = "off"
    return environment


def _known(line: str) -> str | None:
    if "CACHE_URL" in line or "Redis compartilhado" in line:
        return gettext("Falta o Redis: preencha o endereço do Redis na etapa “Cache e arquivos”.")
    if "EMAIL_URL" in line or "DEFAULT_FROM_EMAIL" in line or "SMTP" in line:
        return gettext("Falta o servidor de e-mail: conclua a etapa “E-mail” com um servidor SMTP.")
    if "ALLOWED_HOSTS" in line or "hosts explícitos" in line:
        return gettext("Confira os endereços aceitos na etapa “Instância” (não use *).")
    return None


def start_problems(target: str, base_dir: Path | str) -> list[str]:
    """Load the target settings in a child process; return operator-facing problems (no values).

    Raises ``PreflightUnavailable`` when no child process can be started.
    """
    try:
        result = subprocess.run(  # noqa: S603 - fixed code, this interpreter, no shell
            [sys.executable, "-c", CODE, target],
            cwd=str(base_dir),
            env=_environment(),
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        logger.error("setup.preflight_failed reason=timeout seconds=%s", TIMEOUT)
        return [gettext("A verificação não terminou em 60 segundos. Tente de novo.")]
    except OSError as error:
        logger.error("setup.preflight_unavailable error_class=%s", type(error).__name__)
        raise PreflightUnavailable from None
    if result.returncode == 0:
        return []
    stderr = redact(result.stderr or "")
    logger.error("setup.preflight_failed returncode=%s stderr=%s", result.returncode, stderr)
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    last = lines[-1][:LIMIT] if lines else gettext("sem mensagem de erro")
    known = _known(last)
    prefix = gettext("A instância não subiria com esta configuração: ")
    return [prefix + (known or last)]
