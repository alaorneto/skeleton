"""Private per-operation command logs and internal error stacks of the deployment agent (#244)."""

import io
import json
import os
import stat
import sys
from contextlib import redirect_stderr

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from contextlib import nullcontext  # noqa: E402
from types import SimpleNamespace  # noqa: E402
from uuid import uuid4  # noqa: E402

from deployment_agent import backends  # noqa: E402
from deployment_agent import service as service_module  # noqa: E402
from deployment_agent.backends import Backend  # noqa: E402
from deployment_agent.releases import owned_snapshot  # noqa: E402
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store, atomic_json, digest  # noqa: E402

pytestmark = pytest.mark.posix
OPERATION = "11111111-2222-4333-8444-555555555555"


def python(code):
    return [sys.executable, "-c", code]


@pytest.fixture
def backend(tmp_path):
    store = Store(tmp_path / "journal")
    return Backend({"storage_root": str(store.root), "command_timeout": 60}, store)


def test_failed_command_output_is_kept_in_private_operation_log(backend):
    with backend.logging_to(OPERATION) as directory:
        with pytest.raises(AgentError) as raised:
            backend.run(
                python("import sys; print('out'); print('err', file=sys.stderr); sys.exit(3)"),
                env=dict(os.environ),
                code="migration_failed",
            )
    assert raised.value.code == "migration_failed"
    assert raised.value.log == "01-migration_failed.log"
    content = (directory / raised.value.log).read_text()
    assert "out" in content and "err" in content


def test_successful_command_output_is_also_kept(backend):
    with backend.logging_to(OPERATION) as directory:
        backend.run(python("print('done')"), env=dict(os.environ), code="collectstatic_failed")
        backend.run(python("print('again')"), env=dict(os.environ), code="check_failed")
    assert sorted(path.name for path in directory.iterdir()) == ["01-collectstatic_failed.log", "02-check_failed.log"]
    assert "done" in (directory / "01-collectstatic_failed.log").read_text()


def test_command_log_is_bounded(backend):
    with backend.logging_to(OPERATION) as directory:
        backend.run(python("import sys; sys.stdout.write('x' * (1024 * 1024) + 'END')"), env=dict(os.environ))
    path = next(directory.iterdir())
    assert path.stat().st_size <= backends.MAX_COMMAND_LOG_BYTES
    assert path.read_text().endswith("END")


def test_no_log_file_outside_an_operation(backend):
    backend.run(python("print('quiet')"), env=dict(os.environ))
    with pytest.raises(AgentError) as raised:
        backend.run(python("import sys; sys.exit(1)"), env=dict(os.environ))
    assert raised.value.log is None
    assert not (backend.store.root / "logs").exists()


def test_capture_keeps_stdout_as_data_and_logs_only_stderr(backend):
    with backend.logging_to(OPERATION) as directory:
        output = backend.capture(
            python("import sys; print('data'); print('noise', file=sys.stderr)"),
            env=dict(os.environ),
            code="probe_failed",
        )
    assert output.strip() == b"data"
    content = (directory / "01-probe_failed.log").read_text()
    assert "noise" in content and "data" not in content


def test_log_directory_and_file_modes_are_private(backend):
    with backend.logging_to(OPERATION) as directory:
        backend.run(python("print('x')"), env=dict(os.environ))
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(next(directory.iterdir()).stat().st_mode) == 0o600


def test_log_file_never_follows_a_planted_link(backend, tmp_path):
    """A link planted in the log directory is never written through (new name, O_EXCL)."""
    target = tmp_path / "outside.txt"
    target.write_text("keep")
    with backend.logging_to(OPERATION) as directory:
        (directory / "01-command_failed.log").symlink_to(target)
        backend.run(python("print('written')"), env=dict(os.environ))
    assert target.read_text() == "keep"
    written = directory / "02-command_failed.log"
    assert not written.is_symlink() and "written" in written.read_text()


def test_operation_ids_are_validated(backend):
    with pytest.raises(AgentError):
        with backend.logging_to("../escape"):
            pass


# Same approved plan as tests/unit/test_deployment_journal.py (test modules are not importable).
@pytest.fixture
def approved_deployment(tmp_path, monkeypatch):
    source, release = tmp_path / "source", tmp_path / "release"
    source.mkdir()
    release.mkdir()
    (source / "approved.txt").write_text("before")
    (release / "approved.txt").write_text("after")
    source_owned, target_owned = owned_snapshot(source), owned_snapshot(release)
    store = Store(tmp_path / "journal")
    plan = {
        "id": str(uuid4()),
        "generation": 0,
        "action": "install",
        "release": str(release),
        "artifacts": [],
        "migrations": [],
        "applied_migrations": [],
        "migration_targets": [],
    }
    plan["digest"] = digest(plan)
    previous_inventory = {"generation": 0}
    target_inventory = {"generation": 1}
    store.put(
        "plans",
        plan["id"],
        {
            "plan": plan,
            "previous_release": str(source),
            "source_owned_digest": digest(source_owned),
            "owned_files": target_owned,
            "previous_owned_files": source_owned,
            "previous_inventory": previous_inventory,
            "inventory": target_inventory,
        },
    )
    atomic_json(
        store.root / "active.json",
        {
            "release": str(source),
            "inventory": previous_inventory,
            "owned_files": source_owned,
        },
    )
    service = DeploymentService.__new__(DeploymentService)
    service.store = store
    service.worker = None
    service.config = {
        "maintenance_marker": str(tmp_path / "maintenance"),
        "releases_root": str(tmp_path),
        "minimum_free_bytes": 0,
    }
    service.backend = SimpleNamespace(
        active=lambda: source,
        probe=lambda path: {"migrations": [], "applied": [], "targets": []},
        drain=lambda *args: None,
        backup=lambda *args: str(tmp_path / "backup.json"),
        logging_to=lambda operation_id: nullcontext(),
        preflight=lambda *args: None,
    )
    pending = []

    class DeferredWorker:
        def __init__(self, *, target, args, **kwargs):
            self.target, self.args = target, args

        def start(self):
            pending.append(self)

    monkeypatch.setattr(service_module, "threading", SimpleNamespace(Thread=DeferredWorker))
    values = {
        "plan_id": plan["id"],
        "plan_digest": plan["digest"],
        "generation": 0,
        "operation_id": str(uuid4()),
        "actor_id": str(uuid4()),
        "request_id": str(uuid4()),
    }
    return service, values, pending


def test_unexpected_execution_error_is_logged_with_stack_not_message(approved_deployment):
    service, values, pending = approved_deployment
    service.submit(**values)

    def probe(path):
        raise RuntimeError("segredo-na-mensagem")

    service.backend.probe = probe
    stream = io.StringIO()
    with redirect_stderr(stream):
        pending[0].target(*pending[0].args)
    record = json.loads(stream.getvalue().strip().splitlines()[-1])
    assert record["event"] == "agent.internal_error" and record["error_class"] == "RuntimeError"
    assert "segredo-na-mensagem" not in stream.getvalue()
    operation = service.store.get("operations", values["operation_id"])
    assert (operation["state"], operation["error"]) == ("failed", "RuntimeError")


def test_failed_stage_event_references_log_name_only(approved_deployment):
    service, values, pending = approved_deployment
    service.submit(**values)

    def probe(path):
        error = AgentError("probe_failed", 409)
        error.log = "01-probe_failed.log"
        raise error

    service.backend.probe = probe
    pending[0].target(*pending[0].args)
    operation = service.store.get("operations", values["operation_id"])
    assert operation["error"] == "probe_failed"
    assert operation["events"][-1]["log"] == "01-probe_failed.log"
    public = service.operation(values["operation_id"])
    assert "events" not in public and "01-probe_failed.log" not in json.dumps(public)


def test_public_operation_never_exposes_events_or_logs(approved_deployment):
    service, values, _ = approved_deployment
    service.submit(**values)
    operation = service.store.get("operations", values["operation_id"])
    service.stage(operation, "failed", state="failed", error="migration_failed", log="03-migration_failed.log")
    rendered = json.dumps(service.operation(values["operation_id"]))
    assert "migration_failed.log" not in rendered and "events" not in rendered
