"""Every screen of the core uses the whole width of the grid (#803).

The shell (``base.html``, ``account/base_manage.html``) already limits the content to its grid. A
page wrapper or a section card with its own ``max-w-*`` leaves an empty strip on the right of wide
screens, so only the files below may use one, each with the reason: the shells themselves, the
simple centered forms and messages, and the measure of a text inside a block.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "skeleton", ROOT / "examples"]
ALLOWED = {
    # The grid itself.
    "skeleton/web/templates/skeleton/web/base.html": "shell: grid max-w-7xl",
    "skeleton/web/templates/skeleton/web/error_base.html": "shell das páginas de erro, mensagem centralizada",
    "skeleton/web/templates/skeleton/portal/base.html": "shell do portal",
    "skeleton/web/templates/skeleton/setup/base.html": "shell do setup; sem etapas, formulário centralizado",
    "skeleton/web/templates/account/base_manage.html": "shell da conta; cadastro do MFA obrigatório centralizado",
    # Simple centered forms and messages.
    "skeleton/web/templates/account/base_entrance.html": "entrada (login, cadastro, senha): formulário centralizado",
    "skeleton/web/templates/skeleton/maintenance.html": "manutenção: mensagem centralizada",
    "skeleton/web/templates/skeleton/setup/restarting.html": "reinício do setup: mensagem centralizada",
    "skeleton/web/templates/skeleton/web/error.html": "erro: mensagem centralizada",
    "skeleton/tenancy/templates/skeleton/tenancy/organization_access.html": "acesso negado: mensagem centralizada",
    # Measure of a text, not of the screen.
    "skeleton/web/templates/skeleton/web/pages/public.html": "página pública em blocos: medida de leitura",
    "skeleton/web/templates/skeleton/web/pages/block-hero.html": "subtítulo do hero: medida de leitura",
    "skeleton/integrations/templates/skeleton/integrations/webhooks/deliveries.html": "trecho da resposta numa célula",
}
WIDTH = re.compile(r"(?<![\w-])(?:[a-z]+:)*max-w-(?:xs|sm|md|lg|xl|\dxl|prose|screen-\w+|\[[^\]]+\])(?![\w-])")


def _templates():
    for source in SOURCES:
        for path in sorted(source.rglob("*.html")):
            if "static" in path.parts:
                continue
            yield path.relative_to(ROOT).as_posix(), path.read_text(encoding="utf-8")


def test_no_screen_narrows_the_grid():
    problems = []
    for relative, text in _templates():
        if relative in ALLOWED:
            continue
        for number, line in enumerate(text.splitlines(), 1):
            for match in WIDTH.finditer(line):
                problems.append(f"{relative}:{number}: {match.group(0)}")
    assert problems == []


def test_the_account_area_has_no_narrower_column():
    text = (ROOT / "skeleton/web/templates/account/base_manage.html").read_text(encoding="utf-8")
    assert "content_width" not in text


def test_the_allowlist_has_no_stale_entries():
    used = {relative for relative, text in _templates() if WIDTH.search(text)}
    assert set(ALLOWED) <= used
