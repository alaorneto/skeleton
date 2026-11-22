"""First start of the agent (#652): configuration from the container variables and generation 0
installed from a signed release package, without adoption."""

import json
import os
import stat
from contextlib import nullcontext
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent.bootstrap import (  # noqa: E402
    BOOTSTRAP_PREFIX,
    container_config,
    install_initial,
    write_container_config,
)
from deployment_agent.service import DeploymentService  # noqa: E402
from deployment_agent.storage import AgentError, Store  # noqa: E402
from tests.unit.release_helpers import PUBLISHER, core_package, lock_for, make_wheel, private_key  # noqa: E402
from tests.unit.test_agent_release_package import LINUX, TARGET  # noqa: E402

pytestmark = pytest.mark.posix

RUNTIME = "postgres://skeleton_runtime:runtime-secret@db:5432/skeleton"
OWNER = "postgres://skeleton_owner:owner-secret@db:5432/skeleton"


def environ(tmp_path, **changes):
    values = {
        "SKELETON_AGENT_PRIVATE": str(tmp_path / "agent" / "private"),
        "SKELETON_AGENT_STATE": str(tmp_path / "agent" / "state"),
        "SKELETON_RELEASES": str(tmp_path / "releases"),
        "SKELETON_CONTROL": str(tmp_path / "control"),
        "SKELETON_CONTROL_STATE": str(tmp_path / "control-state"),
        "SKELETON_AGENT_PROJECT": str(tmp_path / "project"),
        "SKELETON_RUNTIME_DATABASE_URL": RUNTIME,
        "SKELETON_MIGRATION_DATABASE_URL": OWNER,
        "SKELETON_AGENT_PUBLISHER": PUBLISHER,
        "SKELETON_AGENT_PUBLISHER_KEY": str(tmp_path / "publisher.pub.pem"),
        "SKELETON_BUNDLED_APPS": "calendar,ledger",
        "SKELETON_ENABLE_EXAMPLE_APP": "true",
        "SKELETON_INSTANCE_DB": "/instance/instance.sqlite3",
    }
    values.update(changes)
    return values


# --------------------------------------------------------------------------- configuration


def test_the_configuration_comes_from_the_variables(tmp_path):
    values, files = container_config(environ(tmp_path, SKELETON_AGENT_SOURCE="org/repo"))
    assert values["backend"] == "directory" and values["approved_publishers"] == {
        PUBLISHER: str(tmp_path / "publisher.pub.pem")
    }
    assert values["catalogue_sources"] == [{"id": "official", "github": "org/repo", "publisher": PUBLISHER}]
    assert values["commands"]["restart"][-4:] == ["restart", "web", "--release", "{release}"]
    assert values["commands"]["worker_stop"][-3:] == ["stop", "worker", "scheduler"]
    assert "MIGRATION_DATABASE_URL" not in files["runtime.env"] and "owner-secret" not in files["runtime.env"]
    assert "owner-secret" in files["migration.env"] and "runtime-secret" not in files["migration.env"]
    for name in ("runtime.env", "migration.env", "build.env"):
        assert "SKELETON_BUNDLED_APPS=calendar,ledger" in files[name]
    assert "SKELETON_INSTANCE_DB=/instance/instance.sqlite3" in files["runtime.env"]


def test_the_official_publisher_is_the_default_and_a_fork_replaces_it(tmp_path, subtests):
    from deployment_agent.bootstrap import OFFICIAL_PUBLISHER, OFFICIAL_PUBLISHER_KEY, OFFICIAL_SOURCE

    defaults = environ(tmp_path)
    for name in ("SKELETON_AGENT_PUBLISHER", "SKELETON_AGENT_PUBLISHER_KEY"):
        del defaults[name]
    with subtests.test(case="official"):
        values, _files = container_config(defaults)
        assert values["approved_publishers"] == {OFFICIAL_PUBLISHER: OFFICIAL_PUBLISHER_KEY}
        assert values["catalogue_sources"] == [
            {"id": "official", "github": OFFICIAL_SOURCE, "publisher": OFFICIAL_PUBLISHER}
        ]
    with subtests.test(case="fork"):
        values, _files = container_config(environ(tmp_path, SKELETON_AGENT_SOURCE="fork/repo"))
        assert values["approved_publishers"] == {PUBLISHER: str(tmp_path / "publisher.pub.pem")}
        assert values["catalogue_sources"][0]["github"] == "fork/repo"
    with subtests.test(case="empty, as Compose passes an unset variable"):
        values, _files = container_config(environ(tmp_path, SKELETON_AGENT_SOURCE=""))
        assert values["catalogue_sources"][0]["github"] == OFFICIAL_SOURCE
    with subtests.test(case="no source"):
        values, _files = container_config(environ(tmp_path, SKELETON_AGENT_SOURCE="none"))
        assert "catalogue_sources" not in values


def test_missing_or_unsafe_variables_are_refused(tmp_path, subtests):
    for case, changes, code in (
        ("no owner", {"SKELETON_MIGRATION_DATABASE_URL": ""}, "missing_bootstrap_variable"),
        ("relative publisher key", {"SKELETON_AGENT_PUBLISHER_KEY": "keys/p.pem"}, "invalid_bootstrap_path"),
        ("relative path", {"SKELETON_RELEASES": "releases"}, "invalid_bootstrap_path"),
        ("shell in a value", {"SKELETON_BUNDLED_APPS": "calendar $(id)"}, "invalid_bootstrap_value"),
    ):
        with subtests.test(case=case):
            with pytest.raises(AgentError) as error:
                container_config(environ(tmp_path, **changes))
            assert error.value.code == code


def test_written_once_private_and_loadable(tmp_path):
    from deployment_agent.config import Config

    (tmp_path / "publisher.pub.pem").write_text("key")
    variables = environ(tmp_path)
    path = write_container_config(variables)
    for name in ("agent.json", "runtime.env", "migration.env", "build.env"):
        assert stat.S_IMODE((path.parent / name).stat().st_mode) == 0o600
    token = (tmp_path / "control" / "agent.token").read_text()
    assert len(token) == 64 and stat.S_IMODE((tmp_path / "control" / "agent.token").stat().st_mode) == 0o640
    before = path.read_text()
    write_container_config({**variables, "SKELETON_PUBLIC_HOST": "changed.example"})
    assert path.read_text() == before and (tmp_path / "control" / "agent.token").read_text() == token
    config = Config.load(path)
    assert config["service_control"]["control_root"] == str(tmp_path / "control" / "services")


# --------------------------------------------------------------------------- the first installation


def signed_release(tmp_path, *, environment=LINUX, publisher=PUBLISHER):
    from skeleton.system.packages import build_release

    private = tmp_path / "keys" / f"{publisher}.pem"
    private.parent.mkdir(parents=True, exist_ok=True)
    if not private.exists():
        private_key(private)
    core = core_package(tmp_path / f"core-{publisher}", private, publisher=publisher)
    wheels = [
        make_wheel(tmp_path / "w", "Django", "6.1.3", files={"django/__init__.py": ""}),
        make_wheel(tmp_path / "w", "asgiref", "3.12.1"),
    ]
    lock = lock_for(tmp_path / f"lock-{publisher}.txt", wheels)
    target = {"id": "cp314-linux-x86_64", "environment": environment, "lock": lock, "wheels": wheels}
    return build_release(core, tmp_path / f"release-{publisher}.skapp", private, targets=[target])


class Backend:
    """A fake backend: records the management commands, never runs pip or Django."""

    def __init__(self, config, *, fail_on=None):
        self.config = config
        self.commands = []
        self.fail_on = fail_on

    def logging_to(self, operation_id):
        return nullcontext()

    def target_environment(self):
        return dict(TARGET)

    def prepare(self, release):
        (Path(release) / ".venv" / "bin").mkdir(parents=True)

    def manage(self, release, arguments, profile, *, code="management_command_failed"):
        self.commands.append(([str(argument) for argument in arguments][0], profile))
        if self.fail_on and arguments[0] == self.fail_on:
            raise AgentError(code, 409)

    def export_infrastructure(self, release, destination):
        Path(destination).write_text("{}")

    def cutover(self, release):
        active = Path(self.config["active_link"])
        active.symlink_to(release, target_is_directory=True)


def service_for(tmp_path, **backend):
    (tmp_path / "keys").mkdir(parents=True, exist_ok=True)
    _private, public = private_key(tmp_path / "keys" / f"{PUBLISHER}.pem")
    project = tmp_path / "project"
    (project / "config").mkdir(parents=True, exist_ok=True)
    (project / "manage.py").write_text("")
    (tmp_path / "wheelhouse").mkdir(exist_ok=True)
    (tmp_path / "operator.lock").write_text("")
    service = DeploymentService.__new__(DeploymentService)
    service.store = Store(tmp_path / "journal")
    service.worker = None
    service._drift = None
    service.config = {
        "storage_root": str(service.store.root),
        "releases_root": str(tmp_path / "releases"),
        "active_link": str(tmp_path / "releases" / "current"),
        "project_root": str(project),
        "project_paths": ["manage.py", "config"],
        "requirements_lock": str(tmp_path / "operator.lock"),
        "wheelhouse": str(tmp_path / "wheelhouse"),
        "approved_publishers": {PUBLISHER: public},
        "max_upload_bytes": 64 * 1024 * 1024,
    }
    service.backend = Backend(service.config, **backend)
    return service


def test_install_from_a_signed_release_then_nothing_more(tmp_path):
    service = service_for(tmp_path)
    package = str(signed_release(tmp_path))
    result = install_initial(service, release_file=package)
    assert result["state"] == "installed" and result["generation"] == 0
    assert result["core"]["origin"] == "installed" and result["core"]["version"] == "0.6.0"
    state = service.state()
    active = Path(service.config["active_link"])
    assert active.is_symlink() and str(active.resolve()) == state["release"]
    assert state["inventory"]["core"]["digest"] == result["core"]["digest"]
    assert Path(state["release"]).name.startswith(BOOTSTRAP_PREFIX)
    assert service.backend.commands[:4] == [
        ("check", "build"),
        ("collectstatic", "build"),
        ("migrate", "migration"),
        ("sync_table_catalog", "migration"),
    ]
    assert ("check_skeleton_schema", "migration") in service.backend.commands
    assert ("sync_deployment_state", "migration") in service.backend.commands
    inventory_text = (Path(state["release"]) / "inventory.json").read_text()
    assert "owner-secret" not in inventory_text and "MIGRATION_DATABASE_URL" not in inventory_text
    count = len(service.backend.commands)
    again = install_initial(service, release_file=package)
    assert again == {"state": "installed", "generation": 0} and len(service.backend.commands) == count


def test_an_interrupted_first_start_leaves_no_active_pointer_and_starts_again(tmp_path):
    service = service_for(tmp_path, fail_on="migrate")
    package = str(signed_release(tmp_path))
    with pytest.raises(AgentError) as error:
        install_initial(service, release_file=package)
    assert error.value.code == "migration_failed"
    active = Path(service.config["active_link"])
    assert not active.exists() and not active.is_symlink()
    assert not (service.store.root / "active.json").exists()
    assert list((tmp_path / "releases").glob(f"{BOOTSTRAP_PREFIX}-*"))
    service.backend.fail_on = None
    assert install_initial(service, release_file=package)["state"] == "installed"
    left = [
        path
        for path in (tmp_path / "releases").glob(f"{BOOTSTRAP_PREFIX}-*")
        if path.is_dir() and not path.is_symlink()
    ]
    assert len(left) == 1  # The partial release was removed before the new one.


def test_untrusted_or_unusable_packages_are_refused_before_any_migration(tmp_path, subtests):
    service = service_for(tmp_path)
    with subtests.test(case="publisher not approved"):
        with pytest.raises(AgentError) as error:
            install_initial(service, release_file=str(signed_release(tmp_path, publisher="other-publisher")))
        assert error.value.code == "package_verification_failed"
    with subtests.test(case="tampered package"):
        tampered = tmp_path / "tampered.skapp"
        data = bytearray(Path(signed_release(tmp_path)).read_bytes())
        data[len(data) // 2] ^= 0xFF
        tampered.write_bytes(bytes(data))
        with pytest.raises(AgentError):
            install_initial(service, release_file=str(tampered))
    with subtests.test(case="no target for this server"):
        arm = {**LINUX, "platform_machine": "aarch64"}
        other = service_for(tmp_path / "arm")
        with pytest.raises(AgentError) as error:
            install_initial(other, release_file=str(signed_release(tmp_path / "arm", environment=arm)))
        assert error.value.code == "release_target_unavailable"
    with subtests.test(case="a component package is not a release package"):
        private = tmp_path / "keys" / f"{PUBLISHER}.pem"
        core = core_package(tmp_path / "plain", private)
        with pytest.raises(AgentError) as error:
            install_initial(service, release_file=str(core))
        assert error.value.code == "release_package_required"
    assert ("migrate", "migration") not in service.backend.commands
    assert not Path(service.config["active_link"]).is_symlink()


def test_an_existing_active_pointer_is_never_overwritten(tmp_path):
    service = service_for(tmp_path)
    (tmp_path / "releases").mkdir()
    (tmp_path / "releases" / "current").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(AgentError) as error:
        install_initial(service, release_file=str(signed_release(tmp_path)))
    assert error.value.code == "initial_active_pointer_conflict"


def test_the_cli_offers_bootstrap():
    from deployment_agent.__main__ import build_parser

    options = build_parser().parse_args(["--config", "/x.json", "bootstrap", "--version", "0.6.0"])
    assert (options.command, options.version, options.release_file) == ("bootstrap", "0.6.0", None)
    assert json.dumps(os.environ.get("NOTHING", "")) == '""'
