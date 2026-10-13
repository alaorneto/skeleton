"""Actual isolated release execution, cutover, backups and fixed operator hooks."""

import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
from urllib.parse import unquote, urlsplit
from uuid import uuid4

from .storage import AgentError, atomic_bytes, atomic_json, file_hash, inventory_files, load_json
from .config import read_environment


class Backend:
    def __init__(self, config, store):
        self.config = config
        self.store = store

    def run(self, arguments, *, env=None, cwd=None, code="command_failed"):
        try:
            subprocess.run([str(argument) for argument in arguments], env=env or self.operator_env(), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=self.config.get("command_timeout", 1800))
        except (OSError, subprocess.SubprocessError):
            raise AgentError(code, 409) from None

    def capture(self, arguments, *, env=None, cwd=None, code="command_failed"):
        try:
            result = subprocess.run([str(argument) for argument in arguments], env=env or self.operator_env(), cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True, timeout=self.config.get("command_timeout", 1800))
            if len(result.stdout) > 16 * 1024 * 1024:
                raise AgentError("command_output_too_large", 409)
            return result.stdout
        except (OSError, subprocess.SubprocessError):
            raise AgentError(code, 409) from None

    def target_environment(self):
        script = "import json,os,platform,sys; print(json.dumps(dict(implementation_name=sys.implementation.name,implementation_version=platform.python_version(),os_name=os.name,platform_machine=platform.machine(),platform_release=platform.release(),platform_system=platform.system(),platform_version=platform.version(),python_full_version=platform.python_version(),platform_python_implementation=platform.python_implementation(),python_version='.'.join(map(str,sys.version_info[:2])),sys_platform=sys.platform)))"
        return json.loads(self.capture([self.config.get("python", sys.executable), "-c", script], code="target_python_unavailable"))

    def operator_env(self):
        return {"PATH": self.config.get("operator_path", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8", "HOME": self.config["storage_root"]}

    def hook(self, name, release, operation=None):
        replacements = {"{release}": str(release), "{active}": self.config["active_link"], "{operation}": str(operation or "")}
        arguments = self.config["commands"][name]
        for old, new in replacements.items():
            arguments = [argument.replace(old, new) for argument in arguments]
        self.run(arguments, code=f"{name}_failed")

    def prepare(self, release):
        self.run([self.config.get("python", sys.executable), "-m", "venv", release / ".venv"], code="venv_failed")
        self.run([release / ".venv/bin/python", "-m", "pip", "install", "--disable-pip-version-check", "--no-index", "--only-binary=:all:", "--find-links", release / "wheels", "--require-hashes", "-r", release / "requirements.lock"], code="dependencies_failed")
        self.run([release / ".venv/bin/python", "-m", "pip", "check"], code="dependency_conflict")

    def python(self, release, arguments, profile, *, code="release_command_failed"):
        self.run([Path(release) / ".venv/bin/python", *arguments], env=self.config.environment(profile, release), cwd=release, code=code)

    def manage(self, release, arguments, profile, *, code="management_command_failed"):
        self.python(release, ["manage.py", *arguments], profile, code=code)

    def probe(self, release):
        path = self.store.root / f"probe-{uuid4()}.json"
        try:
            # Keep sys.path rooted in the release, not beside the agent's config.py.
            runner = "import runpy,sys; script=sys.argv.pop(1); runpy.run_path(script, run_name='__main__')"
            self.python(release, ["-c", runner, Path(__file__).with_name("probe.py"), path], "migration", code="migration_graph_failed")
            return load_json(path)
        finally:
            path.unlink(missing_ok=True)

    def export_schema(self, release, app_label, destination):
        temporary = self.store.root / "probe" / f"schema-{uuid4()}.json"
        temporary.parent.mkdir(exist_ok=True, mode=0o700)
        try:
            self.manage(release, ["export_record_schema", "--app-label", app_label, "--output", temporary], "runtime", code="schema_export_failed")
            atomic_bytes(destination, temporary.read_bytes(), 0o644)
        finally:
            temporary.unlink(missing_ok=True)

    def export_infrastructure(self, release, destination):
        temporary = self.store.root / "probe" / f"infrastructure-{uuid4()}.json"
        temporary.parent.mkdir(exist_ok=True, mode=0o700)
        try:
            self.manage(release, ["export_infrastructure_inventory", "--output", temporary], "migration", code="infrastructure_export_failed")
            atomic_bytes(destination, temporary.read_bytes(), 0o644)
        finally:
            temporary.unlink(missing_ok=True)

    def active(self):
        path = Path(self.config["active_link"])
        if not path.is_symlink():
            raise AgentError("active_release_missing", 409)
        return path.resolve(strict=True)

    def cutover(self, release):
        active = Path(self.config["active_link"])
        temporary = active.with_name(f".{active.name}.{uuid4()}")
        temporary.symlink_to(release, target_is_directory=True)
        os.replace(temporary, active)
        descriptor = os.open(active.parent, os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def start(self, release, operation):
        self.hook("restart", release, operation)

    def drain(self, release, operation):
        self.hook("drain", release, operation)

    def stop(self, release, operation):
        self.hook("stop", release, operation)

    def pg_environment(self, release):
        result = self.operator_env()
        if self.config.get("backup_env_file"):
            url = urlsplit(read_environment(self.config["backup_env_file"])["MIGRATION_DATABASE_URL"])
        else:
            url = urlsplit(self.config.environment("migration", release)["MIGRATION_DATABASE_URL"])
        result.update({"PGHOST": url.hostname or "", "PGPORT": str(url.port or 5432), "PGDATABASE": unquote(url.path.lstrip("/")), "PGUSER": unquote(url.username or ""), "PGPASSWORD": unquote(url.password or "")})
        from urllib.parse import parse_qs
        allowed = {"sslmode": "PGSSLMODE", "sslrootcert": "PGSSLROOTCERT", "sslcert": "PGSSLCERT", "sslkey": "PGSSLKEY", "options": "PGOPTIONS"}
        for key, values in parse_qs(url.query).items():
            if key not in allowed or len(values) != 1:
                raise AgentError("unsupported_backup_database_option")
            result[allowed[key]] = values[0]
        return result

    def sqlite_path(self, release):
        url = self.config.environment("migration", release)["MIGRATION_DATABASE_URL"]
        if not url.startswith("sqlite:////"):
            raise AgentError("sqlite_requires_absolute_path")
        return Path(unquote(url[len("sqlite:///"):].split("?", 1)[0]))

    def backup(self, release, operation):
        root = self.store.root / "backups" / operation
        if root.exists():
            # Preserve a partial snapshot left by a crash instead of merging or deleting it.
            root = root.with_name(f"{operation}-{uuid4()}")
        root.mkdir(mode=0o700, exist_ok=False)
        url = urlsplit(self.config.environment("migration", release)["MIGRATION_DATABASE_URL"])
        if url.scheme == "sqlite":
            database = root / "database.sqlite3"
            with sqlite3.connect(self.sqlite_path(release)) as source, sqlite3.connect(database) as target:
                source.backup(target)
                if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise AgentError("backup_invalid", 409)
        else:
            database = root / "database.dump"
            self.run([self.config.get("pg_dump", "/usr/bin/pg_dump"), "--format=custom", "--schema=skeleton", "--file", database], env=self.pg_environment(release), code="backup_failed")
            self.run([self.config.get("pg_restore", "/usr/bin/pg_restore"), "--list", database], code="backup_invalid")
        if not database.is_file() or database.stat().st_size == 0:
            raise AgentError("backup_invalid", 409)
        resources = []
        for index, source in enumerate(self.config.get("recovery_paths", [])):
            source = Path(source)
            if not source.is_absolute() or source.is_symlink() or not source.exists():
                raise AgentError("invalid_recovery_path", 409)
            target = root / "resources" / str(index)
            target.parent.mkdir(exist_ok=True, mode=0o700)
            if source.is_dir():
                if any(path.is_symlink() for path in source.rglob("*")):
                    raise AgentError("recovery_path_symlink", 409)
                shutil.copytree(source, target)
                hashes = inventory_files(target)
            else:
                shutil.copy2(source, target)
                hashes = {".": {"sha256": file_hash(target)}}
            resources.append({"path": str(source), "snapshot": str(target), "directory": source.is_dir(), "files": hashes})
        metadata = {"id": operation, "database": str(database), "database_sha256": file_hash(database), "database_kind": url.scheme, "resources": resources, "previous_release": str(release)}
        atomic_json(root / "backup.json", metadata)
        for path in root.rglob("*"):
            os.chmod(path, 0o700 if path.is_dir() else 0o600)
        return str(root / "backup.json")

    def restore(self, backup, release):
        metadata = load_json(backup)
        database = Path(metadata["database"])
        if file_hash(database) != metadata["database_sha256"]:
            raise AgentError("backup_hash_conflict", 409)
        for resource in metadata["resources"]:
            target = Path(resource["snapshot"])
            hashes = inventory_files(target) if resource["directory"] else {".": {"sha256": file_hash(target)}}
            if hashes != resource["files"]:
                raise AgentError("backup_hash_conflict", 409)
        if metadata["database_kind"] == "sqlite":
            with sqlite3.connect(database) as source, sqlite3.connect(self.sqlite_path(release)) as target:
                source.backup(target)
        else:
            # Objects introduced after the snapshot are absent from its DROP list.
            self.run([self.config.get("psql", "/usr/bin/psql"), "--no-psqlrc", "--set", "ON_ERROR_STOP=1", "--command", "DROP SCHEMA IF EXISTS skeleton CASCADE"], env=self.pg_environment(release), code="restore_reset_failed")
            self.run([self.config.get("pg_restore", "/usr/bin/pg_restore"), "--clean", "--if-exists", "--exit-on-error", "--no-owner", "--dbname", self.pg_environment(release)["PGDATABASE"], database], env=self.pg_environment(release), code="restore_failed")
        for resource in metadata["resources"]:
            source, target = Path(resource["snapshot"]), Path(resource["path"])
            if resource["directory"]:
                # Writes are drained; replace exact backed-up trees, not a merge that leaves new files.
                displaced = target.with_name(f".{target.name}.recovery-{uuid4()}")
                restored = target.with_name(f".{target.name}.restore-{uuid4()}")
                shutil.copytree(source, restored)
                if target.exists():
                    os.replace(target, displaced)
                os.replace(restored, target)
                # The displaced resource is durable recovery evidence, not silently deleted.
            else:
                atomic_bytes(target, source.read_bytes(), source.stat().st_mode & 0o777)


class ComposeBackend(Backend):
    def image(self, release):
        return f"{self.config['compose'].get('image_repository', 'skeleton-managed')}:{Path(release).name}"

    def base_arguments(self):
        compose = self.config["compose"]
        return [self.config.get("docker", "/usr/bin/docker"), "compose", "--project-name", compose["project"], "--env-file", compose["env_file"], "--file", compose["file"]]

    def base(self, release, profile="runtime"):
        return [*self.base_arguments(), "--file", self.override(release, profile)]

    def override(self, release, profile):
        compose = self.config["compose"]
        release = Path(release)
        path = self.store.root / f"compose-{release.name}-{profile}.json"
        env = self.config.environment("runtime" if profile == "export" else profile, release)
        service = compose["migrator_service"] if profile == "migration" else compose["web_service"]
        # Compose merges environment mappings. Explicitly unset every inherited key
        # before applying the selected private profile, including env_file entries.
        rendered = json.loads(self.capture([*self.base_arguments(), "config", "--format", "json"], code="compose_configuration_failed"))
        inherited = rendered["services"][service].get("environment", {})
        env = {**{key: None for key in inherited}, **env}
        env["PATH"] = "/opt/venv/bin:/usr/local/bin:/usr/bin:/bin"
        volumes = [{"type": "bind", "source": str(release), "target": str(release), "read_only": profile != "build"}]
        marker_parent = str(Path(self.config["maintenance_marker"]).parent)
        volumes.append({"type": "bind", "source": marker_parent, "target": marker_parent, "read_only": True})
        if profile in ("migration", "export"):
            probe = self.store.root / "probe"
            probe.mkdir(exist_ok=True, mode=0o700)
            volumes.append({"type": "bind", "source": str(probe), "target": str(probe), "read_only": False})
        if profile == "migration":
            operations = self.store.root / "operations"
            volumes.append({"type": "bind", "source": str(operations), "target": str(operations), "read_only": True})
        specification = {"image": self.image(release), "working_dir": str(release), "environment": env, "volumes": volumes, "read_only": profile != "build", "user": str(compose.get("runtime_uid", 10001))}
        if profile in ("migration", "export", "build"):
            specification["entrypoint"] = []
            specification["user"] = str(compose.get("migration_uid", os.getuid()))
        atomic_json(path, {"services": {service: specification}})
        return path

    def build_image(self, release):
        self.run([self.config.get("docker", "/usr/bin/docker"), "build", "--network=none", "--build-arg", f"RELEASE_BASE={self.config['compose']['base_image']}", "--file", self.config["compose"]["dockerfile"], "--tag", self.image(release), release], code="image_build_failed")

    def prepare(self, release):
        # Preview executes the same environment that will run the final Compose migrator/web.
        self.build_image(release)
        actual = json.loads(self.capture([self.config.get("docker", "/usr/bin/docker"), "run", "--rm", "--network=none", "--entrypoint", "/opt/venv/bin/python", self.image(release), "-c", "import json; from packaging.markers import default_environment; print(json.dumps(default_environment()))"], code="image_environment_failed"))
        expected = self.target_environment()
        if any(actual[key] != expected[key] for key in ("python_full_version", "platform_machine", "implementation_name", "sys_platform")):
            raise AgentError("image_target_environment_mismatch", 409)

    def python(self, release, arguments, profile, *, code="release_command_failed"):
        compose = self.config["compose"]
        service = compose["migrator_service"] if profile == "migration" else compose["web_service"]
        # No secrets on argv. The private generated override contains profile-specific environment.
        self.run([*self.base(release, profile), "run", "--rm", "--no-deps", "--entrypoint", "/opt/venv/bin/python", service, *arguments], code=code)

    def probe(self, release):
        temporary = self.store.root / "probe"
        temporary.mkdir(exist_ok=True, mode=0o700)
        script = temporary / "probe.py"
        atomic_bytes(script, Path(__file__).with_name("probe.py").read_bytes())
        path = temporary / f"{uuid4()}.json"
        try:
            self.python(release, [script, path], "migration", code="migration_graph_failed")
            return load_json(path)
        finally:
            path.unlink(missing_ok=True)

    def target_environment(self):
        from packaging.markers import default_environment
        supplied = self.config["compose"].get("target_environment")
        if not isinstance(supplied, dict) or not all(key in supplied for key in ("python_full_version", "python_version", "platform_machine", "sys_platform")):
            raise AgentError("compose_target_environment_required")
        return {**default_environment(), **supplied}

    def export_schema(self, release, app_label, destination):
        temporary = self.store.root / "probe" / f"schema-{uuid4()}.json"
        temporary.parent.mkdir(exist_ok=True, mode=0o700)
        try:
            self.manage(release, ["export_record_schema", "--app-label", app_label, "--output", temporary], "export", code="schema_export_failed")
            atomic_bytes(destination, temporary.read_bytes(), 0o644)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, release, operation):
        rendered = json.loads(self.capture([*self.base(release), "config", "--format", "json"], code="compose_configuration_failed"))
        environment = rendered["services"][self.config["compose"]["web_service"]].get("environment", {})
        if environment.get("MIGRATION_DATABASE_URL"):
            raise AgentError("migration_secret_in_runtime", 409)
        self.run([*self.base(release), "up", "--detach", "--no-deps", "--no-build", "--force-recreate", self.config["compose"]["web_service"]], code="compose_restart_failed")
        self.hook("restart", release, operation)

    def drain(self, release, operation):
        self.hook("drain", release, operation)
        self.stop(release, operation)

    def stop(self, release, operation):
        self.run([*self.base(release), "stop", self.config["compose"]["web_service"]], code="compose_stop_failed")
        self.hook("stop", release, operation)


def backend_for(config, store):
    return ComposeBackend(config, store) if config["backend"] == "compose" else Backend(config, store)
