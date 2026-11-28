"""Control of the application containers without the Docker socket (#651): protocol, launcher, hooks."""

import json
import os
import sys
import threading
import time
from argparse import Namespace
from pathlib import Path

import pytest

from deployment_agent import launcher
from deployment_agent.control import ControlError, read_json, request_and_wait, write_json, write_request

POSIX = pytest.mark.skipif(os.name != "posix", reason="Sinais e links simbólicos do container (Linux ou macOS).")


def fake_launcher(state_root, service, *, reply="mirror", delay=0.05):
    """A thread that answers the requests of ``service`` like a launcher would."""

    def answer(control_root):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            request = read_json(Path(control_root) / service / "request.json")
            if request:
                time.sleep(delay)
                state = {"stop": "stopped", "restart": "running"}[request["action"]] if reply == "mirror" else reply
                write_json(Path(state_root) / service / "state.json", {"id": request["id"], "state": state})
                return
            time.sleep(0.01)

    return answer


def run_with(control_root, state_root, services, action, answers, **kwargs):
    threads = [threading.Thread(target=answer, args=(control_root,)) for answer in answers]
    for thread in threads:
        thread.start()
    try:
        return request_and_wait(control_root, state_root, services, action, poll=0.01, **kwargs)
    finally:
        for thread in threads:
            thread.join()


@POSIX  # A reader thread and an atomic rename at once: only Linux and macOS allow it.
def test_requests_wait_for_every_confirmation(tmp_path):
    control, state = tmp_path / "control", tmp_path / "state"
    answers = [fake_launcher(state, "worker"), fake_launcher(state, "scheduler")]
    result = run_with(control, state, ["worker", "scheduler"], "stop", answers, timeout=5, release="/r/1")
    assert {service: value["state"] for service, value in result.items()} == {
        "worker": "stopped",
        "scheduler": "stopped",
    }
    request = read_json(control / "worker" / "request.json")
    assert request["action"] == "stop" and request["release"] == "/r/1" and len(request["id"]) == 32


def test_timeout_failure_and_invalid_requests(tmp_path, subtests):
    control, state = tmp_path / "control", tmp_path / "state"
    with subtests.test(case="nobody answers"):
        with pytest.raises(ControlError) as error:
            request_and_wait(control, state, ["web"], "restart", timeout=0.2, poll=0.01)
        assert (error.value.code, error.value.service) == ("service_control_timeout", "web")
    with subtests.test(case="a stale confirmation does not count"):
        write_json(state / "web" / "state.json", {"id": "0" * 32, "state": "stopped"})
        with pytest.raises(ControlError):
            request_and_wait(control, state, ["web"], "stop", timeout=0.2, poll=0.01)
    for case, services, action in (
        ("bad name", ["Web!"], "stop"),
        ("bad action", ["web"], "kill"),
        ("empty", [], "stop"),
    ):
        with subtests.test(case=case):
            with pytest.raises(ControlError):
                request_and_wait(control, state, services, action, timeout=0.1)


@POSIX
def test_a_reported_failure_stops_the_wait(tmp_path):
    control, state = tmp_path / "control", tmp_path / "state"
    with pytest.raises(ControlError) as error:
        run_with(control, state, ["web"], "restart", [fake_launcher(state, "web", reply="failed")], timeout=5)
    assert error.value.code == "service_control_failed"


def test_read_json_refuses_what_is_not_a_small_object(tmp_path):
    path = tmp_path / "x.json"
    for content in (b"[1]", b"{", b"\xff", json.dumps({"a": "b" * 20000}).encode()):
        path.write_bytes(content)
        assert read_json(path) is None
    assert read_json(tmp_path / "missing.json") is None


# --------------------------------------------------------------------------- the launcher


def release(root, name):
    target = root / name
    (target / ".venv" / "bin").mkdir(parents=True)
    os.symlink(sys.executable, target / ".venv" / "bin" / "python")
    return target


def point(active, target):
    temporary = active.with_name(f".{active.name}.tmp")
    temporary.symlink_to(target, target_is_directory=True)
    os.replace(temporary, active)


def child_script(tmp_path):
    """A child that records the release it runs and finishes its 'job' on SIGTERM."""
    script = tmp_path / "child.py"
    script.write_text(
        "import os, signal, sys, time\n"
        "out = sys.argv[1]\n"
        "open(out, 'a').write(os.environ['SKELETON_PROJECT_ROOT'] + ' ' + os.environ.get('MIGRATION_DATABASE_URL', '-') + '\\n')\n"
        "def stop(*_):\n"
        "    open(out, 'a').write('finished job\\n'); sys.exit(0)\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    return script


def make_launcher(tmp_path, *, grace=5.0, command=None, wait_for_agent=False):
    root = tmp_path / "releases"
    root.mkdir(exist_ok=True)
    options = Namespace(
        service="worker",
        control=str(tmp_path / "control"),
        state=str(tmp_path / "state"),
        releases_root=str(root),
        active=str(root / "current"),
        grace=grace,
        command=command or [sys.executable, str(child_script(tmp_path)), str(tmp_path / "out.txt")],
        wait_for_agent=wait_for_agent,
    )
    return launcher.Launcher(options), root


def state_of(tmp_path):
    return read_json(tmp_path / "state" / "worker" / "state.json")


def until(condition, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return False


@POSIX
def test_launcher_waits_runs_restarts_and_stops(tmp_path, monkeypatch):
    monkeypatch.setenv("MIGRATION_DATABASE_URL", "postgres://owner:secret@db/x")
    runner, root = make_launcher(tmp_path)
    try:
        runner.step()
        assert state_of(tmp_path)["state"] == "waiting"
        first, second = release(root, "r1"), release(root, "r2")
        point(root / "current", first)
        runner.step()
        assert state_of(tmp_path)["state"] == "running" and state_of(tmp_path)["release"] == str(first)
        out = tmp_path / "out.txt"
        assert until(lambda: out.exists() and str(first) in out.read_text())
        assert "secret" not in out.read_text()  # The owner credential never reaches the child.
        point(root / "current", second)
        request = write_request(tmp_path / "control", "worker", "restart")
        runner.step()
        assert state_of(tmp_path)["id"] == request and state_of(tmp_path)["release"] == str(second)
        assert until(lambda: str(second) in out.read_text())
        assert "finished job" in out.read_text()  # The first child finished on SIGTERM.
        stop = write_request(tmp_path / "control", "worker", "stop")
        runner.step()
        assert state_of(tmp_path)["state"] == "stopped" and state_of(tmp_path)["id"] == stop
        runner.step()
        assert runner.child is None  # Applied once: reading the same file again changes nothing.
    finally:
        runner.stop()


@POSIX
def test_in_a_cluster_the_launcher_waits_for_the_agent_even_with_a_release(tmp_path):
    """--wait-for-agent (#681): a node back from an update never starts the old pointer by itself."""
    runner, root = make_launcher(tmp_path, wait_for_agent=True)
    try:
        point(root / "current", release(root, "r1"))
        runner.step()
        runner.step()
        assert runner.child is None and state_of(tmp_path) == {
            "id": "",
            "pid": None,
            "reason": "agent",
            "release": "",
            "state": "waiting",
        }
        request = write_request(tmp_path / "control", "worker", "restart")
        runner.step()
        assert state_of(tmp_path)["state"] == "running" and state_of(tmp_path)["id"] == request
    finally:
        runner.stop()


@POSIX
def test_a_stop_survives_a_restart_of_the_container(tmp_path):
    runner, root = make_launcher(tmp_path)
    point(root / "current", release(root, "r1"))
    runner.step()
    write_request(tmp_path / "control", "worker", "stop")
    runner.step()
    again, _ = make_launcher(tmp_path)
    again.step()
    assert again.child is None and state_of(tmp_path)["state"] == "stopped"


@POSIX
def test_the_grace_period_then_kill(tmp_path):
    stubborn = tmp_path / "stubborn.py"
    stubborn.write_text(
        "import signal, time\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\nwhile True: time.sleep(0.05)\n"
    )
    runner, root = make_launcher(tmp_path, grace=0.3, command=[sys.executable, str(stubborn)])
    point(root / "current", release(root, "r1"))
    runner.step()
    child = runner.child
    time.sleep(0.2)
    write_request(tmp_path / "control", "worker", "stop")
    started = time.monotonic()
    runner.step()
    assert child.poll() is not None and time.monotonic() - started < 3
    assert state_of(tmp_path)["state"] == "stopped"


@POSIX
def test_refusals_of_the_launcher(tmp_path, subtests):
    runner, root = make_launcher(tmp_path)
    outside = tmp_path / "outside"
    (outside / ".venv" / "bin").mkdir(parents=True)
    with subtests.test(case="pointer out of the releases root"):
        (root / "current").symlink_to(outside, target_is_directory=True)
        runner.step()
        assert runner.child is None and state_of(tmp_path)["reason"] == "active_release_outside_root"
    with subtests.test(case="malformed request is ignored"):
        (tmp_path / "control" / "worker").mkdir(parents=True)
        (tmp_path / "control" / "worker" / "request.json").write_text('{"action": "explode"}')
        runner.step()
        assert runner.child is None
    with subtests.test(case="incomplete release"):
        bare = root / "bare"
        bare.mkdir()
        point(root / "current", bare)
        runner.step()
        assert state_of(tmp_path)["reason"] == "active_release_incomplete"


@POSIX
def test_a_child_that_exits_ends_the_launcher_with_its_code(tmp_path):
    runner, root = make_launcher(tmp_path, command=[sys.executable, "-c", "raise SystemExit(3)"])
    point(root / "current", release(root, "r1"))
    runner.step()
    assert until(lambda: runner.child.poll() is not None)
    assert runner.step() == 3 and state_of(tmp_path)["state"] == "exited"


def test_release_environment_and_command():
    env = launcher.release_environment(Path("/releases/r1"), {"PATH": "/usr/bin", "MIGRATION_DATABASE_URL": "x"})
    assert "MIGRATION_DATABASE_URL" not in env
    assert env["SKELETON_PROJECT_ROOT"] == str(Path("/releases/r1"))
    assert env["PATH"].startswith(str(Path("/releases/r1/.venv/bin")))
    command = launcher.command_for(["{venv}/bin/gunicorn", "--chdir", "{release}", "{python}"], Path("/r"))
    assert command[0].endswith("gunicorn") and command[2] == str(Path("/r"))


def test_main_refuses_invalid_arguments(tmp_path):
    base = [
        "--control",
        str(tmp_path),
        "--state",
        str(tmp_path / "s"),
        "--releases-root",
        str(tmp_path),
        "--active",
        str(tmp_path / "c"),
    ]
    assert launcher.main(["--service", "Web!", *base, "--", "x"]) == 2
    assert launcher.main(["--service", "web", *base]) == 2


# --------------------------------------------------------------------------- the agent side


@pytest.mark.skipif(os.name != "posix", reason="O agente de deployment exige fcntl (Linux ou macOS).")
def test_the_control_hook_and_its_configuration(tmp_path, subtests, capsys):
    from deployment_agent.__main__ import control_services
    from deployment_agent.storage import AgentError
    from tests.unit.agent_helpers import load_config

    control, state = tmp_path / "control", tmp_path / "state"
    config = load_config(
        tmp_path, service_control={"control_root": str(control), "state_root": str(state), "timeout": 5}
    )
    options = Namespace(action="restart", services=["web"], release="/r", operation="op")
    thread = threading.Thread(target=fake_launcher(state, "web"), args=(control,))
    thread.start()
    result = control_services(config, options)
    thread.join()
    assert result["services"]["web"]["state"] == "running"
    for case, value in (
        ("relative", {"control_root": "c", "state_root": str(state)}),
        ("same directory", {"control_root": str(control), "state_root": str(control)}),
        ("unknown key", {"control_root": str(control), "state_root": str(state), "x": 1}),
        ("bad timeout", {"control_root": str(control), "state_root": str(state), "timeout": 0}),
    ):
        with subtests.test(case=case):
            with pytest.raises(AgentError) as error:
                load_config(tmp_path, service_control=value)
            assert error.value.code == "invalid_service_control"
    with subtests.test(case="not configured"):
        with pytest.raises(AgentError) as error:
            control_services(load_config(tmp_path), options)
        assert error.value.code == "service_control_not_configured"
    with subtests.test(case="timeout becomes an agent error"):
        quick = load_config(
            tmp_path, service_control={"control_root": str(control), "state_root": str(state), "timeout": 0.2}
        )
        with pytest.raises(AgentError) as error:
            control_services(quick, Namespace(action="stop", services=["worker"], release="", operation=""))
        assert error.value.code == "service_control_timeout"


def test_wait_for_agent_recovery_state_machine_and_exec_error(tmp_path, monkeypatch, subtests):
    """#916: launcher resumes running after crash under --wait-for-agent and handles Popen OSError."""
    control, state = tmp_path / "control", tmp_path / "state"
    with subtests.test(case="fresh start without state stays held"):
        runner, _ = make_launcher(tmp_path, wait_for_agent=True)
        assert runner.wanted == "held"

    req_id = write_request(control, "worker", "restart")
    with subtests.test(case="restarted container after crash resumes running when request matches"):
        write_json(state / "worker" / "state.json", {"id": req_id, "state": "exited", "wanted": "running"})
        runner, _ = make_launcher(tmp_path, wait_for_agent=True)
        assert runner.wanted == "running"

    with subtests.test(case="explicit stop stays stopped across restart"):
        stop_id = write_request(control, "worker", "stop")
        write_json(state / "worker" / "state.json", {"id": stop_id, "state": "stopped", "wanted": "stopped"})
        runner, _ = make_launcher(tmp_path, wait_for_agent=True)
        assert runner.wanted == "stopped"

    with subtests.test(case="Popen OSError publishes failed without crashing"):
        write_json(state / "worker" / "state.json", {"id": "", "state": "waiting"})
        runner, root = make_launcher(tmp_path, command=["/nonexistent-binary-916"])
        fake_release = root / "r1"
        fake_release.mkdir(exist_ok=True)
        monkeypatch.setattr(launcher, "resolve_active", lambda _a, _r: (fake_release, ""))

        def boom(*_args, **_kwargs):
            raise OSError("No such file")

        monkeypatch.setattr(launcher.subprocess, "Popen", boom)
        runner.start()
        assert runner.child is None
        current = state_of(tmp_path)
        assert current["state"] == "failed" and current["reason"] == "exec_failed"


@POSIX
def test_wait_for_agent_survives_child_sigkill_and_container_restart(tmp_path):
    """#916: killing the child and recreating the launcher under --wait-for-agent resumes running."""
    runner, root = make_launcher(tmp_path, wait_for_agent=True)
    try:
        point(root / "current", release(root, "r1"))
        request = write_request(tmp_path / "control", "worker", "restart")
        runner.step()
        assert state_of(tmp_path)["state"] == "running" and state_of(tmp_path)["wanted"] == "running"
        os.kill(runner.child.pid, 9)
        assert until(lambda: runner.child.poll() is not None)
        assert runner.step() != 0 and state_of(tmp_path)["state"] == "exited"
    finally:
        runner.stop()
    restarted, _ = make_launcher(tmp_path, wait_for_agent=True)
    try:
        restarted.step()
        assert restarted.child is not None
        assert state_of(tmp_path)["state"] == "running" and state_of(tmp_path)["id"] == request
    finally:
        restarted.stop()


@POSIX
def test_stop_kills_entire_process_group_without_orphans(tmp_path):
    """#916: stop() sends SIGTERM/SIGKILL to the child's process group so grandchildren do not survive."""
    pid_file = tmp_path / "grandchild.pid"
    parent_script = tmp_path / "parent.py"
    parent_script.write_text(
        "import subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        f"open({str(pid_file)!r}, 'w').write(str(child.pid))\n"
        "while True: time.sleep(0.05)\n",
        encoding="utf-8",
    )
    runner, root = make_launcher(tmp_path, grace=0.3, command=[sys.executable, str(parent_script)])
    try:
        point(root / "current", release(root, "r1"))
        runner.step()
        assert until(lambda: pid_file.exists() and pid_file.read_text().strip().isdigit())
        grandchild_pid = int(pid_file.read_text().strip())
        write_request(tmp_path / "control", "worker", "stop")
        runner.step()
        assert until(lambda: not _pid_alive(grandchild_pid), timeout=3.0)
    finally:
        runner.stop()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def test_follower_reconciles_held_services_under_wait_for_agent(tmp_path):
    """#916: Follower.ensure_started() restarts services reported as waiting (agent) outside maintenance."""
    from types import SimpleNamespace

    from deployment_agent.rollout import Follower

    state_root = tmp_path / "state"
    release_dir = tmp_path / "releases" / "r1"
    release_dir.mkdir(parents=True)
    marker = tmp_path / "maintenance.json"
    started: list[tuple[Path, str]] = []
    service = SimpleNamespace(
        config={"maintenance_marker": str(marker), "service_control": {"state_root": str(state_root)}},
        coordinator=SimpleNamespace(),
        backend=SimpleNamespace(start=lambda rel, op: started.append((rel, op))),
        state=lambda: {"release": str(release_dir), "inventory": {"generation": 1}},
    )
    follower = Follower(service)  # type: ignore[arg-type]

    # Initial boot starts services once.
    write_json(state_root / "web" / "state.json", {"id": "", "state": "waiting", "reason": "agent"})
    follower.ensure_started()
    assert len(started) == 1

    # Once running, subsequent heartbeats do not restart.
    write_json(state_root / "web" / "state.json", {"id": "a" * 32, "state": "running", "wanted": "running"})
    follower.ensure_started()
    assert len(started) == 1

    # Container crashes and restarts with wiped state -> held in waiting (agent): follower reconciles!
    write_json(state_root / "web" / "state.json", {"id": "", "state": "waiting", "reason": "agent"})
    follower.ensure_started()
    assert len(started) == 2

    # During maintenance, follower never restarts held services.
    marker.write_text("{}", encoding="utf-8")
    follower.ensure_started()
    assert len(started) == 2
