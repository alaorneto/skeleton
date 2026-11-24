"""The agent container's entrypoint (#772): the volume's agent when newer and sound, else the image's."""

import json
import os
import sys

import pytest
from packaging.version import Version

from deployment_agent import entry

pytestmark = pytest.mark.posix

PYTHON = (3, 14)
IMAGE = Version("0.7.0")
IMAGE_PYTHON = "/usr/local/bin/python"
IMAGE_AGENT = (IMAGE_PYTHON, [IMAGE_PYTHON, "-m", "deployment_agent.bootstrap"])


def version(root, name, *, number="0.8.0", python="3.14.10", ready=True, current=True):
    """A version of the agent under ``root`` as selfupdate.build_version and switch leave it."""
    directory = root / "versions" / name
    (directory / "venv" / "bin").mkdir(parents=True)
    (directory / "venv" / "bin" / "python").write_text("")
    (directory / "venv" / "pyvenv.cfg").write_text(f"home = /usr/local/bin\nversion = {python}\n")
    if ready:
        (directory / "READY").write_text("")
    if current:
        (root / "current").unlink(missing_ok=True)
        os.symlink(f"versions/{name}", root / "current")
        (root / "state.json").write_text(json.dumps({"current": name, "previous": None, "version": number}))
    return directory / "venv" / "bin" / "python"


def choose(root, **environ):
    return entry.choose(
        {"SKELETON_AGENT_CODE": str(root), **environ}, image=IMAGE, python=PYTHON, executable=IMAGE_PYTHON
    )


def boot(root):
    return json.loads((root / entry.BOOT_FILE).read_text())


def test_a_newer_sound_version_of_the_volume_runs_and_its_start_is_counted(tmp_path):
    python = version(tmp_path, "0.8.0-aaaaaaaaaaaa")
    assert choose(tmp_path) == (str(python), [str(python), "-m", "deployment_agent.bootstrap"])
    assert boot(tmp_path) == {"candidate": "0.8.0-aaaaaaaaaaaa", "attempts": 1}
    choose(tmp_path)
    assert boot(tmp_path)["attempts"] == 2


def test_the_image_runs_whenever_the_volume_is_not_newer_or_not_sound(tmp_path, subtests):
    cases = {
        "same version: a newer image wins, an equal one too": {"number": "0.7.0"},
        "older version": {"number": "0.6.3"},
        "prerelease of the image's version": {"number": "0.7.0b1"},
        "not ready (a build that stopped)": {"ready": False},
        "venv of another Python": {"python": "3.13.5"},
    }
    for case, options in cases.items():
        with subtests.test(case=case):
            root = tmp_path / case.split(":")[0].replace(" ", "-")
            root.mkdir()
            version(root, "x-bbbbbbbbbbbb", **options)
            assert choose(root) == IMAGE_AGENT
            assert not (root / entry.BOOT_FILE).exists()


def test_the_image_runs_without_a_usable_volume(tmp_path, subtests):
    with subtests.test(case="switched off"):
        version(tmp_path, "0.8.0-aaaaaaaaaaaa")
        assert choose(tmp_path, SKELETON_AGENT_CODE="none") == IMAGE_AGENT
    with subtests.test(case="emergency switch"):
        assert choose(tmp_path, SKELETON_AGENT_USE_IMAGE="1") == IMAGE_AGENT
    with subtests.test(case="missing or relative directory"):
        assert choose(tmp_path / "missing") == IMAGE_AGENT
        assert choose(tmp_path, SKELETON_AGENT_CODE="agent/code") == IMAGE_AGENT
    with subtests.test(case="unknown image version (a source checkout)"):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(entry, "image_version", lambda: None)
            assert entry.choose({"SKELETON_AGENT_CODE": str(tmp_path)}, python=PYTHON, executable=IMAGE_PYTHON) == (
                IMAGE_AGENT
            )
    with subtests.test(case="empty volume"):
        empty = tmp_path / "empty"
        empty.mkdir()
        assert choose(empty) == IMAGE_AGENT


def test_a_current_link_or_state_the_agent_never_writes_is_ignored(tmp_path, subtests):
    with subtests.test(case="link outside versions"):
        root = tmp_path / "outside"
        root.mkdir()
        version(root, "0.8.0-aaaaaaaaaaaa")
        (root / "current").unlink()
        os.symlink(str(root / "versions" / "0.8.0-aaaaaaaaaaaa"), root / "current")  # Absolute.
        assert choose(root) == IMAGE_AGENT
        (root / "current").unlink()
        os.symlink("versions/../versions/0.8.0-aaaaaaaaaaaa", root / "current")
        assert choose(root) == IMAGE_AGENT
    with subtests.test(case="corrupt state"):
        root = tmp_path / "corrupt"
        root.mkdir()
        version(root, "0.8.0-aaaaaaaaaaaa")
        (root / "state.json").write_text("{")
        assert choose(root) == IMAGE_AGENT
    with subtests.test(case="state of another version"):
        root = tmp_path / "other"
        root.mkdir()
        version(root, "0.8.0-aaaaaaaaaaaa")
        (root / "state.json").write_text(json.dumps({"current": "0.9.0-cccccccccccc", "version": "0.9.0"}))
        assert choose(root) == IMAGE_AGENT
    with subtests.test(case="not a link"):
        root = tmp_path / "directory"
        root.mkdir()
        version(root, "0.8.0-aaaaaaaaaaaa")
        (root / "current").unlink()
        (root / "current").mkdir()
        assert choose(root) == IMAGE_AGENT


def test_after_three_starts_that_never_served_the_image_runs(tmp_path, capsys):
    python = version(tmp_path, "0.8.0-aaaaaaaaaaaa")
    for _attempt in range(entry.MAX_ATTEMPTS):
        assert choose(tmp_path)[0] == str(python)
    assert choose(tmp_path) == IMAGE_AGENT
    assert json.loads(capsys.readouterr().err.splitlines()[-1]) == {
        "event": "agent.entry_fallback",
        "candidate": "0.8.0-aaaaaaaaaaaa",
        "attempts": entry.MAX_ATTEMPTS,
    }
    assert choose(tmp_path) == IMAGE_AGENT  # Until that version serves, or another one is current.
    newer = version(tmp_path, "0.9.0-dddddddddddd", number="0.9.0")
    assert choose(tmp_path)[0] == str(newer)
    assert boot(tmp_path) == {"candidate": "0.9.0-dddddddddddd", "attempts": 1}


def test_a_start_that_served_resets_the_count_only_for_the_volume_agent(tmp_path):
    from deployment_agent.selfupdate import confirm_start

    version(tmp_path, "0.8.0-aaaaaaaaaaaa")
    for _attempt in range(entry.MAX_ATTEMPTS):
        choose(tmp_path)
    config = {"agent_root": str(tmp_path)}
    # The image's agent (the fallback) leaves the count alone: the broken version stays skipped.
    assert confirm_start(config, prefix="/usr/local") is False
    assert boot(tmp_path)["attempts"] == entry.MAX_ATTEMPTS
    assert confirm_start(config, prefix=tmp_path / "versions" / "0.8.0-aaaaaaaaaaaa" / "venv") is True
    assert boot(tmp_path) == {"candidate": "0.8.0-aaaaaaaaaaaa", "attempts": 0}
    assert confirm_start({}, prefix=tmp_path / "versions" / "0.8.0-aaaaaaaaaaaa" / "venv") is False


def test_main_executes_the_choice_and_falls_back_to_the_image_when_exec_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(entry, "image_version", lambda: IMAGE)
    running = f"{sys.version_info.major}.{sys.version_info.minor}.0"
    python = version(tmp_path, "0.8.0-aaaaaaaaaaaa", python=running)
    calls = []

    def execv(executable, argv):
        calls.append((executable, argv))
        if executable == str(python):
            raise OSError("exec format error")

    entry.main({"SKELETON_AGENT_CODE": str(tmp_path)}, execv=execv)
    assert calls == [
        (str(python), [str(python), "-m", "deployment_agent.bootstrap"]),
        (sys.executable, [sys.executable, "-m", "deployment_agent.bootstrap"]),
    ]
