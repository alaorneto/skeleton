"""One icon per concept and one concept per icon, on every screen (#768)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SOURCES = [ROOT / "skeleton", ROOT / "examples"]
ICON_TEMPLATE = ROOT / "skeleton/web/templates/skeleton/web/components/icon.html"
LITERAL = re.compile(r"""(?:with name|icon)=["']([a-z-]+)["'](?!\|)""")
CONCEPT = re.compile(r"""["']([a-z_]+)["']\|concept_icon""")


def _templates():
    for source in SOURCES:
        for path in sorted(source.rglob("*.html")):
            if path != ICON_TEMPLATE:
                yield path


def _drawn_icons():
    return set(re.findall(r'name == "([a-z-]+)"', ICON_TEMPLATE.read_text(encoding="utf-8")))


def test_the_map_is_one_to_one_and_apart_from_the_action_icons():
    from skeleton.web.icons import ACTION_ICONS, CONCEPT_ICONS

    icons = list(CONCEPT_ICONS.values())
    repeated = {icon for icon in icons if icons.count(icon) > 1}
    assert not repeated, f"icons shared by two concepts: {sorted(repeated)}"
    assert not set(icons) & ACTION_ICONS
    drawn = _drawn_icons()
    assert set(icons) <= drawn, sorted(set(icons) - drawn)
    assert ACTION_ICONS <= drawn, sorted(ACTION_ICONS - drawn)
    assert drawn <= set(icons) | ACTION_ICONS, f"icons with no role: {sorted(drawn - set(icons) - ACTION_ICONS)}"


def test_templates_write_only_action_icons_and_name_concepts():
    from skeleton.web.icons import ACTION_ICONS, CONCEPT_ICONS

    literal, unknown = [], []
    for path in _templates():
        text = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT)
        for match in LITERAL.finditer(text):
            if match.group(1) not in ACTION_ICONS:
                literal.append(f"{relative}: {match.group(1)}")
        for match in CONCEPT.finditer(text):
            if match.group(1) not in CONCEPT_ICONS:
                unknown.append(f"{relative}: {match.group(1)}")
    assert literal == [], 'a concept icon is written by its concept: name="<concept>"|concept_icon'
    assert unknown == []


def test_settings_items_name_concepts():
    from skeleton.web.icons import CONCEPT_ICONS
    from skeleton.web.navigation import SETTINGS_GROUPS

    items = [item for _, _, entries in SETTINGS_GROUPS for item in entries]
    assert items and all(item.icon in CONCEPT_ICONS for item in items), [i.icon for i in items]


def test_an_unknown_concept_does_not_break_the_page():
    from django.template import Context, Template

    rendered = Template(
        '{% load skeleton_format %}{% include "skeleton/web/components/icon.html" with name="nope"|concept_icon %}'
    ).render(Context({}))
    assert '<circle cx="12" cy="12" r="3"/>' in rendered
