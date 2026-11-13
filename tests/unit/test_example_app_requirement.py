"""One rule decides what happens to tests that need examples.notes (#264)."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
# Files that cite the example app only as data, without needing it installed.
NOT_DEPENDENT = frozenset(
    {
        # URLs of the example app as data, and an optional branch when it is installed.
        "tests/integration/test_platform_boundary.py",
        # Exercises the example table only when it is installed.
        "tests/integration/test_platform_instance.py",
    }
)


@pytest.mark.parametrize(
    ("installed", "required", "action"),
    [(True, False, "run"), (True, True, "run"), (False, False, "skip"), (False, True, "error")],
)
def test_example_app_action_matrix(pytestconfig, installed, required, action):
    conftest = next(
        plugin
        for plugin in pytestconfig.pluginmanager.get_plugins()
        if Path(getattr(plugin, "__file__", None) or ".").resolve() == ROOT / "tests" / "conftest.py"
    )
    assert conftest.example_app_action(installed=installed, required=required) == action


def test_every_notes_dependent_module_is_marked():
    unmarked = []
    for path in sorted((ROOT / "tests").glob("integration*/*.py")):
        relative = path.relative_to(ROOT).as_posix()
        text = path.read_text(encoding="utf-8")
        if ("examples.notes" in text or "example_notes" in text) and "needs_example_app" not in text:
            if relative not in NOT_DEPENDENT:
                unmarked.append(relative)
    assert unmarked == []
    copies = [
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "tests").rglob("*.py")
        if "SKELETON_ENABLE_EXAMPLE_APP=true is required" in path.read_text(encoding="utf-8")
        and path.relative_to(ROOT).as_posix() not in {"tests/conftest.py", "tests/unit/test_example_app_requirement.py"}
    ]
    assert copies == []
