"""Real signed artifacts, closed ownership, offline trust and release rollback tests."""
from base64 import b64encode
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import io
import json
from pathlib import Path
import stat
import subprocess
import sys
import zipfile

import pytest

from cryptography.hazmat.primitives import serialization
from packaging.utils import canonicalize_name, parse_wheel_filename
from skeleton.system.packages import (
    PackageError, build_artifact, build_catalog, build_package, canonical_json,
    catalog_entry, extract_package, initialize_keys, inspect_package, load_manifest,
    manifest_template, sign_package, validate_catalog, validate_dependency_graph,
    validate_manifest, validate_package, validate_wheel_set,
)
from skeleton.versioning import normalize_version, version_satisfies


@pytest.fixture
def signed_app(tmp_path):
    root = tmp_path / 'source'
    root.mkdir()
    (root / '__init__.py').write_text('')
    (root / 'apps.py').write_text('from django.apps import AppConfig\nclass DemoConfig(AppConfig):\n    name = __package__\n    label = "project_demo"\n')
    (root / 'identity.py').write_text('VALUE = "verified app"\n')
    (root / 'docs').mkdir()
    (root / 'docs/spec.md').write_text('## Contract\nOwn directory only.\n## Acceptance\nSigned package import.\n')
    (root / 'docs/adr.md').write_text('## Context\nRelease artifacts.\n## Decision\nSigned.\n## Consequences\nPrivileged code.\n')
    manifest = manifest_template(app_id='org.test.demo', app_label='project_demo', package_name='package_demo', publisher='test-publisher', app_config_class='DemoConfig')
    manifest_path = root / 'manifest.json'
    manifest_path.write_bytes(canonical_json(manifest))
    private, public = initialize_keys(tmp_path / 'publisher-private.pem', tmp_path / 'publisher-public.pem')
    package = build_package(root, manifest_path, tmp_path / 'demo.skapp', private)
    return {'root': root, 'manifest': manifest_path, 'private': private, 'public': public, 'package': package, 'trust': {'test-publisher': public}}


def rewrite(source, destination, mutate):
    with zipfile.ZipFile(source) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    entries = mutate(entries)
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for info, data in entries:
            archive.writestr(info, data)
    return destination


def wheel_file(root, name='dependency', version='1.0.0', *, files=None, entry_points=None, metadata=''):
    root.mkdir(parents=True, exist_ok=True)
    distribution = canonicalize_name(name).replace('-', '_')
    wheel = root / f'{distribution}-{version}-py3-none-any.whl'
    dist_info = f'{distribution}-{version}.dist-info'
    with zipfile.ZipFile(wheel, 'w') as archive:
        for path, data in (files if files is not None else {distribution + '/__init__.py': ''}).items():
            archive.writestr(path, data)
        archive.writestr(dist_info + '/METADATA', f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n{metadata}')
        archive.writestr(dist_info + '/WHEEL', 'Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        if entry_points is not None:
            archive.writestr(dist_info + '/entry_points.txt', entry_points)
    return wheel


def wheel_dependency(wheel, *, marker=None):
    name, version, _, _ = parse_wheel_filename(wheel.name)
    dependency = {'name': name, 'version': str(version), 'wheel': 'wheels/' + wheel.name, 'sha256': hashlib.sha256(wheel.read_bytes()).hexdigest()}
    if marker is not None:
        dependency['marker'] = marker
    return dependency


def write_signed_archive(manifest, contents, output, private):
    """Independent trusted publisher bypassing the safety-enforcing builder."""
    manifest = json.loads(canonical_json(manifest))
    roles = {item['path']: item['role'] for item in manifest['files']}
    manifest['files'] = [{'path': path, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'role': roles.get(path, 'wheel')} for path, data in sorted(contents.items())]
    raw = canonical_json(manifest)
    key = serialization.load_pem_private_key(private.read_bytes(), password=None)
    with zipfile.ZipFile(output, 'x') as archive:
        archive.writestr('manifest.json', raw)
        archive.writestr('manifest.sig', b64encode(key.sign(raw)))
        for path, data in contents.items():
            archive.writestr(path, data)
    return output


def signed_wheel_archive(signed_app, output, wheels):
    manifest = load_manifest(signed_app['manifest'])
    with zipfile.ZipFile(signed_app['package']) as archive:
        contents = {item['path']: archive.read(item['path']) for item in validate_package(signed_app['package'], signed_app['trust']).manifest['files']}
    manifest['python_dependencies'] = [wheel_dependency(wheel) for wheel in wheels]
    contents.update({'wheels/' + wheel.name: wheel.read_bytes() for wheel in wheels})
    # Preserve existing app roles while generating the added wheel inventory.
    manifest['files'] = validate_package(signed_app['package'], signed_app['trust']).manifest['files']
    return write_signed_archive(manifest, contents, output, signed_app['private'])


def core_manifest(signed_app):
    manifest = load_manifest(signed_app['manifest'])
    for key in ('app_id', 'app_label', 'package_name', 'app_config', 'runtime'):
        manifest.pop(key)
    manifest.update(kind='core', component_id='django-skeleton')
    manifest['migrations'] = {'leaves': [], 'from_versions': '>=0.0.0'}
    return manifest


def declare_dependency(manifest, name, version, digest, marker=None):
    wheel = f'wheels/{name.replace("-", "_")}-{version}-py3-none-any.whl'
    dependency = {'name': name, 'version': version, 'wheel': wheel, 'sha256': digest}
    if marker is not None:
        dependency['marker'] = marker
    manifest['python_dependencies'] = [dependency]
    manifest['files'] = [{'path': wheel, 'size': 0, 'sha256': digest, 'role': 'wheel'}]


def alternate_package(signed_app, tmp_path):
    manifest = load_manifest(signed_app['manifest'])
    manifest['version'] = '0.2.0'
    signed_app['manifest'].write_bytes(canonical_json(manifest))
    (signed_app['root'] / 'identity.py').write_text('VALUE = "replacement app"\n')
    return build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'replacement.skapp', signed_app['private'])


def test_signed_app_extracts_imports_and_loads_one_manifest(signed_app, tmp_path, monkeypatch):
    verified = validate_package(signed_app['package'], signed_app['trust'])
    destination = tmp_path / 'release/applications/package_demo'
    extract_package(verified, destination)
    assert load_manifest(destination / 'manifest.json') == verified.manifest
    assert not (destination / 'wheels').exists()
    monkeypatch.syspath_prepend(str(tmp_path / 'release'))
    import applications
    monkeypatch.setattr(applications, '__path__', [str(tmp_path / 'release/applications')])
    module = importlib.import_module('applications.package_demo.identity')
    assert module.VALUE == 'verified app'
    from applications.package_demo.apps import DemoConfig
    from skeleton.system.app_registry import AppManifest
    config = DemoConfig('applications.package_demo', importlib.import_module('applications.package_demo'))
    manifest = AppManifest.from_json(destination / 'manifest.json', app_config=config)
    assert manifest.app_id == 'org.test.demo'
    assert manifest.version == '0.1.0'
    assert manifest.origin == 'project-local'  # Not an active confirmed inventory.
    assert Path(manifest.documentation_root) == destination / 'docs'
    for name in tuple(sys.modules):
        if name.startswith('applications.package_demo'):
            sys.modules.pop(name)


@pytest.mark.parametrize('attack', ['corrupt', 'unlisted', 'traversal', 'duplicate', 'symlink', 'normalized', 'signature', 'nul', 'file-parent'])
def test_malicious_archives_fail_before_copy(signed_app, tmp_path, attack):
    def mutate(entries):
        if attack == 'corrupt':
            return [(info, b'CHANGED' if info.filename == 'app/identity.py' else data) for info, data in entries]
        if attack == 'signature':
            return [(info, b64encode(bytes(64)) if info.filename == 'manifest.sig' else data) for info, data in entries]
        if attack == 'duplicate':
            return entries + [next(item for item in entries if item[0].filename == 'app/identity.py')]
        if attack == 'symlink':
            info = zipfile.ZipInfo('app/link')
            info.create_system = 3
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
            return entries + [(info, b'/etc/passwd')]
        if attack == 'normalized':
            return entries + [(zipfile.ZipInfo('app/IDENTITY.py'), b'bad')]
        path = {'unlisted': 'app/unlisted.py', 'traversal': 'app/../../escape.py', 'nul': 'app/bad\x00.py', 'file-parent': 'app/identity.py/child.py'}[attack]
        return entries + [(zipfile.ZipInfo(path), b'bad')]
    artifact = rewrite(signed_app['package'], tmp_path / f'{attack}.skapp', mutate)
    destination = tmp_path / 'must-not-exist'
    with pytest.raises(PackageError):
        extract_package(validate_package(artifact, signed_app['trust']), destination)
    assert not destination.exists()


@pytest.mark.parametrize('manifest_version', [1, 2])
def test_published_history_keeps_signed_bytes_inventory_and_digest(signed_app, tmp_path, manifest_version):
    with zipfile.ZipFile(signed_app['package']) as archive:
        manifest = json.loads(archive.read('manifest.json'))
        contents = {item['path']: archive.read(item['path']) for item in manifest['files']}
    manifest['manifest_version'] = manifest_version
    manifest['compatibility']['skeleton'] = f'<1.0.0,>=0.{manifest_version}.0'
    if manifest_version == 1:
        manifest['runtime'].pop('administration')
        manifest['runtime'].pop('model_ui')
    else:
        manifest['runtime']['model_ui'] = dimension_layout()
    historical = write_signed_archive(manifest, contents, tmp_path / 'published-v1.skapp', signed_app['private'])
    original = historical.read_bytes()
    with zipfile.ZipFile(historical) as archive:
        signed_bytes = archive.read('manifest.json')
    verified = validate_package(historical, signed_app['trust'])
    assert verified.manifest['manifest_version'] == manifest_version
    assert canonical_json(verified.manifest) == signed_bytes
    assert verified.manifest['files'] == json.loads(signed_bytes)['files']
    assert verified.digest == hashlib.sha256(original).hexdigest()
    extract_package(verified, tmp_path / 'restored-v1')
    assert (tmp_path / 'restored-v1/manifest.json').read_bytes() == signed_bytes
    assert historical.read_bytes() == original
    resigned = sign_package(historical, tmp_path / 'resigned.skapp', signed_app['private'])
    with zipfile.ZipFile(resigned) as archive:
        assert archive.read('manifest.json') == signed_bytes
    assert validate_package(resigned, signed_app['trust']).manifest == verified.manifest
    assert historical.read_bytes() == original


def test_new_build_upgrades_v1_source_without_rewriting_it(signed_app, tmp_path):
    source = load_manifest(signed_app['manifest'])
    source['manifest_version'] = 1
    source['compatibility']['skeleton'] = '<1.0.0,>=0.1.0'
    source['runtime'].pop('administration')
    source['runtime'].pop('model_ui')
    original = canonical_json(source)
    signed_app['manifest'].write_bytes(original)
    artifact = build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'new-v2.skapp', signed_app['private'])
    new = validate_package(artifact, signed_app['trust']).manifest
    assert new['manifest_version'] == 2
    assert version_satisfies('0.2.0', new['compatibility']['skeleton'])
    assert not version_satisfies('0.1.0', new['compatibility']['skeleton'])
    assert signed_app['manifest'].read_bytes() == original


def test_unknown_publisher_and_key_mismatch(signed_app, tmp_path):
    _, public = initialize_keys(tmp_path / 'other-private.pem', tmp_path / 'other-public.pem')
    for trust in ({}, {'test-publisher': public}):
        with pytest.raises(PackageError):
            validate_package(signed_app['package'], trust)
    assert inspect_package(signed_app['package'])['trusted'] is False


def test_post_verification_change_and_destination_conflict(signed_app, tmp_path):
    verified = validate_package(signed_app['package'], signed_app['trust'])
    destination = tmp_path / 'existing'
    destination.mkdir()
    with pytest.raises(PackageError):
        extract_package(verified, destination)
    artifact = rewrite(signed_app['package'], tmp_path / 'changed.skapp', lambda entries: entries + [(zipfile.ZipInfo('app/unlisted.py'), b'bad')])
    signed_app['package'].write_bytes(artifact.read_bytes())
    with pytest.raises(PackageError):
        extract_package(verified, tmp_path / 'fresh')
    assert not (tmp_path / 'fresh').exists()


def test_symlink_root_and_bomb_are_rejected(signed_app, tmp_path):
    (signed_app['root'] / 'outside').symlink_to(tmp_path)
    with pytest.raises(PackageError):
        build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'linked.skapp', signed_app['private'])
    bomb = rewrite(signed_app['package'], tmp_path / 'bomb.skapp', lambda entries: entries + [(zipfile.ZipInfo('app/bomb'), bytes(2 * 1024 * 1024))])
    with pytest.raises(PackageError):
        validate_package(bomb, signed_app['trust'])


def test_docs_entrypoint_and_external_files_required(signed_app, tmp_path):
    (signed_app['root'] / 'docs/adr.md').write_text('No decision sections')
    with pytest.raises(PackageError):
        build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'missing-doc.skapp', signed_app['private'])
    (signed_app['root'] / '.env').write_text('secret')
    with pytest.raises(PackageError):
        build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'secret.skapp', signed_app['private'])
    assert not (tmp_path / 'secret.skapp').exists()


def test_dependency_versions_cycles_and_namespace_collisions(signed_app):
    first = load_manifest(signed_app['manifest'])
    second = manifest_template(app_id='org.test.second', app_label='project_second', package_name='second', publisher='test-publisher')
    second['dependencies'] = {first['app_id']: '>=0.1.0,<0.2.0'}
    kwargs = {'core': '0.3.0', 'python': '3.14.0', 'django': '5.2.18'}
    assert validate_dependency_graph([second, first], **kwargs) == [first['app_id'], second['app_id']]
    with pytest.raises(PackageError):
        validate_dependency_graph([second], **kwargs)
    first['dependencies'] = {second['app_id']: '>=0.1.0'}
    with pytest.raises(PackageError):
        validate_dependency_graph([second, first], **kwargs)
    first['dependencies'] = {}
    second['package_name'] = first['package_name']
    second['app_config'] = first['app_config']
    with pytest.raises(PackageError):
        validate_dependency_graph([first, second], **kwargs)
    with pytest.raises(PackageError):
        validate_dependency_graph([first], **{**kwargs, 'django': '6.0.0'})


def test_manifest_disallows_unknown_fields_reserved_names_and_runtime_escape(signed_app):
    base = load_manifest(signed_app['manifest'])
    for change in ({'shell': 'echo bad'}, {'app_label': 'sk_accounts'}, {'runtime': {**base['runtime'], 'urls': 'config.urls'}}, {'files': [{'path': 'app/../other.py', 'size': 0, 'sha256': '0' * 64, 'role': 'code'}]}):
        with pytest.raises(PackageError):
            validate_manifest({**base, **change})


def test_signed_catalog_expiration_channel_filter_and_rollback(signed_app, tmp_path):
    package = validate_package(signed_app['package'], signed_app['trust'])
    entry = catalog_entry(package, artifact='demo.skapp')
    now = datetime.now(timezone.utc)
    index = build_catalog([entry], tmp_path / 'index.json', signed_app['private'], publisher='test-publisher', generation=7, expires_at=(now + timedelta(days=1)).isoformat())
    assert validate_catalog(index, signed_app['trust'], minimum_generation=7)['entries'] == [entry]
    assert validate_catalog(index, signed_app['trust'], channels=['beta'])['entries'] == []
    with pytest.raises(PackageError):
        validate_catalog(index, signed_app['trust'], minimum_generation=8)
    with pytest.raises(PackageError):
        validate_catalog(index, signed_app['trust'], now=now + timedelta(days=2))
    value = json.loads(index.read_text())
    value['catalog']['generation'] = 8
    with pytest.raises(PackageError):
        validate_catalog(value, signed_app['trust'])


def test_resigning_checks_hashes_and_creates_new_release(signed_app, tmp_path):
    output = sign_package(signed_app['package'], tmp_path / 'resigned.skapp', signed_app['private'])
    assert validate_package(output, signed_app['trust']).manifest == validate_package(signed_app['package'], signed_app['trust']).manifest
    with pytest.raises(PackageError):
        sign_package(output, output, signed_app['private'])


def core_wheel(tmp_path, *, escaped=False):
    wheel = tmp_path / 'django_skeleton-0.1.0-py3-none-any.whl'
    with zipfile.ZipFile(wheel, 'w') as archive:
        archive.writestr('skeleton/__init__.py', '')
        archive.writestr('django_skeleton-0.1.0.dist-info/METADATA', 'Metadata-Version: 2.1\nName: django-skeleton\nVersion: 0.1.0\n')
        archive.writestr('django_skeleton-0.1.0.dist-info/WHEEL', 'Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n')
        if escaped:
            archive.writestr('config/settings.py', 'bad')
    return wheel


def test_core_uses_same_trust_and_only_isolated_wheel(signed_app, tmp_path):
    manifest = load_manifest(signed_app['manifest'])
    for key in ('app_id', 'app_label', 'package_name', 'app_config', 'runtime'):
        manifest.pop(key)
    manifest.update(kind='core', component_id='django-skeleton')
    wheel = core_wheel(tmp_path)
    manifest['python_dependencies'] = [{'name': 'django-skeleton', 'version': '0.1.0', 'wheel': 'wheels/' + wheel.name, 'sha256': '0' * 64}]
    manifest['migrations'] = {'leaves': [], 'from_versions': '>=0.0.0'}
    artifact = build_artifact(manifest, {'wheels/' + wheel.name: wheel}, tmp_path / 'core.skapp', signed_app['private'])
    verified = validate_package(artifact, signed_app['trust'])
    roots = extract_package(verified, None, wheels_destination=tmp_path / 'isolated-wheels')
    assert (roots['wheels'] / wheel.name).is_file()
    with pytest.raises(PackageError):
        extract_package(verified, tmp_path / 'wrong')
    core_wheel(tmp_path, escaped=True)
    with pytest.raises(PackageError):
        build_artifact(manifest, {'wheels/' + wheel.name: wheel}, tmp_path / 'escaped-core.skapp', signed_app['private'])


def test_semver_prerelease_and_nonlexical_ranges():
    assert normalize_version('1.2.3-rc.2') == '1.2.3rc2'
    assert version_satisfies('0.10.0', '>=0.2.0,<1.0.0')
    assert not version_satisfies('1.0.0', '<1.0.0')


def test_standalone_import_needs_no_django_configuration():
    result = subprocess.run([sys.executable, '-c', 'import sys; from skeleton.system.packages import manifest_template; assert "django" not in sys.modules; print("standalone")'], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == 'standalone'


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_BYTES", "MAX_FILE_BYTES", "MAX_TOTAL_BYTES", "MAX_MEMBERS"])
def test_archive_limits_fail_closed(signed_app, monkeypatch, limit):
    from skeleton.system import packages
    monkeypatch.setattr(packages, limit, 1)
    with pytest.raises(PackageError):
        validate_package(signed_app["package"], signed_app["trust"])


def test_symlink_extraction_ancestor_and_entrypoint_mismatch(signed_app, tmp_path):
    verified = validate_package(signed_app["package"], signed_app["trust"])
    (tmp_path / "linked-root").symlink_to(tmp_path / "source", target_is_directory=True)
    with pytest.raises(PackageError):
        extract_package(verified, tmp_path / "linked-root" / "escape")
    (signed_app["root"] / "apps.py").write_text(
        'from django.apps import AppConfig\nclass DemoConfig(AppConfig):\n'
        '    name = "config"\n    label = "project_demo"\n')
    with pytest.raises(PackageError):
        build_package(signed_app["root"], signed_app["manifest"], tmp_path / "wrong-name.skapp", signed_app["private"])


def test_cli_commands_share_standalone_api(signed_app, tmp_path):
    from argparse import ArgumentParser
    from skeleton.system.packages import configure_parser, run_command
    trust_path = tmp_path / "trust.json"
    trust_path.write_text(json.dumps({"test-publisher": signed_app["public"].name}))
    parser = configure_parser(ArgumentParser())
    options = vars(parser.parse_args(["validate", str(signed_app["package"]), "--trust-store", str(trust_path)]))
    assert run_command(options)["trusted"] is True
    options = vars(parser.parse_args(["versions", "--manifest", str(signed_app["manifest"])]))
    assert run_command(options)["apps"][0]["origin"] == "project-local"


def test_source_metadata_never_claims_an_unrelated_installed_distribution(monkeypatch):
    import skeleton.versioning as versioning
    class UnrelatedDistribution:
        version = "9.9.9"
        def locate_file(self, path):
            return Path("/unrelated/site-packages") / path
    monkeypatch.setattr(versioning.metadata, "distribution", lambda _: UnrelatedDistribution())
    assert versioning.core_version()["origin"] == "source"
    assert versioning.core_version()["version"] != "9.9.9"


@pytest.mark.parametrize('launcher', ['python', 'python3.14', 'Python.exe', 'pip3.14', 'activate_this.py', 'gunicorn', 'skeleton-core', 'skeleton-deployment-agent'])
@pytest.mark.parametrize('section', ['console_scripts', 'gui_scripts'])
def test_signed_wheel_cannot_replace_reserved_launchers(signed_app, tmp_path, launcher, section):
    wheel = wheel_file(tmp_path / 'wheelhouse', entry_points=f'[{section}]\n{launcher} = dependency:main\n')
    artifact = signed_wheel_archive(signed_app, tmp_path / 'launcher.skapp', [wheel])
    with pytest.raises(PackageError, match='launcher'):
        extract_package(validate_package(artifact, signed_app['trust']), tmp_path / 'app', wheels_destination=tmp_path / 'wheels')
    assert not (tmp_path / 'app').exists()
    assert not (tmp_path / 'wheels').exists()


@pytest.mark.parametrize('hook', ['startup.pth', 'dependency/startup.pth', 'sitecustomize.py', 'usercustomize.py', 'dependency/__pycache__/startup.pyc'])
def test_signed_wheel_startup_hooks_fail_before_copy(signed_app, tmp_path, hook):
    sentinel = tmp_path / 'executed'
    wheel = wheel_file(tmp_path / 'wheelhouse', files={hook: f'import pathlib; pathlib.Path({str(sentinel)!r}).touch()'})
    artifact = signed_wheel_archive(signed_app, tmp_path / 'hook.skapp', [wheel])
    with pytest.raises(PackageError):
        extract_package(validate_package(artifact, signed_app['trust']), tmp_path / 'app', wheels_destination=tmp_path / 'wheels')
    assert not sentinel.exists()
    assert not (tmp_path / 'app').exists()
    assert not (tmp_path / 'wheels').exists()


@pytest.mark.parametrize('collision', ['file', 'script', 'script-case', 'script-wrapper'])
def test_wheelhouse_and_signed_artifact_detect_cross_distribution_ownership(signed_app, tmp_path, collision):
    first = wheel_file(tmp_path / 'wheelhouse', 'first', files={'shared/__init__.py': ''} if collision == 'file' else None,
                       entry_points='[console_scripts]\ndemo-tool = first:main\n' if collision != 'file' else None)
    second_launcher = {'script': 'demo-tool', 'script-case': 'DEMO-TOOL', 'script-wrapper': 'demo-tool.exe'}.get(collision)
    second = wheel_file(tmp_path / 'wheelhouse', 'second', files={'shared/__init__.py': 'OVERWRITE'} if collision == 'file' else None,
                        entry_points=f'[gui_scripts]\n{second_launcher} = second:main\n' if second_launcher else None)
    with pytest.raises(PackageError, match='collision'):
        validate_wheel_set([first, second])
    artifact = signed_wheel_archive(signed_app, tmp_path / 'collision.skapp', [first, second])
    with pytest.raises(PackageError, match='collision'):
        validate_package(artifact, signed_app['trust'])


def test_complete_framework_wheelhouse_tracks_owners_and_identical_reuse(tmp_path):
    core = wheel_file(tmp_path, 'django-skeleton', '0.1.0', files={'skeleton/__init__.py': ''},
                      entry_points='[console_scripts]\nskeleton-core = skeleton.distribution:main\n')
    django = wheel_file(tmp_path, 'Django', '5.2.18', files={'django/__init__.py': ''},
                        entry_points='[console_scripts]\ndjango-admin = django.core.management:execute_from_command_line\n')
    gunicorn = wheel_file(tmp_path, 'gunicorn', '23.0.0', entry_points='[console_scripts]\ngunicorn = gunicorn.app.wsgiapp:run\n')
    duplicate = tmp_path / 'duplicate' / gunicorn.name
    duplicate.parent.mkdir()
    duplicate.write_bytes(gunicorn.read_bytes())
    ownership = validate_wheel_set([core, django, gunicorn, duplicate])
    assert ownership['site-packages/skeleton/__init__.py']['name'] == 'django-skeleton'
    assert ownership['site-packages/django/__init__.py']['name'] == 'django'
    assert ownership['scripts/gunicorn'] == {'name': 'gunicorn', 'version': '23.0.0', 'sha256': hashlib.sha256(gunicorn.read_bytes()).hexdigest()}
    assert ownership['scripts/skeleton-core']['name'] == 'django-skeleton'
    malicious = wheel_file(tmp_path, 'addon', files={'django/__init__.py': 'OVERWRITE'})
    with pytest.raises(PackageError):
        validate_wheel_set([core, django, gunicorn, malicious])
    different_build = wheel_file(tmp_path / 'different-build', 'gunicorn', '23.0.0', files={'gunicorn/__init__.py': 'DIFFERENT'})
    with pytest.raises(PackageError, match='versions or builds'):
        validate_wheel_set([gunicorn, different_build])


def test_core_manifest_version_binds_early_to_wheel(signed_app, tmp_path):
    manifest = core_manifest(signed_app)
    manifest['version'] = '0.2.0'
    wheel = core_wheel(tmp_path)
    manifest['python_dependencies'] = [wheel_dependency(wheel)]
    artifact = write_signed_archive(manifest, {'wheels/' + wheel.name: wheel.read_bytes()}, tmp_path / 'mismatched.skapp', signed_app['private'])
    with pytest.raises(PackageError):
        validate_package(artifact, signed_app['trust'])
    with pytest.raises(PackageError):
        sign_package(artifact, tmp_path / 'resigned-mismatch.skapp', signed_app['private'])
    with pytest.raises(PackageError):
        build_artifact(manifest, {'wheels/' + wheel.name: wheel}, tmp_path / 'built-mismatch.skapp', signed_app['private'])
    assert not (tmp_path / 'resigned-mismatch.skapp').exists()
    assert not (tmp_path / 'built-mismatch.skapp').exists()
    manifest['version'] = '0.1.0'
    manifest['python_dependencies'][0]['marker'] = 'sys_platform == "win32"'
    with pytest.raises(PackageError):
        build_artifact(manifest, {'wheels/' + wheel.name: wheel}, tmp_path / 'conditional-core.skapp', signed_app['private'])


@pytest.mark.parametrize('failure', ['python', 'django', 'skeleton', 'missing', 'range', 'conflict', 'cycle', 'target-version'])
def test_dependency_graph_validates_complete_core_contract(signed_app, failure):
    app = load_manifest(signed_app['manifest'])
    core = core_manifest(signed_app)
    core['version'] = '0.3.0'
    kwargs = {'core': '0.3.0', 'python': '3.14.0', 'django': '5.2.18'}
    core['dependencies'] = {app['app_id']: '>=0.1.0,<0.2.0'}
    assert validate_dependency_graph([core, app], **kwargs) == [app['app_id']]
    if failure in ('python', 'django', 'skeleton'):
        core['compatibility'][failure] = '>=99'
    elif failure == 'missing':
        core['dependencies'] = {'org.test.absent': '>=0.1.0'}
    elif failure == 'range':
        core['dependencies'] = {app['app_id']: '>=0.2.0'}
    elif failure == 'conflict':
        core['conflicts'] = {app['app_id']: '>=0.1.0'}
    elif failure == 'cycle':
        app['dependencies'] = {'django-skeleton': '>=0.1.0'}
    else:
        kwargs['core'] = '0.4.0'
    with pytest.raises(PackageError):
        validate_dependency_graph([core, app], **kwargs)


@pytest.mark.parametrize('target', ['linux', 'win32'])
def test_dependency_graph_compares_only_target_active_wheel_pins(signed_app, target):
    first = load_manifest(signed_app['manifest'])
    second = manifest_template(app_id='org.test.second', app_label='project_second', package_name='second', publisher='test-publisher')
    declare_dependency(first, 'dependency', '1.0.0', 'a' * 64, 'sys_platform == "win32"')
    declare_dependency(second, 'dependency', '2.0.0', 'b' * 64, 'sys_platform == "linux"')
    kwargs = {'core': '0.3.0', 'python': '3.14.0', 'django': '5.2.18', 'target_environment': {'sys_platform': target}}
    assert set(validate_dependency_graph([first, second], **kwargs)) == {first['app_id'], second['app_id']}
    first['python_dependencies'][0]['marker'] = f'sys_platform == "{target}"'
    second['python_dependencies'][0]['marker'] = f'sys_platform == "{target}"'
    with pytest.raises(PackageError, match='versions or builds'):
        validate_dependency_graph([first, second], **kwargs)


def test_target_python_markers_and_metadata_requirements_agree(signed_app, tmp_path):
    first = load_manifest(signed_app['manifest'])
    second = manifest_template(app_id='org.test.second', app_label='project_second', package_name='second', publisher='test-publisher')
    declare_dependency(first, 'dependency', '1.0.0', 'a' * 64, 'python_version < "3.13"')
    declare_dependency(second, 'dependency', '2.0.0', 'b' * 64, 'python_version >= "3.13"')
    for version in ('3.12.10', '3.14.0'):
        assert len(validate_dependency_graph([first, second], core='0.3.0', django='5.2.18', target_environment={'python_full_version': version})) == 2
    with pytest.raises(PackageError, match='disagree'):
        validate_dependency_graph([first], core='0.3.0', django='5.2.18', python='3.14.0', target_environment={'python_full_version': '3.12.10'})
    consumer = wheel_file(tmp_path, 'consumer', metadata='Requires-Python: >=3.12,<3.15\nRequires-Dist: absent>=2; sys_platform == "win32"\nRequires-Dist: helper>=1; python_version >= "3.14"\n')
    assert validate_wheel_set([consumer], {'sys_platform': 'linux', 'python_full_version': '3.12.10'})
    for environment in ({'sys_platform': 'linux', 'python_full_version': '3.14.0'},
                        {'sys_platform': 'win32', 'python_full_version': '3.12.10'},
                        {'sys_platform': 'linux', 'python_full_version': '3.15.0'}):
        with pytest.raises(PackageError):
            validate_wheel_set([consumer], environment)
    helper = wheel_file(tmp_path, 'helper')
    assert validate_wheel_set([consumer, helper], {'sys_platform': 'linux', 'python_full_version': '3.14.0'})


def test_complete_wheelhouse_enforces_requested_extras(tmp_path):
    consumer = wheel_file(tmp_path, 'consumer', metadata='Requires-Dist: helper[feature]>=1\n')
    helper = wheel_file(tmp_path, 'helper', metadata='Requires-Dist: optional>=2; extra == "feature"\n')
    with pytest.raises(PackageError, match='Missing or incompatible'):
        validate_wheel_set([consumer, helper])
    optional = wheel_file(tmp_path, 'optional', '2.0.0')
    assert validate_wheel_set([consumer, helper, optional])


def test_verified_digest_and_zip_share_one_immutable_snapshot(signed_app, tmp_path, monkeypatch):
    from skeleton.system import packages
    original = signed_app['package'].read_bytes()
    replacement = alternate_package(signed_app, tmp_path).read_bytes()
    protected_read = packages._protected_snapshot
    def replace_after_read(path, maximum=None):
        snapshot = protected_read(path, maximum)
        Path(path).write_bytes(replacement)
        return snapshot
    monkeypatch.setattr(packages, '_protected_snapshot', replace_after_read)
    verified = validate_package(signed_app['package'], signed_app['trust'])
    assert verified.digest == hashlib.sha256(original).hexdigest()
    assert verified.manifest['version'] == '0.1.0'
    assert verified._snapshot == original
    assert signed_app['package'].read_bytes() == replacement
    with pytest.raises(PackageError, match='changed after validation'):
        extract_package(verified, tmp_path / 'changed')
    assert not (tmp_path / 'changed').exists()


@pytest.mark.parametrize('consumer', ['extract', 'catalog'])
def test_post_reverification_source_swap_cannot_change_extraction_or_catalog(signed_app, tmp_path, monkeypatch, consumer):
    from skeleton.system import packages
    verified = validate_package(signed_app['package'], signed_app['trust'])
    replacement = alternate_package(signed_app, tmp_path).read_bytes()
    verifier = packages.validate_package
    def replace_after_verify(path, trust):
        fresh = verifier(path, trust)
        Path(path).write_bytes(replacement)
        return fresh
    monkeypatch.setattr(packages, 'validate_package', replace_after_verify)
    if consumer == 'extract':
        destination = tmp_path / 'bound-app'
        extract_package(verified, destination)
        assert (destination / 'identity.py').read_text() == 'VALUE = "verified app"\n'
        assert load_manifest(destination / 'manifest.json') == verified.manifest
    else:
        entry = catalog_entry(verified, artifact='demo.skapp')
        assert entry['digest'] == verified.digest
        assert entry['version'] == '0.1.0'
        with zipfile.ZipFile(io.BytesIO(verified._snapshot)) as archive:
            assert entry['signature'] == archive.read('manifest.sig').decode('ascii')


def test_resigning_reads_only_the_validated_snapshot(signed_app, tmp_path, monkeypatch):
    from skeleton.system import packages
    verified = validate_package(signed_app['package'], signed_app['trust'])
    replacement = alternate_package(signed_app, tmp_path).read_bytes()
    validator = packages._validate_archive
    def replace_after_verify(path, trust_store=None):
        fresh = validator(path, trust_store)
        if Path(path) == signed_app['package']:
            Path(path).write_bytes(replacement)
        return fresh
    monkeypatch.setattr(packages, '_validate_archive', replace_after_verify)
    output = sign_package(signed_app['package'], tmp_path / 'snapshot-resigned.skapp', signed_app['private'])
    assert validate_package(output, signed_app['trust']).manifest == verified.manifest


def test_runtime_wheelhouse_can_exceed_one_artifact_member_limit(tmp_path, monkeypatch):
    from skeleton.system import packages
    first = wheel_file(tmp_path, 'first', files={f'first/module_{index}.py': 'VALUE = 1\n' for index in range(3000)})
    second = wheel_file(tmp_path, 'second', files={f'second/module_{index}.py': 'VALUE = 2\n' for index in range(3000)})
    ownership = validate_wheel_set([first, second])
    assert ownership['site-packages/first/module_2999.py']['name'] == 'first'
    assert ownership['site-packages/second/module_2999.py']['name'] == 'second'
    monkeypatch.setattr(packages, 'MAX_WHEELHOUSE_MEMBERS', 6003)
    with pytest.raises(PackageError):
        validate_wheel_set([first, second])


def dimension_layout(**dimensions):
    return [{'table_key': 'project_demo.item', 'form': {'sections': [
        {'id': 'content', 'label': 'Content', 'fields': [
            {'name': 'body', 'widget': 'textarea', **dimensions}]}]}}]


@pytest.mark.parametrize('dimensions', [{'col_span': 4}, {'rows': 8}, {'col_span': None}, {'rows': None}])
@pytest.mark.parametrize('bound', ['>=0.3.0,<1.0.0', '>0.3.0,<1.0.0', '==0.3.0', '~=0.3.0'])
def test_dimensions_require_explicit_new_floor(signed_app, dimensions, bound):
    manifest = load_manifest(signed_app['manifest'])
    manifest['runtime']['model_ui'] = dimension_layout(**dimensions)
    manifest['compatibility']['skeleton'] = '>=0.2.0,<1.0.0'
    with pytest.raises(PackageError, match=r'^Field dimensions require a core minimum of 0\.3\.0\.$'):
        validate_manifest(manifest)
    manifest['compatibility']['skeleton'] = bound
    assert validate_manifest(manifest)['runtime']['model_ui'] == dimension_layout(**dimensions)


@pytest.mark.parametrize('dimensions', [{'col_span': 4, 'rows': 8}, {'rows': None}])
def test_dimension_source_requires_floor_but_artifact_adds_it_without_mutation(signed_app, tmp_path, dimensions):
    manifest = load_manifest(signed_app['manifest'])
    manifest['runtime']['model_ui'] = dimension_layout(**dimensions)
    manifest['compatibility']['skeleton'] = '>=0.2.0,<1.0.0'
    original = canonical_json(manifest)
    signed_app['manifest'].write_bytes(original)
    with pytest.raises(PackageError, match='Field dimensions require a core minimum'):
        build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'rejected.skapp', signed_app['private'])
    assert signed_app['manifest'].read_bytes() == original
    with zipfile.ZipFile(signed_app['package']) as archive:
        payloads = {}
        for index, item in enumerate(json.loads(archive.read('manifest.json'))['files']):
            source = tmp_path / f'payload-{index}'
            source.write_bytes(archive.read(item['path']))
            payloads[item['path']] = source
    artifact = build_artifact(manifest, payloads, tmp_path / 'dimensions.skapp', signed_app['private'])
    assert canonical_json(manifest) == original
    published = validate_package(artifact, signed_app['trust']).manifest
    assert published['runtime']['model_ui'] == dimension_layout(**dimensions)
    assert version_satisfies('0.3.0', published['compatibility']['skeleton'])
    assert not version_satisfies('0.2.0', published['compatibility']['skeleton'])
    def tamper(entries):
        changed = []
        for info, data in entries:
            if info.filename == 'manifest.json':
                value = json.loads(data)
                value['runtime']['model_ui'][0]['form']['sections'][0]['fields'][0]['col_span'] = 12
                data = canonical_json(value)
            changed.append((info, data))
        return changed
    altered = rewrite(artifact, tmp_path / 'altered.skapp', tamper)
    with pytest.raises(PackageError):
        validate_package(altered, signed_app['trust'])


def test_artifact_validates_dimensions_before_detection(signed_app, tmp_path):
    manifest = load_manifest(signed_app['manifest'])
    manifest['runtime']['model_ui'] = dimension_layout(rows=True)
    with pytest.raises(PackageError):
        build_artifact(manifest, {}, tmp_path / 'invalid.skapp', signed_app['private'])
    assert not (tmp_path / 'invalid.skapp').exists()


def test_new_manifest_template_requires_current_presentation_core(signed_app):
    manifest = load_manifest(signed_app['manifest'])
    assert version_satisfies('0.3.0', manifest['compatibility']['skeleton'])
    assert not version_satisfies('0.2.0', manifest['compatibility']['skeleton'])


def test_dimension_source_with_new_floor_builds_and_verifies(signed_app, tmp_path):
    manifest = load_manifest(signed_app['manifest'])
    manifest['runtime']['model_ui'] = dimension_layout(col_span=12, rows=8)
    original = canonical_json(manifest)
    signed_app['manifest'].write_bytes(original)
    artifact = build_package(signed_app['root'], signed_app['manifest'], tmp_path / 'source-dimensions.skapp', signed_app['private'])
    verified = validate_package(artifact, signed_app['trust'])
    assert verified.manifest['runtime']['model_ui'] == dimension_layout(col_span=12, rows=8)
    assert signed_app['manifest'].read_bytes() == original
