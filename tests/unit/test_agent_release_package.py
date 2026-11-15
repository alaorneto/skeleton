"""Release packages in the agent (#495): upload, publisher environment and the locked build."""

import json
import os
from pathlib import Path

import pytest

pytest.importorskip("fcntl", reason="O agente de deployment exige fcntl (Linux ou macOS).")

from deployment_agent import environments  # noqa: E402
from deployment_agent.planning import dropped_dependencies  # noqa: E402
from deployment_agent.releases import build_lock  # noqa: E402
from deployment_agent.storage import AgentError  # noqa: E402
from skeleton.system.packages import build_release  # noqa: E402
from tests.unit.agent_helpers import bare_service  # noqa: E402
from tests.unit.release_helpers import (  # noqa: E402
    ARM,
    LINUX,
    PUBLISHER,
    core_package,
    lock_for,
    make_wheel,
    private_key,
    read_archive,
    write_archive,
)

pytestmark = pytest.mark.posix
TARGET = {**LINUX, "python_full_version": "3.14.0", "implementation_version": "3.14.0"}


def publisher_release(tmp_path, *, name="release.skapp", wheels=None, core=None, environment=LINUX):
    """A release package whose target pins Django and asgiref, signed like its core."""
    private = tmp_path / "publisher.pem"
    if not private.exists():
        private_key(private)
    if core is None:
        core = core_package(tmp_path, private)
    if wheels is None:
        wheels = [
            make_wheel(tmp_path / "w", "Django", "6.1.3", files={"django/__init__.py": ""}),
            make_wheel(tmp_path / "w", "asgiref", "3.12.1"),
        ]
    lock = lock_for(tmp_path / f"{name}.lock.txt", wheels)
    target = {"id": "cp314-linux-x86_64", "environment": environment, "lock": lock, "wheels": wheels}
    path = build_release(core, tmp_path / name, private, targets=[target])
    return path, core


@pytest.fixture
def agent(tmp_path):
    service = bare_service(tmp_path)
    _, public = private_key(tmp_path / "publisher.pem")
    service.config["approved_publishers"] = {PUBLISHER: public}
    service.backend.target_environment = lambda: dict(TARGET)
    return service


def upload(service, path):
    return service.upload(Path(path).read_bytes())


def uploads(service):
    return sorted(path.name for path in (service.store.root / "uploads").iterdir())


def test_upload_keeps_release_and_core_and_records_the_environment(agent, tmp_path):
    release, core = publisher_release(tmp_path)
    result = upload(agent, release)
    assert result["kind"] == "release" and result["target"] == "cp314-linux-x86_64"
    assert result["manifest"]["kind"] == "release" and result["core"]["manifest"]["kind"] == "core"
    assert result["agent"] == {"version": None}
    names = uploads(agent)
    assert names == sorted([f"{result['digest']}.skapp", f"{result['core']['digest']}.skapp"])
    for name in names:
        assert (agent.store.root / "uploads" / name).stat().st_mode & 0o777 == 0o400
    recorded = json.loads((agent.store.root / "environments" / f"{result['core']['digest']}.json").read_text())
    assert recorded["release_digest"] == result["digest"] and recorded["target"] == "cp314-linux-x86_64"
    assert upload(agent, release) == result


def test_component_upload_reports_its_kind(agent, tmp_path):
    (tmp_path / "c").mkdir()
    result = upload(agent, core_package(tmp_path / "c", tmp_path / "publisher.pem"))
    assert result["kind"] == "core" and set(result) == {"digest", "kind", "manifest"}


def test_unverifiable_release_packages_leave_nothing(agent, tmp_path):
    release, _ = publisher_release(tmp_path)
    contents = read_archive(release)
    member = next(name for name in contents if name.startswith("locks/"))
    tampered = write_archive(tmp_path / "tampered.skapp", {**contents, member: contents[member] + b"\n"})
    other_dir = tmp_path / "other"
    other_dir.mkdir()
    foreign, _ = publisher_release(other_dir)  # Its own, unapproved key.
    for path in (tampered, foreign):
        with pytest.raises(AgentError, match="package_verification_failed"):
            upload(agent, path)
    assert uploads(agent) == []
    assert not (agent.store.root / "environments").exists()


def test_target_outside_the_package_records_no_environment(agent, tmp_path):
    release, _ = publisher_release(tmp_path, environment=ARM)
    result = upload(agent, release)
    assert result["target"] is None
    assert not (agent.store.root / "environments").exists()
    assert len(uploads(agent)) == 2


def test_unknown_target_environment_does_not_fail_the_upload(agent, tmp_path):
    def unavailable():
        raise AgentError("target_python_unavailable")

    agent.backend.target_environment = unavailable
    assert upload(agent, publisher_release(tmp_path)[0])["target"] is None


def test_same_core_with_other_dependencies_conflicts(agent, tmp_path):
    first, core = publisher_release(tmp_path)
    upload(agent, first)
    other = [make_wheel(tmp_path / "w2", "Django", "6.1.3", files={"django/__init__.py": "X = 1\n"})]
    second, _ = publisher_release(tmp_path, name="second.skapp", wheels=other, core=core)
    before = uploads(agent)
    with pytest.raises(AgentError) as raised:
        upload(agent, second)
    assert (raised.value.code, raised.value.status) == ("release_environment_conflict", 409)
    assert uploads(agent) == before


def config(tmp_path, **values):
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    lock = tmp_path / "operator.lock"
    lock.write_text("")
    return {"requirements_lock": str(lock), "wheelhouse": str(wheelhouse), **values}


def core_release_dir(tmp_path, agent, result, name="release"):
    """A release directory holding the core wheel, as _install_core_wheels leaves it."""
    release = tmp_path / name
    (release / "wheels").mkdir(parents=True)
    from zipfile import ZipFile

    core = result["core"]["manifest"]
    member = core["python_dependencies"][0]["wheel"]
    with ZipFile(agent.store.path("uploads", result["core"]["digest"])) as archive:
        (release / member).write_bytes(archive.read(member))
    return release, core


def test_build_lock_uses_the_publisher_environment_with_an_empty_wheelhouse(agent, tmp_path):
    result = upload(agent, publisher_release(tmp_path)[0])
    settings = {**agent.config, **config(tmp_path)}
    for name in ("first", "second"):  # A later plan without a new release package still uses it.
        release, core = core_release_dir(tmp_path, agent, result, name)
        rows = build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
        assert sorted(rows) == ["asgiref", "django", "django-skeleton"]
        lines = (release / "requirements.lock").read_text().splitlines()
        assert [line.split("==")[0] for line in lines] == ["asgiref", "django", "django-skeleton"]
        assert not (release / ".publisher-environment").exists()


def test_build_lock_without_environment_keeps_the_operator_lock(agent, tmp_path):
    result = upload(agent, publisher_release(tmp_path, environment=ARM)[0])
    release, core = core_release_dir(tmp_path, agent, result)
    settings = {**agent.config, **config(tmp_path)}
    with pytest.raises(AgentError, match="missing_core_dependencies"):
        build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
    Path(settings["requirements_lock"]).write_text(f"django==6.1.3 --hash=sha256:{'a' * 64}\n")
    with pytest.raises(AgentError, match="locked_wheel_unavailable"):
        build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
    with pytest.raises(AgentError, match="locked_wheel_unavailable"):
        build_lock(settings, release, [core], TARGET)  # No store: today's behaviour.


def test_project_lock_adds_pins_and_conflicts_on_versions(agent, tmp_path):
    result = upload(agent, publisher_release(tmp_path)[0])
    extra = make_wheel(tmp_path / "project", "icalendar", "6.1.0")
    project = lock_for(tmp_path / "project.lock", [extra])
    settings = {**agent.config, **config(tmp_path, project_requirements_lock=str(project))}
    (Path(settings["wheelhouse"]) / extra.name).write_bytes(extra.read_bytes())
    release, core = core_release_dir(tmp_path, agent, result)
    rows = build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
    assert "icalendar" in rows
    conflicting = make_wheel(tmp_path / "project2", "asgiref", "3.13.0")
    settings["project_requirements_lock"] = str(lock_for(tmp_path / "conflict.lock", [conflicting]))
    release, core = core_release_dir(tmp_path, agent, result, "conflict")
    with pytest.raises(AgentError, match="dependency_version_conflict"):
        build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
    assert not (release / ".publisher-environment").exists()


def test_changed_or_missing_release_package_is_never_used(agent, tmp_path):
    result = upload(agent, publisher_release(tmp_path)[0])
    stored = agent.store.path("uploads", result["digest"])
    os.chmod(stored, 0o600)
    stored.write_bytes(stored.read_bytes() + b"\0")
    settings = {**agent.config, **config(tmp_path)}
    release, core = core_release_dir(tmp_path, agent, result)
    with pytest.raises(AgentError, match="package_verification_failed"):
        build_lock(settings, release, [core], TARGET, store=agent.store, core_digest=result["core"]["digest"])
    stored.unlink()
    with pytest.raises(AgentError, match="package_verification_failed"):
        environments.publisher_environment(settings, agent.store, result["core"]["digest"], TARGET)
    assert not (release / ".publisher-environment").exists()


def test_publisher_environment_materializes_only_file_names(agent, tmp_path):
    result = upload(agent, publisher_release(tmp_path)[0])
    environment = environments.publisher_environment(agent.config, agent.store, result["core"]["digest"], TARGET)
    lock = environment.materialize(tmp_path / "staging")
    assert lock == tmp_path / "staging" / "requirements.lock"
    assert sorted(path.name for path in (tmp_path / "staging" / "wheels").iterdir()) == [
        "asgiref-3.12.1-py3-none-any.whl",
        "django-6.1.3-py3-none-any.whl",
    ]
    assert environments.publisher_environment(agent.config, agent.store, result["core"]["digest"], ARM) is None
    assert environments.publisher_environment(agent.config, agent.store, "0" * 64, TARGET) is None


def test_dropped_dependencies(tmp_path):
    active, release = tmp_path / "active", tmp_path / "release"
    active.mkdir()
    release.mkdir()
    (release / "requirements.lock").write_text(f"django==6.1.3 --hash=sha256:{'a' * 64}\n")
    assert dropped_dependencies(active, release) == []  # No active lock: nothing dropped.
    (active / "requirements.lock").write_text(
        f"django==6.1.3 --hash=sha256:{'a' * 64}\nredis==5.0.0 --hash=sha256:{'b' * 64}\n"
        f"icalendar==6.1.0 --hash=sha256:{'c' * 64}\n"
    )
    assert dropped_dependencies(active, release) == ["icalendar", "redis"]


def test_upload_never_reaches_the_network(agent, tmp_path, monkeypatch):
    from deployment_agent import catalogue

    monkeypatch.setattr(catalogue, "build_opener", lambda *handlers: pytest.fail("Network was used."))
    assert upload(agent, publisher_release(tmp_path)[0])["kind"] == "release"


def test_dependency_check_reads_django_from_the_publisher_environment(agent, tmp_path):
    from deployment_agent.releases import target_django_version

    result = upload(agent, publisher_release(tmp_path)[0])
    settings = {**agent.config, **config(tmp_path)}  # The operator lock is empty.
    core = result["core"]["manifest"]
    digest = result["core"]["digest"]
    assert target_django_version(settings, [core], TARGET, store=agent.store, core_digest=digest) == "6.1.3"
    with pytest.raises(AgentError, match="missing_core_dependencies"):
        target_django_version(settings, [core], TARGET)
