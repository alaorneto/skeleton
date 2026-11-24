"""One visual form, one meaning, on every screen (#767).

A state is a pill with a dot and only comes out of ``status_badge``; a fixed property is a pill
without the dot and only comes out of ``marker``; identifiers are ``code`` and values are text.
Writing a badge by hand is how the same thing ended up as a pill on one screen and as text on
another, so the templates of the core never write the ``badge`` class themselves.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "skeleton", ROOT / "examples"]
# The allauth element maps allauth's own tags (verified, unverified, primary) to the vocabulary.
BADGE_ALLOWED = {Path("skeleton/web/templates/allauth/elements/badge.html")}
BADGE = re.compile(r"""class=["'][^"']*\bbadge\b""")


def _templates():
    for source in SOURCES:
        yield from sorted(source.rglob("*.html"))


def test_templates_never_write_a_badge_by_hand():
    offenders = []
    for path in _templates():
        relative = path.relative_to(ROOT)
        if relative in BADGE_ALLOWED:
            continue
        text = path.read_text(encoding="utf-8")
        for match in BADGE.finditer(text):
            offenders.append(f"{relative}:{text.count(chr(10), 0, match.start()) + 1}")
    assert offenders == [], "use status_badge (state) or marker (fixed property): " + ", ".join(offenders)


def test_tones_keep_one_meaning():
    from skeleton.web.templatetags.skeleton_format import CODE_LABELS, MARKER_TONES, MARKERS, STATUS, TONES

    assert {tone for _, tone in STATUS.values()} <= TONES
    assert {tone for _, tone in MARKERS.values()} <= MARKER_TONES
    # A failure is always danger, whatever the domain.
    failures = {key: tone for key, (_, tone) in STATUS.items() if key.rsplit(":", 1)[-1] in {"failed", "dead"}}
    assert set(failures.values()) == {"danger"}, failures
    assert all(isinstance(labels, dict) and labels for labels in CODE_LABELS.values())


def test_the_scan_catches_a_hand_written_badge():
    assert BADGE.search('<span class="badge badge-success">Ativo</span>')
    assert BADGE.search("<li class='flex badge'>x</li>")
    assert not BADGE.search('<span class="counter">3</span>')
    assert not BADGE.search('{{ "x"|status_badge }}')


# Verbs of an action that removes access or data, or cannot be undone (#769). Its button is red:
# btn-danger-outline where it starts (row, toolbar, danger zone), btn-danger on the confirmation.
DESTRUCTIVE = (
    "Remover",
    "Revogar",
    "Excluir",
    "Desativar",
    "Sair da organização",
    "Cancelar a assinatura",
    "Cancelar processo",
    "Cancelar título",
    "Descartar",
    "Anular",
    "Eliminar",
    "Estornar",
    "Aprovar e eliminar",
    "Pedir a eliminação",
    "Rotacionar segredo",
    "Redefinir MFA",
    "Destravar",
)
# Labels that start like a destructive verb but switch a personal preference back and forth.
REVERSIBLE = {"Desativar neste navegador"}
BUTTON = re.compile(
    r"""class=["']btn (?P<variant>btn-(?:primary|secondary|ghost))\b[^"']*["'][^>]*>"""
    r"""(?:\s*(?:<[^>]*>|\{%(?!\s*(?:translate|if|else|elif|endif))[^%]*%\}))*"""
    r"""\s*\{%\s*translate\s+["'](?P<label>[^"']+)["']"""
)


def _neutral_destructive_buttons(text):
    for match in BUTTON.finditer(text):
        if match.group("label").startswith(DESTRUCTIVE) and match.group("label") not in REVERSIBLE:
            yield match


def test_destructive_actions_are_red():
    offenders = []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for match in _neutral_destructive_buttons(text):
            line = text.count("\n", 0, match.start()) + 1
            offenders.append(f"{path.relative_to(ROOT)}:{line}: {match.group('label')}")
    assert offenders == [], "destructive actions use btn-danger-outline (btn-danger on the confirmation)"


def test_the_scan_catches_a_grey_destructive_button():
    grey = '<button type="submit" class="btn btn-secondary btn-sm">{% translate "Remover logo" %}</button>'
    with_icon = (
        '<a class="btn btn-secondary" href="/x">{% include "skeleton/web/components/icon.html" with name="trash" %}'
        '{% translate "Excluir" %}</a>'
    )
    red = '<a class="btn btn-danger-outline btn-sm" href="/x">{% translate "Revogar" %}</a>'
    safe = '<button class="btn btn-secondary btn-sm">{% translate "Reenviar" %}</button>'
    assert [m.group("label") for m in _neutral_destructive_buttons(grey + with_icon + red + safe)] == [
        "Remover logo",
        "Excluir",
    ]


# The empty state of the organizations home carries links of its own (join directory, invitation).
EMPTY_STATE_ALLOWED = {Path("skeleton/web/templates/skeleton/web/home.html")}
FEEDBACK_COMPONENTS = {
    Path("skeleton/web/templates/skeleton/web/components/form-errors.html"),
    Path("skeleton/web/templates/skeleton/web/components/error-summary.html"),
    Path("skeleton/web/templates/skeleton/web/components/empty-row.html"),
    Path("skeleton/web/templates/skeleton/web/components/empty-state.html"),
}


def test_feedback_uses_one_vocabulary():
    """Alerts are message + tone, error summaries and empty lists come from their components (#776)."""
    offenders = []
    for path in _templates():
        relative = path.relative_to(ROOT)
        if relative in FEEDBACK_COMPONENTS:
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"""class=["']alert\b""", text):
            offenders.append(f"{relative}: alert (use message info|success|warning|error)")
        if re.search(r"""class=["']error-summary\b""", text):
            offenders.append(f"{relative}: error-summary (use components/error-summary.html)")
        if relative not in EMPTY_STATE_ALLOWED and re.search(r"""class=["'][^"']*\bempty-state\b""", text):
            offenders.append(f"{relative}: empty-state (use components/empty-row.html or empty-state.html)")
    assert offenders == []
