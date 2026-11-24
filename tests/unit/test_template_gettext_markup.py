"""Only visible text goes through gettext: never a CSS class, an attribute or markup (#774).

A class name inside ``{% translate %}`` turns into the translated text in the ``class``
attribute, so any translation (or the pseudo-translation of the i18n coverage test) breaks the
style. This test reads every core template and refuses a msgid that is markup or a class name.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "skeleton", ROOT / "examples"]
SITE_CSS = ROOT / "frontend" / "src" / "site.css"
TRANSLATE = re.compile(r"{%\s*(?:translate|trans)\s+(['\"])(?P<msgid>.*?)\1")
MARKUP = re.compile(r"[<>=]")
UTILITY = re.compile(
    r"^(?:[a-z-]+:)*(?:-?(?:m|p|mt|mb|ms|me|mx|my|pt|pb|ps|pe|px|py|w|h|min-w|max-w|min-h|max-h|gap|text|bg|border"
    r"|rounded|grid-cols|col-span|space-[xy]|size)-[a-z0-9.\[\]/-]+|flex|grid|hidden|block|inline|sr-only|truncate)$"
)


def _css_classes() -> set[str]:
    css = SITE_CSS.read_text(encoding="utf-8")
    return set(re.findall(r"\.([a-z][a-z0-9-]*)", css)) | set(re.findall(r"@utility ([a-z][a-z0-9-]*)", css))


def _templates():
    for source in SOURCES:
        yield from sorted(source.rglob("*.html"))


def _offenders():
    classes = _css_classes() | {"btn", "w-full"}
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        for match in TRANSLATE.finditer(text):
            msgid = match.group("msgid")
            tokens = msgid.split()
            if MARKUP.search(msgid) or (tokens and all(t in classes or UTILITY.match(t) for t in tokens)):
                line = text.count("\n", 0, match.start()) + 1
                yield f"{path.relative_to(ROOT)}:{line}: {msgid!r}"


def test_no_markup_or_class_name_goes_through_gettext():
    assert list(_offenders()) == []


def test_the_scan_refuses_a_class_name_and_markup(tmp_path):
    classes = _css_classes() | {"btn", "w-full"}
    for msgid in ("btn-danger", "with-sidebar", "max-lg:pt-0", 'class="btn', 'a href="'):
        tokens = msgid.split()
        assert MARKUP.search(msgid) or all(t in classes or UTILITY.match(t) for t in tokens), msgid
    for msgid in ("Salvar", "Sair da organização", "e-mail", "Padrão para todos os tipos"):
        tokens = msgid.split()
        assert not (MARKUP.search(msgid) or all(t in classes or UTILITY.match(t) for t in tokens)), msgid
