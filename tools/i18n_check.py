"""Check that every translatable string of the core is valid for gettext (#174).

The core ships pt-BR only and its msgids are pt-BR, so there is no versioned catalog. This
script extracts one into a temporary ``locale/`` with ``makemessages`` and validates it with
``msgfmt --check-format``: a broken placeholder (``%(name)`` without its conversion, mismatched
``{}``) fails here. Coverage, that no visible text escapes gettext, is the pseudo-translation
test ``tests/integration/test_i18n_coverage.py``.

Needs GNU gettext (``xgettext``, ``msgfmt``); the CI installs it. Exit code 0 on success.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IGNORE = ("tests/*", ".venv/*", "venv/*", "node_modules/*", "frontend/*", "var/*", "staticfiles/*", "build/*")


def main() -> int:
    if not (shutil.which("xgettext") and shutil.which("msgfmt")):
        print("GNU gettext (xgettext, msgfmt) is required.", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory() as temporary:
        work = Path(temporary) / "tree"
        shutil.copytree(
            ROOT,
            work,
            ignore=shutil.ignore_patterns(".git", ".venv", "venv", "node_modules", "var", "__pycache__", ".mypy_cache"),
        )
        (work / "locale").mkdir(exist_ok=True)
        command = [sys.executable, "-m", "django", "makemessages", "-l", "pt_BR", "-e", "html,txt,py", "--no-wrap"]
        for pattern in IGNORE:
            command += ["--ignore", pattern]
        extract = subprocess.run(command, cwd=work, capture_output=True, text=True)
        if extract.returncode:
            print(extract.stdout, extract.stderr, file=sys.stderr)
            return 1
        catalog = work / "locale" / "pt_BR" / "LC_MESSAGES" / "django.po"
        check = subprocess.run(
            ["msgfmt", "--check-format", "-o", str(Path(temporary) / "django.mo"), str(catalog)],
            capture_output=True,
            text=True,
        )
        if check.returncode:
            print(check.stderr, file=sys.stderr)
            return 1
        count = catalog.read_text(encoding="utf-8").count('\nmsgid "') - 1
        print(f"{count} msgids extracted and valid.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
