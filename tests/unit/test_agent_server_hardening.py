"""Control and status servers of the deployment agent: token, socket mode, signals, errors (#280)."""

import io
import json
import os
import signal
import stat
import subprocess
import sys
import time
from contextlib import redirect_stderr
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import server as server_module  # noqa: E402
from deployment_agent.server import ControlHandler, StatusHandler, install_signal_handlers  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402

pytestmark = pytest.mark.posix
TOKEN = "disposable-control-token-at-least-32-chars"
OPERATION = "11111111-2222-4333-8444-555555555555"


@pytest.fixture
def token_file(tmp_path):
    path = tmp_path / "control-token"
    path.write_text(TOKEN)
    path.chmod(0o600)
    return path


def control_handler(token_file, authorization):
    handler = ControlHandler.__new__(ControlHandler)
    handler.headers = {"Authorization": authorization}
    handler.server = SimpleNamespace(service=SimpleNamespace(config={"token_file": str(token_file)}))
    return handler


def test_non_ascii_authorization_is_401_not_500(token_file):
    # http.server decodes headers as latin-1: a byte above 0x7F arrives as a non-ASCII character.
    handler = control_handler(token_file, "Bearer \xe9" + TOKEN[1:])
    with pytest.raises(AgentError) as raised:
        handler.authenticate()
    assert (raised.value.code, raised.value.status) == ("unauthorized", 401)


def test_non_ascii_authorization_does_not_log_internal_error(token_file):
    stream = io.StringIO()
    with redirect_stderr(stream), pytest.raises(AgentError):
        control_handler(token_file, "Bearer \xff\xfe").authenticate()
    assert "agent.internal_error" not in stream.getvalue()


def test_valid_token_is_still_accepted(token_file):
    control_handler(token_file, "Bearer " + TOKEN).authenticate()


def test_wrong_ascii_token_is_still_401(token_file):
    for value in ("", "Bearer wrong", "Basic " + TOKEN, "Bearer " + TOKEN + "x"):
        with pytest.raises(AgentError, match="unauthorized"):
            control_handler(token_file, value).authenticate()


def status_handler(tmp_path, operation):
    handler = StatusHandler.__new__(StatusHandler)
    handler.path = f"/status/{OPERATION}?ticket=t"
    store = SimpleNamespace(path=lambda collection, key: tmp_path / "exists")
    (tmp_path / "exists").write_text("{}")
    service = SimpleNamespace(
        config={"status_http": {"public_url": "https://status.example.test/status"}}, store=store, operation=operation
    )
    handler.server = SimpleNamespace(service=service, tickets=SimpleNamespace(verify=lambda *args: None))
    handler.wfile = io.BytesIO()
    handler.sent = []
    handler.send_json = lambda value, status=200: handler.sent.append((status, value))
    handler.send_response = handler.send_header = lambda *args: None
    handler.end_headers = lambda: None
    return handler


def test_status_handler_unexpected_error_is_json_500_without_message(tmp_path):
    def operation(operation_id):
        raise RuntimeError("segredo")

    handler = status_handler(tmp_path, operation)
    stream = io.StringIO()
    with redirect_stderr(stream):
        handler.do_GET()
    assert handler.sent == [(500, {"error": "agent_internal_error"})]
    record = json.loads(stream.getvalue().strip())
    assert record["event"] == "agent.internal_error" and record["error_class"] == "RuntimeError"
    assert "segredo" not in stream.getvalue()


def test_status_page_shows_generation_zero(tmp_path):
    summary = {
        "id": OPERATION,
        "state": "succeeded",
        "stage": "ready",
        "generation": 0,
        "error": None,
        "created_at": "2026-11-13",
        "updated_at": "2026-11-13",
    }
    handler = status_handler(tmp_path, lambda operation_id: summary)
    handler.do_GET()
    page = handler.wfile.getvalue().decode()
    assert "<th>generation</th><td>0</td>" in page
    assert "<th>error</th><td>—</td>" in page


def test_install_signal_handlers_requests_shutdown(monkeypatch):
    called, registered = Event(), {}
    monkeypatch.setattr(server_module.signal, "signal", lambda signum, handler: registered.setdefault(signum, handler))
    install_signal_handlers(SimpleNamespace(shutdown=called.set))
    assert set(registered) == {signal.SIGTERM, signal.SIGINT}
    registered[signal.SIGTERM](signal.SIGTERM, None)
    assert called.wait(5)


def serving(tmp_path, token_file, *, worker=None):
    service = SimpleNamespace(
        config={"socket_path": str(tmp_path / "control" / "agent.sock"), "token_file": str(token_file)},
        reconcile_interrupted=lambda: None,
        worker=worker,
        store=SimpleNamespace(root=tmp_path / "journal"),
    )
    service.store.root.mkdir(mode=0o700)
    return service, Path(service.config["socket_path"])


def wait_for(path, timeout=10):
    deadline = time.monotonic() + timeout
    while not path.exists():
        if time.monotonic() > deadline:
            raise AssertionError(f"{path} never appeared")
        time.sleep(0.01)


def test_control_socket_is_never_more_open_than_0660(tmp_path, token_file, monkeypatch, servers):
    service, path = serving(tmp_path, token_file)
    modes, real_chmod = [], os.chmod

    def chmod(target, mode, *args, **kwargs):
        if Path(target) == path:
            modes.append(stat.S_IMODE(os.stat(target).st_mode))
        return real_chmod(target, mode, *args, **kwargs)

    monkeypatch.setattr(server_module.os, "chmod", chmod)
    previous = os.umask(0)
    try:
        thread = Thread(target=server_module.serve, args=(service,), daemon=True)
        thread.start()
        wait_for(path)
        while not modes:
            time.sleep(0.01)
        assert modes[0] & 0o117 == 0  # No bits for others and no execution bits.
        assert os.umask(0) == 0  # serve() restored the process umask it found.
    finally:
        os.umask(previous)
        _stop(servers)
        thread.join(5)


def test_umask_is_restored_after_binding(tmp_path, token_file, servers):
    service, path = serving(tmp_path, token_file)
    previous = os.umask(0o022)
    try:
        thread = Thread(target=server_module.serve, args=(service,), daemon=True)
        thread.start()
        wait_for(path)
        time.sleep(0.05)
        assert os.umask(0o022) == 0o022
    finally:
        os.umask(previous)
        _stop(servers)
        thread.join(5)


@pytest.fixture
def servers(monkeypatch):
    """The control servers serve() creates, so a test can shut them down like a signal would."""
    created = []
    original = server_module.UnixHTTPServer.__init__

    def tracking(self, *args, **kwargs):
        original(self, *args, **kwargs)
        created.append(self)

    monkeypatch.setattr(server_module.UnixHTTPServer, "__init__", tracking)
    return created


def _stop(servers):
    for server in servers:
        server.shutdown()


def test_serve_waits_for_running_operation_before_exiting(tmp_path, token_file, servers):
    release = Event()
    worker = Thread(target=release.wait, daemon=True)
    worker.start()
    service, path = serving(tmp_path, token_file, worker=worker)
    thread = Thread(target=server_module.serve, args=(service,), daemon=True)
    thread.start()
    wait_for(path)
    _stop(servers)
    thread.join(0.5)
    assert thread.is_alive()  # Still waiting for the operation.
    release.set()
    thread.join(5)
    assert not thread.is_alive() and not path.exists()


def test_sigterm_stops_idle_agent_and_removes_socket(tmp_path, token_file):
    socket_path = tmp_path / "control" / "agent.sock"
    script = (
        "from pathlib import Path; from types import SimpleNamespace; from deployment_agent.server import serve; "
        f"serve(SimpleNamespace(config={{'socket_path': {str(socket_path)!r}, 'token_file': {str(token_file)!r}}}, "
        f"reconcile_interrupted=lambda: None, worker=None, store=SimpleNamespace(root=Path({str(tmp_path / 'journal')!r}))))"
    )
    (tmp_path / "journal").mkdir(mode=0o700)
    root = Path(__file__).resolve().parents[2]
    process = subprocess.Popen([sys.executable, "-c", script], cwd=root, env={**os.environ, "PYTHONPATH": str(root)})
    try:
        wait_for(socket_path)
        process.send_signal(signal.SIGTERM)
        assert process.wait(timeout=10) == 0
        assert not socket_path.exists()
    finally:
        if process.poll() is None:
            process.kill()
