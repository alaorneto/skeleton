"""W046 (#640): a template of the core shell overridden by the project is a warning; error pages are not."""

from django.test import override_settings

from skeleton.system.checks.shell import shell_override_checks


def templates_with(directory):
    from django.conf import settings

    engines = [dict(engine) for engine in settings.TEMPLATES]
    engines[0] = {**engines[0], "DIRS": [str(directory), *engines[0].get("DIRS", [])]}
    return engines


def write(root, name, text="{% block content %}{% endblock %}"):
    target = root / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def ids(messages):
    return [(message.id, message.obj) for message in messages]


def test_no_override_no_warning():
    assert shell_override_checks() == []


def test_shell_overrides_are_warned_error_pages_are_not(tmp_path, subtests):
    write(tmp_path, "skeleton/web/base.html")
    write(tmp_path, "account/base_entrance.html")
    write(tmp_path, "404.html")
    with override_settings(TEMPLATES=templates_with(tmp_path)):
        messages = shell_override_checks()
    with subtests.test(case="ids"):
        assert ids(messages) == [
            ("skeleton.W046", "skeleton/web/base.html"),
            ("skeleton.W046", "account/base_entrance.html"),
        ]
    with subtests.test(case="no server path in the message"):
        text = " ".join(message.msg + message.hint for message in messages)
        assert str(tmp_path) not in text and "docs/appearance.md" in text


def test_the_warning_can_be_silenced(tmp_path):
    write(tmp_path, "skeleton/web/error_base.html")
    with override_settings(TEMPLATES=templates_with(tmp_path), SILENCED_SYSTEM_CHECKS=["skeleton.W046"]):
        messages = shell_override_checks()
        assert messages and all(message.is_silenced() for message in messages)  # What manage.py check drops.
