"""O Tailwind varre os templates de todos os pacotes do núcleo (#462).

O `site.css` é compilado só com as classes que o Tailwind encontra nas
fontes declaradas por `@source` em `frontend/src/site.css`. Um diretório de
templates fora dessa lista produz utilitários que não existem no CSS.
"""

from __future__ import annotations

import posixpath
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCE_CSS = ROOT / "frontend" / "src" / "site.css"
COMPILED_CSS = ROOT / "skeleton" / "web" / "static" / "skeleton" / "web" / "site.css"


def _source_patterns() -> list[str]:
    text = SOURCE_CSS.read_text(encoding="utf-8")
    patterns = []
    for raw in re.findall(r'^@source\s+"([^"]+)";', text, flags=re.MULTILINE):
        # As fontes são relativas a frontend/src/.
        patterns.append(posixpath.normpath(posixpath.join("frontend/src", raw)))
    return patterns


def _glob(pattern: str) -> re.Pattern[str]:
    """Tailwind's glob: ``**/`` is zero or more directories, ``*`` stays inside one."""
    regex = ""
    for part in re.split(r"(\*\*/|\*|\?)", pattern):
        regex += {"**/": "(?:.*/)?", "*": "[^/]*", "?": "[^/]"}.get(part, re.escape(part))
    return re.compile(regex + "$")


def _is_scanned(path: str, patterns: list[str]) -> bool:
    for pattern in patterns:
        if any(char in pattern for char in "*?["):
            if _glob(pattern).match(path):
                return True
        elif path == pattern or path.startswith(pattern + "/"):
            return True
    return False


def test_every_core_template_directory_is_scanned() -> None:
    patterns = _source_patterns()
    assert patterns, "frontend/src/site.css não declara @source"
    missing = []
    for directory in sorted((ROOT / "skeleton").rglob("templates")):
        if not directory.is_dir():
            continue
        for template in sorted(directory.rglob("*.html")):
            relative = template.relative_to(ROOT).as_posix()
            if not _is_scanned(relative, patterns):
                missing.append(relative)
    assert not missing, f"templates fora da varredura do Tailwind: {missing}"


def test_known_utilities_are_compiled() -> None:
    compiled = COMPILED_CSS.read_text(encoding="utf-8")
    # Classes usadas só em templates fora de skeleton/web (comentários e fluxos).
    assert ".whitespace-pre-line" in compiled
    assert r".lg\:grid-cols-2" in compiled
