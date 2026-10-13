"""Closed, signed release artifacts. This module never imports Django or app code.

Trust store: publisher/key-id -> Ed25519PublicKey, public PEM text/path, or
{'public_key': public PEM text/path/object, 'publisher': same publisher/key-id}.
An artifact cannot supply its own trust anchor. Python payload remains privileged.
"""
from configparser import ConfigParser, Error as ConfigParserError
from base64 import b64decode, b64encode
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import unicodedata
import zipfile

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from packaging.markers import Marker, default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.version import InvalidVersion, Version
from skeleton.versioning import core_version, normalize_version, validate_specifier, version_satisfies


class PackageError(ValueError):
    pass


MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_MEMBERS = 4096
MAX_WHEELHOUSE_MEMBERS = 65536
MAX_METADATA_BYTES = 2 * 1024 * 1024
MAX_EXPANSION_RATIO = 200
RESERVED_NAMES = frozenset({'skeleton', 'config', 'applications', 'examples', 'django', 'deployment_agent', 'site', 'os', 'sys'})
RESERVED_LAUNCHERS = {
    'gunicorn': 'gunicorn', 'uvicorn': 'uvicorn', 'django-admin': 'django',
    'celery': 'celery', 'skeleton-core': 'django-skeleton',
}
INTERPRETER_LAUNCHER = re.compile(r'(?:pythonw?|pypy|pip|easy_install)(?:\d+(?:\.\d+)*)?(?:[-_].*)?\Z')
IDENTIFIER = re.compile(r'[a-z][a-z0-9_]*\Z')
STABLE_ID = re.compile(r'[a-z][a-z0-9._-]{1,127}\Z')
SHA256 = re.compile(r'[a-f0-9]{64}\Z')


@dataclass(frozen=True)
class VerifiedPackage:
    manifest: dict
    digest: str
    path: Path
    _public_key: object = field(default=None, repr=False, compare=False)
    _snapshot: bytes = field(default=None, repr=False, compare=False)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False).encode('utf-8')


def _fail(message):
    raise PackageError(message)


def _object(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        _fail('Unknown or missing manifest fields.')
    return value


def _string(value, maximum=4096):
    if not isinstance(value, str) or not value or len(value) > maximum or '\x00' in value:
        _fail('Invalid manifest string.')
    return value


def _identifier(value):
    if not isinstance(value, str) or not IDENTIFIER.fullmatch(value):
        _fail('Invalid Python/Django identifier.')
    return value


def _stable_id(value):
    if not isinstance(value, str) or not STABLE_ID.fullmatch(value):
        _fail('Invalid stable component identity.')
    return value


def _path(value):
    _string(value, 512)
    if '\\' in value or value.startswith('/') or unicodedata.normalize('NFC', value) != value:
        _fail('Unsafe archive path.')
    parts = value.split('/')
    if any(not p or p in ('.', '..') or ':' in p or p.rstrip(' .') != p or any(ord(c) < 32 for c in p) for p in parts):
        _fail('Unsafe archive path.')
    if any(p.split('.')[0].casefold() in {'con', 'prn', 'aux', 'nul', *(f'com{i}' for i in range(1, 10)), *(f'lpt{i}' for i in range(1, 10))} for p in parts):
        _fail('Reserved archive path.')
    return value


def _version(value):
    try:
        return normalize_version(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(str(exc)) from exc


def _python_version(value):
    try:
        return Version(_string(value))
    except InvalidVersion as exc:
        raise PackageError('Invalid Python distribution/runtime version.') from exc


def _specifier(value):
    try:
        return validate_specifier(value)
    except (TypeError, ValueError) as exc:
        raise PackageError(str(exc)) from exc


def _compatibility(value):
    _object(value, ('skeleton', 'python', 'django'), ('skeleton', 'python', 'django'))
    return {key: _specifier(spec) for key, spec in value.items()}


def _validate_manifest(value):
    fields = {'manifest_version', 'kind', 'app_id', 'component_id', 'app_label', 'package_name', 'app_config', 'name', 'description', 'version', 'publisher', 'compatibility', 'dependencies', 'conflicts', 'runtime', 'migrations', 'files', 'python_dependencies', 'lifecycle', 'release_notes', 'build'}
    required = {'manifest_version', 'kind', 'name', 'description', 'version', 'publisher', 'compatibility', 'dependencies', 'migrations', 'files', 'python_dependencies', 'lifecycle'}
    _object(value, fields, required)
    m = json.loads(canonical_json(value))
    if type(m['manifest_version']) is not int or m['manifest_version'] != 1 or m['kind'] not in ('app', 'core'):
        _fail('Unsupported manifest format.')
    _string(m['name'], 200)
    _string(m['description'])
    _stable_id(m['publisher'])
    m['version'] = _version(m['version'])
    m['compatibility'] = _compatibility(m['compatibility'])
    if m['kind'] == 'app':
        if set(('app_id', 'app_label', 'package_name', 'app_config', 'runtime')) - set(m) or 'component_id' in m:
            _fail('Application identity/runtime is required.')
        _stable_id(m['app_id'])
        label, package = _identifier(m['app_label']), _identifier(m['package_name'])
        if label.startswith('sk_') or label in RESERVED_NAMES or package in RESERVED_NAMES:
            _fail('Reserved core namespace.')
        prefix = f'applications.{package}.'
        if not isinstance(m['app_config'], str) or not re.fullmatch(re.escape(prefix) + r'apps\.[A-Za-z][A-Za-z0-9_]*', m['app_config']):
            _fail('AppConfig must belong to the managed application namespace.')
        r = _object(m['runtime'], ('urls', 'api_urls', 'default_enabled', 'configuration_fields', 'navigation', 'documentation_root'), ('default_enabled', 'configuration_fields', 'navigation', 'documentation_root'))
        if type(r['default_enabled']) is not bool:
            _fail('default_enabled must be boolean.')
        for key in ('urls', 'api_urls'):
            if r.get(key) is not None and (not isinstance(r[key], str) or not re.fullmatch(re.escape(prefix) + r'[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)*', r[key])):
                _fail('Runtime module must belong to the application.')
        _path(r['documentation_root'])
        if not isinstance(r['configuration_fields'], list) or not isinstance(r['navigation'], list):
            _fail('Runtime fields and navigation require lists.')
        names = set()
        for config in r['configuration_fields']:
            _object(config, ('name', 'type', 'default', 'max_length'), ('name', 'type', 'default'))
            name = _identifier(config['name'])
            if name in names or any(s in name for s in ('secret', 'password', 'token', 'private_key')):
                _fail('Duplicate or secret application configuration.')
            names.add(name)
            expected = {'str': str, 'int': int, 'bool': bool}.get(config['type'])
            maximum = config.get('max_length', 200)
            if expected is None or type(config['default']) is not expected or type(maximum) is not int or not 1 <= maximum <= 10000 or (expected is str and len(config['default']) > maximum):
                _fail('Invalid scalar configuration field.')
        for entry in r['navigation']:
            _object(entry, ('label', 'url_name', 'table_key', 'action'), ('label', 'url_name', 'table_key'))
            _string(entry['label'], 200)
            _string(entry['url_name'], 200)
            if not isinstance(entry['table_key'], str) or not re.fullmatch(re.escape(label) + r'\.[a-z][a-z0-9_]*', entry['table_key']) or entry.get('action', 'read') not in ('read', 'create', 'update', 'delete', 'restore', 'manage_access', 'audit'):
                _fail('Invalid navigation ownership/action.')
    else:
        if m.get('component_id') != 'django-skeleton' or any(k in m for k in ('app_id', 'app_label', 'package_name', 'app_config', 'runtime')):
            _fail('Invalid core component identity.')
    identity = m.get('app_id', m.get('component_id'))
    if not isinstance(m['dependencies'], dict):
        _fail('Dependencies require an app-ID/version-range mapping.')
    for dependency, spec in m['dependencies'].items():
        _stable_id(dependency)
        if dependency == identity:
            _fail('Self dependency.')
        _specifier(spec)
    conflicts = m.get('conflicts', {})
    if not isinstance(conflicts, dict):
        _fail('Conflicts require an app-ID/version-range mapping.')
    for dependency, spec in conflicts.items():
        _stable_id(dependency)
        _specifier(spec)
    migrations = _object(m['migrations'], ('leaves', 'from_versions', 'labels'), ('leaves', 'from_versions'))
    if not isinstance(migrations['leaves'], list) or len(set(migrations['leaves'])) != len(migrations['leaves']):
        _fail('Migration leaves must be unique names.')
    for leaf in migrations['leaves']:
        if not isinstance(leaf, str) or not re.fullmatch(r'[A-Za-z0-9_]+', leaf):
            _fail('Invalid migration leaf.')
    _specifier(migrations['from_versions'])
    if 'labels' in migrations:
        if not isinstance(migrations['labels'], list) or len(set(migrations['labels'])) != len(migrations['labels']):
            _fail('Invalid migration labels.')
        for label in migrations['labels']:
            _identifier(label)
        if m['kind'] == 'app' and migrations['labels'] != [m['app_label']]:
            _fail('Application migrations must use its own label.')
    lifecycle = _object(m['lifecycle'], ('data_retention', 'maintenance', 'backup'), ('data_retention', 'maintenance', 'backup'))
    if lifecycle['data_retention'] != 'retain' or lifecycle['maintenance'] is not True or lifecycle['backup'] is not True:
        _fail('Release requires maintenance, backup and retained data.')
    if not isinstance(m['files'], list) or len(m['files']) > MAX_MEMBERS - 2:
        _fail('Invalid file inventory.')
    seen = set()
    for item in m['files']:
        _object(item, ('path', 'size', 'sha256', 'role'), ('path', 'size', 'sha256', 'role'))
        path = _path(item['path'])
        allowed_roots = ('app/', 'wheels/') if m['kind'] == 'app' else ('wheels/',)
        if not path.startswith(allowed_roots) or path.casefold() in seen:
            _fail('Unowned or duplicate file inventory path.')
        if path in ('app/manifest.json', 'app/manifest.sig'):
            _fail('Canonical manifest/signature are installer-owned, not payload files.')
        if m['kind'] == 'app' and path.startswith(('app/templates/', 'app/static/')):
            prefix = '/'.join(path.split('/')[:2]) + '/' + m['app_label'] + '/'
            if not path.startswith(prefix):
                _fail('Static/template files must belong to the app label namespace.')
        seen.add(path.casefold())
        if type(item['size']) is not int or not 0 <= item['size'] <= MAX_FILE_BYTES or not isinstance(item['sha256'], str) or not SHA256.fullmatch(item['sha256']):
            _fail('Invalid file size/hash.')
        roles = ('code', 'migration', 'template', 'static', 'documentation', 'data') if path.startswith('app/') else ('wheel',)
        if item['role'] not in roles:
            _fail('Invalid file role.')
    if not isinstance(m['python_dependencies'], list):
        _fail('Python dependencies require a wheel list.')
    wheel_paths = set()
    wheel_names = set()
    for dependency in m['python_dependencies']:
        _object(dependency, ('name', 'version', 'wheel', 'sha256', 'marker'), ('name', 'version', 'wheel', 'sha256'))
        path = _path(dependency['wheel'])
        if not path.startswith('wheels/') or '/' in path[7:] or not path.endswith('.whl') or path in wheel_paths:
            _fail('Only individually declared wheels are supported.')
        try:
            name, version, _, _ = parse_wheel_filename(Path(path).name)
        except Exception as exc:
            raise PackageError('Invalid wheel filename.') from exc
        if canonicalize_name(_string(dependency['name'])) != name or version != _python_version(dependency['version']) or name in wheel_names:
            _fail('Wheel identity or version mismatch.')
        if 'marker' in dependency:
            from packaging.markers import Marker, InvalidMarker
            try:
                Marker(dependency['marker'])
            except (InvalidMarker, TypeError) as exc:
                raise PackageError('Invalid wheel marker.') from exc
        wheel_paths.add(path)
        wheel_names.add(name)
        item = next((i for i in m['files'] if i['path'] == path), None)
        if item is None or item['sha256'] != dependency['sha256']:
            _fail('Wheel inventory/hash mismatch.')
        if m['kind'] == 'core' and name == 'django-skeleton':
            if Version(dependency['version']) != Version(m['version']) or 'marker' in dependency:
                _fail('Core manifest version must match its unconditional core wheel.')
    if {i['path'] for i in m['files'] if i['path'].startswith('wheels/')} != wheel_paths:
        _fail('Unlisted wheel dependency.')
    if m['kind'] == 'core' and 'django-skeleton' not in wheel_names and m['files']:
        _fail('Core artifact requires its core wheel.')
    for optional in ('release_notes', 'build'):
        if optional in m:
            _string(m[optional])
    return m


def validate_manifest(value):
    """Validate source or inventoried manifest; return canonical independent data."""
    try:
        return _validate_manifest(value)
    except PackageError:
        raise
    except (ValueError, TypeError, KeyError, RecursionError) as exc:
        raise PackageError('Invalid manifest field type or value.') from exc


def _json(data):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                _fail('Duplicate JSON object key.')
            result[key] = value
        return result
    try:
        return json.loads(data, object_pairs_hook=pairs, parse_constant=lambda _: _fail('Invalid JSON number.'))
    except (ValueError, UnicodeError, TypeError, RecursionError) as exc:
        raise PackageError('Invalid release JSON.') from exc


def load_manifest(path):
    path = Path(path)
    if path.stat().st_size > MAX_METADATA_BYTES:
        _fail('Manifest is too large.')
    return validate_manifest(_json(path.read_bytes()))


def _public_key(value):
    if isinstance(value, Ed25519PublicKey):
        return value
    if isinstance(value, dict):
        _object(value, ('public_key', 'publisher'), ('public_key',))
        value = value['public_key']
    if isinstance(value, Ed25519PublicKey):
        return value
    try:
        data = value.encode() if isinstance(value, str) and '-----BEGIN' in value else Path(value).read_bytes()
        key = serialization.load_pem_public_key(data)
    except (ValueError, TypeError, OSError) as exc:
        raise PackageError('Invalid trusted publisher key.') from exc
    if not isinstance(key, Ed25519PublicKey):
        _fail('Publisher keys must be Ed25519.')
    return key


def _trusted_key(publisher, trust_store):
    if not isinstance(trust_store, dict) or publisher not in trust_store:
        _fail('Publisher is not trusted.')
    value = trust_store[publisher]
    if isinstance(value, dict) and value.get('publisher', publisher) != publisher:
        _fail('Publisher trust restriction mismatch.')
    return _public_key(value)


def _private_key(path):
    try:
        key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise PackageError('Invalid private signing key.') from exc
    if not isinstance(key, Ed25519PrivateKey):
        _fail('Signing key must be Ed25519.')
    return key


def _verify(key, signature, data):
    try:
        key.verify(b64decode(signature, validate=True), data)
    except (InvalidSignature, ValueError, TypeError) as exc:
        raise PackageError('Invalid release signature.') from exc


def _zip_members(archive):
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        _fail('Archive contains too many members.')
    seen = set()
    files = {}
    directories = set()
    total = 0
    for info in members:
        raw = info.filename
        if info.orig_filename != raw:
            _fail('Truncated/NUL archive filename.')
        name = _path(raw[:-1] if raw.endswith('/') else raw)
        folded = name.casefold()
        if folded in seen:
            _fail('Duplicate or normalized archive collision.')
        seen.add(folded)
        mode = info.external_attr >> 16
        if stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR) or info.flag_bits & 1 or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
            _fail('Links, special files, encryption and unsupported compression are forbidden.')
        if info.is_dir():
            if info.file_size or stat.S_IFMT(mode) == stat.S_IFREG:
                _fail('Invalid directory archive member.')
            directories.add(name)
            continue
        if stat.S_IFMT(mode) == stat.S_IFDIR or info.file_size > MAX_FILE_BYTES or info.file_size > max(1, info.compress_size) * MAX_EXPANSION_RATIO:
            _fail('Archive expansion limit exceeded.')
        total += info.file_size
        if total > MAX_TOTAL_BYTES:
            _fail('Archive uncompressed size limit exceeded.')
        files[name] = info
    file_names = {p.casefold() for p in files}
    for path in files:
        if any(str(parent).casefold() in file_names for parent in PurePosixPath(path).parents if str(parent) != '.'):
            _fail('File/directory archive collision.')
    for directory in directories:
        if not any(p.startswith(directory + '/') for p in files):
            _fail('Unowned empty archive directory.')
    return files


def _read_member(archive, info, maximum=MAX_FILE_BYTES):
    if info.file_size > maximum:
        _fail('Release metadata/member size limit exceeded.')
    data = archive.read(info)
    if len(data) != info.file_size:
        _fail('Archive member size mismatch.')
    return data


def _target_environment(value=None):
    environment = default_environment()
    environment['extra'] = ''
    if value is not None:
        if not isinstance(value, dict) or any(key not in environment or not isinstance(item, str) for key, item in value.items()):
            _fail('Invalid target marker environment.')
        environment.update(value)
        if 'python_full_version' in value and 'python_version' not in value:
            environment['python_version'] = '.'.join(str(part) for part in _python_version(environment['python_full_version']).release[:2])
    full = _python_version(environment['python_full_version'])
    short = _python_version(environment['python_version'])
    if full.release[:2] != short.release[:2]:
        _fail('Target Python marker versions disagree.')
    return environment


def _marker_active(marker, environment):
    try:
        return not marker or Marker(marker).evaluate(environment=environment)
    except (ValueError, TypeError, KeyError) as exc:
        raise PackageError('Invalid target dependency marker.') from exc


def _wheel_scripts(archive, files, dist_info, distribution):
    entry_path = dist_info + '/entry_points.txt'
    if entry_path not in files:
        return set()
    parser = ConfigParser(interpolation=None, strict=True)
    parser.optionxform = str
    try:
        parser.read_string(_read_member(archive, files[entry_path], MAX_METADATA_BYTES).decode('utf-8'))
    except (ConfigParserError, UnicodeError) as exc:
        raise PackageError('Invalid wheel entry points.') from exc
    scripts = set()
    for section in ('console_scripts', 'gui_scripts'):
        for name, entry in parser.items(section) if parser.has_section(section) else ():
            _path(name)
            folded = name.casefold()
            launcher = re.sub(r'(?:\.exe|\.pyw?|-script\.pyw?)\Z', '', folded)
            if '/' in name or INTERPRETER_LAUNCHER.fullmatch(launcher) or re.fullmatch(r'(?:activate|deactivate)(?:[._-].*)?', launcher) or launcher == 'skeleton-deployment-agent':
                _fail('Wheel entry point replaces a reserved interpreter/operator launcher.')
            if launcher in RESERVED_LAUNCHERS and distribution != RESERVED_LAUNCHERS[launcher]:
                _fail('Wheel entry point replaces a framework launcher.')
            if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*(?:\s*\[[A-Za-z0-9_, .-]+\])?', entry):
                _fail('Invalid executable wheel entry point.')
            # Include POSIX and Windows wrappers, regardless of build host.
            generated = {f'scripts/{folded}', f'scripts/{folded}.exe', f'scripts/{folded}-script.py', f'scripts/{folded}-script.pyw'}
            if generated & scripts:
                _fail('Wheel generated executable collision.')
            scripts.update(generated)
    return scripts


def _wheel(data, dependency, *, core=False):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            files = _zip_members(archive)
            distribution = canonicalize_name(dependency['name'])
            name = distribution.replace('-', '_')
            dist_info = f"{name}-{dependency['version']}.dist-info"
            metadata_paths = [p for p in files if p.endswith('.dist-info/METADATA')]
            if len(metadata_paths) != 1 or metadata_paths[0].split('/')[0].casefold() != dist_info.casefold():
                _fail('Wheel metadata identity mismatch.')
            from email.parser import BytesParser
            metadata = BytesParser().parsebytes(_read_member(archive, files[metadata_paths[0]], MAX_METADATA_BYTES))
            if canonicalize_name(metadata.get('Name', '')) != distribution or str(Version(metadata.get('Version', ''))) != dependency['version']:
                _fail('Wheel metadata version mismatch.')
            is_core = distribution == 'django-skeleton'
            if is_core and not core:
                _fail('Application cannot own the core distribution.')
            for path, info in files.items():
                root = path.split('/')[0]
                if '.data' in root or (root.endswith('.dist-info') and root != dist_info) or (is_core and root not in ('skeleton', dist_info)) or (not is_core and root.split('.')[0].casefold() in ('applications', 'skeleton', 'config', 'examples', 'deployment_agent')):
                    _fail('Wheel escapes its permitted distribution namespace.')
                folded_root = root.casefold().split('.')[0]
                if path.casefold().endswith(('.pth', '.pyc', '.pyo')) or folded_root in ('sitecustomize', 'usercustomize', 'site', 'os', 'sys'):
                    _fail('Wheel startup hooks and precompiled interpreter files are forbidden.')
                if folded_root == 'django' and distribution != 'django':
                    _fail('Only Django may own the framework namespace.')
                _read_member(archive, info)
            if is_core and 'skeleton/__init__.py' not in files:
                _fail('Core wheel lacks skeleton namespace.')
            roots = {path.split('/')[0].casefold().split('.')[0] for path in files if not path.split('/')[0].endswith('.dist-info')}
            destinations = {'site-packages/' + path.casefold() for path in files}
            destinations.update('site-packages/' + dist_info.casefold() + '/' + name.casefold() for name in ('INSTALLER', 'REQUESTED', 'direct_url.json'))
            destinations.update(_wheel_scripts(archive, files, dist_info, distribution))
            return destinations, roots, sum(info.file_size for info in files.values()), len(files), metadata
    except (zipfile.BadZipFile, RuntimeError, OSError, ValueError) as exc:
        if isinstance(exc, PackageError):
            raise
        raise PackageError('Invalid isolated wheel.') from exc


def _claim_wheel(destinations, roots, owner, ownership, namespaces):
    for destination in destinations:
        if destination in ownership and ownership[destination] != owner:
            _fail('Wheel installed destination collision.')
        for parent in PurePosixPath(destination).parents:
            previous = ownership.get(str(parent))
            if previous is not None and previous != owner:
                _fail('Wheel installed file/directory collision.')
    if any(previous != owner and any(path.startswith(destination + '/') for destination in destinations)
           for path, previous in ownership.items()):
        _fail('Wheel installed file/directory collision.')
    for namespace in roots:
        if namespace in namespaces and namespaces[namespace] != owner:
            _fail('Wheel Python namespace collision.')
    ownership.update(dict.fromkeys(destinations, owner))
    namespaces.update(dict.fromkeys(roots, owner))


def _protected_snapshot(path, maximum=None):
    """Read once from a regular no-follow descriptor into immutable bytes."""
    maximum = MAX_ARCHIVE_BYTES if maximum is None else maximum
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(descriptor, 'rb') as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > maximum:
                _fail('Compressed archive size limit exceeded or file is not regular.')
            data = handle.read(maximum + 1)
            after = os.fstat(handle.fileno())
            if len(data) > maximum:
                _fail('Compressed archive size limit exceeded.')
            if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns) or len(data) != before.st_size:
                _fail('Package changed while reading its snapshot.')
            return data
    except OSError as exc:
        raise PackageError('Package file is unavailable.') from exc


def validate_wheel_set(paths, target_environment=None):
    """Validate the complete runtime wheelhouse before pip can overwrite files.

Return case-folded site-packages/<path> and scripts/<name> ownership entries,
each containing name, version and sha256. Identical distribution/digest reuse
is allowed; .pth startup hooks and bootstrap/operator launchers are not.
Marker and Requires-Python evaluation uses the explicit target environment.
"""
    environment = _target_environment(target_environment)
    ownership, namespaces, distributions = {}, {}, {}
    expanded_total = member_total = 0
    for path in paths:
        path = Path(path)
        try:
            name, version, _, _ = parse_wheel_filename(path.name)
        except ValueError as exc:
            raise PackageError('Invalid wheel filename.') from exc
        data = _protected_snapshot(path, MAX_FILE_BYTES)
        owner = {'name': name, 'version': str(version), 'sha256': hashlib.sha256(data).hexdigest()}
        if name in distributions:
            if distributions[name][0] != owner:
                _fail('Conflicting fixed Python dependency versions or builds.')
            continue
        destinations, roots, expanded, members, metadata = _wheel(data, owner, core=True)
        expanded_total += expanded
        member_total += members
        if expanded_total > MAX_TOTAL_BYTES or member_total > MAX_WHEELHOUSE_MEMBERS:
            _fail('Combined wheelhouse expansion/member limit exceeded.')
        _claim_wheel(destinations, roots, owner, ownership, namespaces)
        if metadata.get('Requires-Python') and not version_satisfies(environment['python_full_version'], _specifier(metadata['Requires-Python'])):
            _fail('Incompatible wheel Python version.')
        try:
            requirements = [Requirement(value) for value in metadata.get_all('Requires-Dist', ())]
        except ValueError as exc:
            raise PackageError('Invalid wheel dependency metadata.') from exc
        distributions[name] = (owner, requirements)
    extras = {name: {''} for name in distributions}
    changed = True
    while changed:
        changed = False
        for name, (_, requirements) in distributions.items():
            for requirement in requirements:
                if requirement.marker and not any(requirement.marker.evaluate(environment={**environment, 'extra': extra}) for extra in tuple(extras[name])):
                    continue
                dependency = canonicalize_name(requirement.name)
                if requirement.url or dependency not in distributions or not requirement.specifier.contains(distributions[dependency][0]['version'], prereleases=True):
                    _fail('Missing or incompatible wheel dependency.')
                requested = set(requirement.extras) - extras[dependency]
                if requested:
                    extras[dependency].update(requested)
                    changed = True
    return ownership


def _documentation(manifest, contents):
    root = 'app/' + manifest['runtime']['documentation_root']
    for filename, headings in {'spec.md': ('## Contract', '## Acceptance'), 'adr.md': ('## Context', '## Decision', '## Consequences')}.items():
        try:
            text = contents[root + '/' + filename].decode('utf-8')
        except (KeyError, UnicodeError) as exc:
            raise PackageError('Required app documentation is missing.') from exc
        if any(heading not in text for heading in headings):
            _fail('App documentation lacks required contract/ADR sections.')


def _entrypoint(manifest, archive, files):
    """Verify declared identities without importing publisher code."""
    import ast
    try:
        tree = ast.parse(_read_member(archive, files["app/apps.py"]))
    except (SyntaxError, UnicodeError) as exc:
        raise PackageError("Application entrypoint is not valid Python.") from exc
    name = manifest["app_config"].rsplit(".", 1)[1]
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == name]
    if len(classes) != 1:
        _fail("Declared AppConfig class is absent or ambiguous.")
    attributes = {}
    for node in classes[0].body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    attributes[target.id] = node.value
    label = attributes.get("label")
    package = attributes.get("name")
    if not isinstance(label, ast.Constant) or label.value != manifest["app_label"]:
        _fail("AppConfig must explicitly preserve its declared Django label.")
    expected = "applications." + manifest["package_name"]
    if not ((isinstance(package, ast.Name) and package.id == "__package__")
            or (isinstance(package, ast.Constant) and package.value == expected)):
        _fail("AppConfig name must be its managed namespace or __package__.")
    for key in ("urls", "api_urls"):
        module = manifest["runtime"].get(key)
        if module:
            relative = module[len(expected) + 1:].replace(".", "/")
            if f"app/{relative}.py" not in files and f"app/{relative}/__init__.py" not in files:
                _fail("Declared runtime module is absent.")
    for leaf in manifest["migrations"]["leaves"]:
        if f"app/migrations/{leaf}.py" not in files:
            _fail("Declared migration leaf is absent.")


def _validate_archive(path, trust_store=None):
    path = Path(path)
    snapshot = _protected_snapshot(path, MAX_ARCHIVE_BYTES)
    digest = hashlib.sha256(snapshot).hexdigest()
    try:
        with zipfile.ZipFile(io.BytesIO(snapshot)) as archive:
            files = _zip_members(archive)
            if not {'manifest.json', 'manifest.sig'} <= files.keys():
                _fail('Signed manifest is required.')
            raw = _read_member(archive, files['manifest.json'], MAX_METADATA_BYTES)
            manifest = validate_manifest(_json(raw))
            if raw != canonical_json(manifest):
                _fail('Manifest must use canonical JSON and normalized versions/ranges.')
            signature = _read_member(archive, files['manifest.sig'], 256)
            try:
                if len(b64decode(signature, validate=True)) != 64:
                    _fail('Invalid release signature encoding.')
            except (ValueError, TypeError) as exc:
                raise PackageError('Invalid release signature encoding.') from exc
            key = _trusted_key(manifest['publisher'], trust_store) if trust_store is not None else None
            if key is not None:
                _verify(key, signature, raw)
            inventory = {i['path']: i for i in manifest['files']}
            if files.keys() != inventory.keys() | {'manifest.json', 'manifest.sig'}:
                _fail('Archive files differ from the complete signed inventory.')
            docs = {}
            wheel_ownership, wheel_namespaces = {}, {}
            expanded_total = sum(info.file_size for info in files.values())
            member_total = len(files)
            for name, item in inventory.items():
                data = _read_member(archive, files[name])
                if len(data) != item['size'] or hashlib.sha256(data).hexdigest() != item['sha256']:
                    _fail('Payload hash or size mismatch.')
                if name.startswith('wheels/'):
                    dependency = next(d for d in manifest['python_dependencies'] if d['wheel'] == name)
                    destinations, owned_roots, expanded, members, _ = _wheel(data, dependency, core=manifest['kind'] == 'core')
                    expanded_total += expanded
                    member_total += members
                    if expanded_total > MAX_TOTAL_BYTES or member_total > MAX_MEMBERS:
                        _fail('Combined nested wheel expansion/member limit exceeded.')
                    owner = {'name': canonicalize_name(dependency['name']), 'version': dependency['version'], 'sha256': item['sha256']}
                    _claim_wheel(destinations, owned_roots, owner, wheel_ownership, wheel_namespaces)
                if name.startswith('app/' + manifest.get('runtime', {}).get('documentation_root', '\x00') + '/'):
                    docs[name] = data
            if manifest['kind'] == 'app':
                if not {'app/__init__.py', 'app/apps.py'} <= inventory.keys():
                    _fail('Application package entrypoint is missing.')
                _documentation(manifest, docs)
                _entrypoint(manifest, archive, files)
            elif not manifest['files']:
                _fail('Core artifact is empty.')
            return VerifiedPackage(manifest, digest, path.absolute(), key, snapshot)
    except (zipfile.BadZipFile, RuntimeError, OSError, KeyError) as exc:
        raise PackageError('Invalid package archive.') from exc


def validate_package(path, trust_store):
    if trust_store is None:
        _fail('Trusted publisher configuration is required.')
    return _validate_archive(path, trust_store)


def inspect_package(path, trust_store=None):
    package = _validate_archive(path, trust_store)
    return {'trusted': trust_store is not None, 'digest': package.digest, 'manifest': package.manifest}


def _role(path):
    if path.startswith('wheels/'):
        return 'wheel'
    if '/migrations/' in path:
        return 'migration'
    if '/templates/' in path:
        return 'template'
    if '/static/' in path:
        return 'static'
    if '/docs/' in path:
        return 'documentation'
    return 'code' if path.endswith('.py') else 'data'


def build_artifact(manifest, payloads, output_path, private_key_path):
    """Build app/core from a complete mapping of archive path -> regular local file."""
    m = json.loads(canonical_json(manifest))
    m['files'] = []
    contents = {}
    for name, source in sorted(payloads.items()):
        _path(name)
        source = Path(source)
        if source.is_symlink() or not source.is_file():
            _fail('Payload must be regular files, never links.')
        if source.stat().st_size > MAX_FILE_BYTES:
            _fail('Payload file too large.')
        data = source.read_bytes()
        contents[name] = data
        m['files'].append({'path': name, 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest(), 'role': _role(name)})
    for dependency in m['python_dependencies']:
        if dependency['wheel'] not in contents:
            _fail('Declared wheel is missing.')
        dependency['sha256'] = hashlib.sha256(contents[dependency['wheel']]).hexdigest()
    m = validate_manifest(m)
    raw = canonical_json(m)
    key = _private_key(private_key_path)
    output = Path(output_path)
    if output.exists():
        _fail('Output already exists; releases are immutable.')
    output.parent.mkdir(parents=True, exist_ok=True)
    try:
        with zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', raw)
            archive.writestr('manifest.sig', b64encode(key.sign(raw)))
            for name, data in contents.items():
                archive.writestr(name, data)
        validate_package(output, {m['publisher']: key.public_key()})
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def build_package(app_root, manifest_path, output_path, private_key_path, wheels=()):
    root = Path(app_root).resolve()
    manifest = load_manifest(manifest_path)
    if manifest['kind'] != 'app':
        _fail('build_package requires an application manifest.')
    output = Path(output_path).resolve()
    if output.is_relative_to(root):
        _fail('Build output must be outside the application root.')
    payloads = {}
    excluded = {'__pycache__', '.git', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'build', 'dist', '.venv'}
    manifest_source = Path(manifest_path).resolve()
    for current, directories, filenames in os.walk(root, followlinks=False):
        for name in list(directories):
            candidate = Path(current) / name
            if candidate.is_symlink():
                _fail('Application root contains a symlink.')
            if name in excluded:
                directories.remove(name)
        for name in filenames:
            source = Path(current) / name
            if source.is_symlink():
                _fail('Application root contains a symlink.')
            if name.endswith(('.pyc', '.pyo')) or source.resolve() == manifest_source:
                continue
            relative = source.relative_to(root).as_posix()
            if any(part.startswith('.') for part in source.relative_to(root).parts) or name in ('.env', 'db.sqlite3') or name.endswith(('.pem', '.key')):
                _fail('Private files must not be packaged.')
            payloads['app/' + relative] = source
    for wheel in wheels:
        wheel = Path(wheel)
        name = 'wheels/' + wheel.name
        if name in payloads:
            _fail('Duplicate supplied wheel.')
        payloads[name] = wheel
    # Runtime uses the same canonical contract after relocation; the package's
    # root signed manifest remains authoritative (no circular inventory hash).
    return build_artifact(manifest, payloads, output_path, private_key_path)


def extract_package(verified, destination, *, wheels_destination=None):
    """Reverify and exclusively create exact app root and separate wheel root.

No merge/overwrite, symlink ancestors, or wheels beneath the app directory.
The canonical manifest is copied beside app files for AppManifest.from_json.
"""
    if not isinstance(verified, VerifiedPackage) or verified._public_key is None:
        _fail('Only trusted verified artifacts may be extracted.')
    fresh = validate_package(verified.path, {verified.manifest['publisher']: verified._public_key})
    if fresh.digest != verified.digest or fresh.manifest != verified.manifest:
        _fail('Artifact changed after validation.')
    roots = {}
    if fresh.manifest['kind'] == 'app':
        if destination is None:
            _fail('Application destination is required.')
        roots['app'] = Path(destination).absolute()
    elif destination is not None:
        _fail('Core artifacts contain isolated wheels only.')
    if fresh.manifest['python_dependencies']:
        if wheels_destination is None:
            _fail('An explicit isolated wheels destination is required.')
        roots['wheels'] = Path(wheels_destination).absolute()
    for root in roots.values():
        if root.exists() or root.is_symlink() or any(parent.is_symlink() for parent in root.parents):
            _fail('Extraction destination exists or has symlink ancestors.')
    if len(roots) == 2 and (roots['app'].is_relative_to(roots['wheels']) or roots['wheels'].is_relative_to(roots['app'])):
        _fail('App and wheels destinations must be separate.')
    created = []
    try:
        for root in roots.values():
            root.mkdir(parents=True, exist_ok=False)
            created.append(root)
        with zipfile.ZipFile(io.BytesIO(fresh._snapshot)) as archive:
            for item in fresh.manifest['files']:
                prefix, relative = item['path'].split('/', 1)
                target = roots[prefix] / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                data = archive.read(item['path'])
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    _fail('Artifact changed during extraction.')
                with target.open('xb') as handle:
                    handle.write(data)
                target.chmod(0o644)
            if 'app' in roots:
                with (roots['app'] / 'manifest.json').open('xb') as handle:
                    handle.write(canonical_json(fresh.manifest))
                with (roots['app'] / 'manifest.sig').open('xb') as handle:
                    handle.write(archive.read('manifest.sig'))
    except Exception:
        for root in created:
            shutil.rmtree(root)
        raise
    return roots


def validate_dependency_graph(manifests, *, core=None, python=None, django=None, target_environment=None):
    """Validate core and all apps; return dependency-first application IDs.

Wheel pins are compared only when their markers apply to target_environment,
which accepts the string mapping from packaging.markers.default_environment().
"""
    from importlib.metadata import version as distribution_version
    inventory = [validate_manifest(value) for value in manifests]
    selected_core = [m for m in inventory if m['kind'] == 'core']
    if len(selected_core) > 1:
        _fail('Core component identity collision.')
    core = core or (selected_core[0]['version'] if selected_core else core_version()['version'])
    environment = _target_environment(target_environment)
    python = python or environment['python_full_version']
    if target_environment and 'python_full_version' in target_environment and Version(python) != Version(environment['python_full_version']):
        _fail('Target Python version and marker environment disagree.')
    environment['python_full_version'] = python
    environment['python_version'] = '.'.join(str(part) for part in Version(python).release[:2])
    django = django or distribution_version('Django')
    if selected_core and Version(selected_core[0]['version']) != Version(core):
        _fail('Core manifest version differs from the selected core.')
    components, labels, packages = {}, set(), set()
    python_distributions = {}
    for m in inventory:
        for dependency in m['python_dependencies']:
            if not _marker_active(dependency.get('marker'), environment):
                continue
            name = canonicalize_name(dependency['name'])
            pin = (dependency['version'], dependency['sha256'])
            if name in python_distributions and python_distributions[name] != pin:
                _fail('Conflicting fixed Python dependency versions or builds.')
            python_distributions[name] = pin
        identity = m.get('app_id', m.get('component_id'))
        if identity in components:
            _fail('Component identity collision.')
        if m['kind'] == 'app':
            if m['app_label'] in labels or m['package_name'] in packages:
                _fail('Application identity/namespace collision.')
            labels.add(m['app_label'])
            packages.add(m['package_name'])
        components[identity] = m
        for target, version in {'skeleton': core, 'python': python, 'django': django}.items():
            if version is None or not version_satisfies(version, m['compatibility'][target]):
                _fail(f'Incompatible {target} version for {identity}.')
    order, visiting, visited = [], set(), set()
    def visit(identity):
        if identity in visiting:
            _fail('Cyclic component dependency.')
        if identity in visited:
            return
        visiting.add(identity)
        for dependency, spec in components[identity]['dependencies'].items():
            if dependency not in components or not version_satisfies(components[dependency]['version'], spec):
                _fail('Missing or incompatible application dependency.')
            visit(dependency)
        for conflict, spec in components[identity].get('conflicts', {}).items():
            if conflict in components and version_satisfies(components[conflict]['version'], spec):
                _fail('Conflicting application releases.')
        visiting.remove(identity)
        visited.add(identity)
        if components[identity]['kind'] == 'app':
            order.append(identity)
    for identity in sorted(components):
        visit(identity)
    return order


def initialize_keys(private_path, public_path):
    private, public = Path(private_path), Path(public_path)
    if private == public or private.exists() or public.exists():
        _fail('Key output paths must be new and distinct.')
    key = Ed25519PrivateKey.generate()
    private.parent.mkdir(parents=True, exist_ok=True)
    public.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, 'wb') as handle:
        handle.write(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    with public.open('xb') as handle:
        handle.write(key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, public


def manifest_template(*, app_id, app_label, package_name, publisher, version='0.1.0', app_config_class='AppConfig'):
    return validate_manifest({'manifest_version': 1, 'kind': 'app', 'app_id': app_id, 'app_label': app_label, 'package_name': package_name, 'app_config': f'applications.{package_name}.apps.{app_config_class}', 'name': app_label, 'description': 'Application release contract', 'version': version, 'publisher': publisher, 'compatibility': {'skeleton': '>=0.1.0,<1.0.0', 'python': '>=3.12,<3.15', 'django': '>=5.2,<5.3'}, 'dependencies': {}, 'conflicts': {}, 'runtime': {'urls': None, 'api_urls': None, 'default_enabled': False, 'configuration_fields': [], 'navigation': [], 'documentation_root': 'docs'}, 'migrations': {'leaves': [], 'from_versions': '>=0.0.0', 'labels': [app_label]}, 'files': [], 'python_dependencies': [], 'lifecycle': {'data_retention': 'retain', 'maintenance': True, 'backup': True}})


def sign_package(path, output_path, private_key_path, *, publisher=None):
    """Inspect all hashes/structure first, then republish with an operator signature."""
    package = _validate_archive(path)
    m = package.manifest
    if publisher is not None:
        m['publisher'] = _stable_id(publisher)
    m = validate_manifest(m)
    output = Path(output_path)
    if output.exists():
        _fail('Signed output must be a new artifact.')
    key = _private_key(private_key_path)
    raw = canonical_json(m)
    try:
        with zipfile.ZipFile(io.BytesIO(package._snapshot)) as source, zipfile.ZipFile(output, 'x', compression=zipfile.ZIP_DEFLATED) as target:
            target.writestr('manifest.json', raw)
            target.writestr('manifest.sig', b64encode(key.sign(raw)))
            for item in m['files']:
                data = source.read(item['path'])
                if hashlib.sha256(data).hexdigest() != item['sha256']:
                    _fail('Payload changed during signing.')
                target.writestr(item['path'], data)
        validate_package(output, {m['publisher']: key.public_key()})
    except Exception:
        output.unlink(missing_ok=True)
        raise
    return output


def _timestamp(value):
    try:
        parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (AttributeError, ValueError) as exc:
        raise PackageError('Invalid catalog timestamp.') from exc
    if parsed.tzinfo is None:
        _fail('Catalog timestamps require an explicit timezone.')
    return parsed


def _catalog(value):
    _object(value, ('catalog_version', 'publisher', 'generation', 'expires_at', 'entries'), ('catalog_version', 'publisher', 'generation', 'expires_at', 'entries'))
    if type(value['catalog_version']) is not int or value['catalog_version'] != 1 or type(value['generation']) is not int or value['generation'] < 1:
        _fail('Invalid catalog format/generation.')
    _stable_id(value['publisher'])
    _timestamp(value['expires_at'])
    if not isinstance(value['entries'], list) or len(value['entries']) > MAX_MEMBERS:
        _fail('Invalid release catalog entries.')
    identities = set()
    for entry in value['entries']:
        fields = ('component_id', 'kind', 'version', 'channel', 'compatibility', 'artifact', 'digest', 'signature', 'publisher', 'notes', 'migrations')
        _object(entry, fields, fields)
        _stable_id(entry['component_id'])
        _stable_id(entry['publisher'])
        _stable_id(entry['channel'])
        if entry['kind'] not in ('core', 'app') or (entry['kind'] == 'core' and entry['component_id'] != 'django-skeleton'):
            _fail('Invalid catalog component kind.')
        if _version(entry['version']) != entry['version']:
            _fail('Catalog version must be normalized.')
        _compatibility(entry['compatibility'])
        _string(entry['artifact'], 2048)
        from urllib.parse import urlsplit
        url = urlsplit(entry['artifact'])
        if url.scheme:
            if url.scheme != 'https' or not url.hostname or url.username or url.password or url.fragment:
                _fail('Catalog artifacts require HTTPS or relative safe paths.')
        else:
            _path(entry['artifact'])
        if not isinstance(entry['digest'], str) or not SHA256.fullmatch(entry['digest']):
            _fail('Invalid catalog artifact digest.')
        try:
            if len(b64decode(entry['signature'], validate=True)) != 64:
                _fail('Invalid catalog artifact signature.')
        except (ValueError, TypeError) as exc:
            raise PackageError('Invalid catalog artifact signature.') from exc
        if not isinstance(entry['notes'], str) or len(entry['notes']) > 10000:
            _fail('Invalid release notes.')
        migrations = _object(entry['migrations'], ('leaves', 'from_versions', 'labels'), ('leaves', 'from_versions'))
        if not isinstance(migrations["leaves"], list) or any(
                not isinstance(leaf, str) or not re.fullmatch(r"[A-Za-z0-9_]+", leaf)
                for leaf in migrations["leaves"]):
            _fail("Invalid catalog migration leaves.")
        if len(set(migrations["leaves"])) != len(migrations["leaves"]):
            _fail("Duplicate catalog migration leaves.")
        _specifier(migrations["from_versions"])
        if "labels" in migrations:
            if not isinstance(migrations["labels"], list):
                _fail("Invalid catalog migration labels.")
            for label in migrations["labels"]:
                _identifier(label)
        identity = (entry['component_id'], entry['version'], entry['channel'])
        if identity in identities:
            _fail('Duplicate catalog release identity.')
        identities.add(identity)
    return value


def build_catalog(entries, output_path, private_key_path, *, publisher, generation, expires_at):
    catalog = _catalog({'catalog_version': 1, 'publisher': publisher, 'generation': generation, 'expires_at': expires_at, 'entries': entries})
    if _timestamp(expires_at) <= datetime.now(timezone.utc):
        _fail('Cannot publish an already expired catalog.')
    signature = b64encode(_private_key(private_key_path).sign(canonical_json(catalog))).decode('ascii')
    output = Path(output_path)
    with output.open('xb') as handle:
        handle.write(canonical_json({'catalog': catalog, 'signature': signature}))
    return output


def validate_catalog(path_or_dict, trust_store, *, minimum_generation=0, channels=('stable',), now=None):
    if isinstance(path_or_dict, dict):
        envelope = path_or_dict
    else:
        path = Path(path_or_dict)
        if path.stat().st_size > MAX_METADATA_BYTES:
            _fail('Catalog size limit exceeded.')
        envelope = _json(path.read_bytes())
    _object(envelope, ('catalog', 'signature'), ('catalog', 'signature'))
    catalog = _catalog(envelope['catalog'])
    _verify(_trusted_key(catalog['publisher'], trust_store), envelope['signature'], canonical_json(catalog))
    if _timestamp(catalog['expires_at']) <= (now or datetime.now(timezone.utc)):
        _fail('Release catalog is expired.')
    if catalog['generation'] < minimum_generation:
        _fail('Release catalog generation rollback.')
    return {**catalog, 'entries': [e for e in catalog['entries'] if e['channel'] in channels]}


def catalog_entry(package, *, artifact, channel='stable', notes=''):
    if not isinstance(package, VerifiedPackage) or package._public_key is None:
        _fail('Catalog requires trusted verified packages.')
    fresh = validate_package(package.path, {package.manifest['publisher']: package._public_key})
    if fresh.digest != package.digest or fresh.manifest != package.manifest:
        _fail('Catalog artifact changed after verification.')
    with zipfile.ZipFile(io.BytesIO(fresh._snapshot)) as archive:
        signature = archive.read('manifest.sig').decode('ascii')
    m = fresh.manifest
    return {'component_id': m.get('app_id', m.get('component_id')), 'kind': m['kind'], 'version': m['version'], 'channel': channel, 'compatibility': m['compatibility'], 'artifact': artifact, 'digest': package.digest, 'signature': signature, 'publisher': m['publisher'], 'notes': notes or m.get('release_notes', ''), 'migrations': m['migrations']}


def configure_parser(parser):
    commands = parser.add_subparsers(dest="operation", required=True)
    keys = commands.add_parser("keygen", help="Generate a private Ed25519 key (0600) and public PEM.")
    keys.add_argument("--private-key", required=True)
    keys.add_argument("--public-key", required=True)
    init = commands.add_parser("init", help="Initialize the canonical manifest without overwriting files.")
    init.add_argument("--output", required=True)
    for flag in ("app-id", "app-label", "package-name", "publisher", "app-config-class"):
        init.add_argument("--" + flag, required=True)
    init.add_argument("--version", default="0.1.0")
    build = commands.add_parser("build", help="Build a complete signed .skapp, checking docs and wheels.")
    build.add_argument("--app-root", required=True)
    build.add_argument("--manifest", required=True)
    build.add_argument("--output", required=True)
    build.add_argument("--private-key", required=True)
    build.add_argument("--wheel", action="append", default=[])
    manifest = commands.add_parser("validate-manifest")
    manifest.add_argument("path")
    for name in ("validate", "inspect"):
        command = commands.add_parser(name)
        command.add_argument("path")
        command.add_argument("--trust-store", required=name == "validate")
    sign = commands.add_parser("sign", help="Validate inventory then sign an existing app/core artifact.")
    sign.add_argument("path")
    sign.add_argument("--output", required=True)
    sign.add_argument("--private-key", required=True)
    sign.add_argument("--publisher")
    extract = commands.add_parser("extract")
    extract.add_argument("path")
    extract.add_argument("--trust-store", required=True)
    extract.add_argument("--destination")
    extract.add_argument("--wheels-destination")
    index = commands.add_parser("index", help="Build a signed expiring release index from trusted artifacts.")
    index.add_argument("artifacts", nargs="+")
    for name in ("output", "private-key", "trust-store", "publisher", "expires-at"):
        index.add_argument("--" + name, required=True)
    index.add_argument("--generation", type=int, required=True)
    index.add_argument("--channel", default="stable")
    index.add_argument("--artifact-base", default="")
    validate_index = commands.add_parser("validate-index")
    validate_index.add_argument("path")
    validate_index.add_argument("--trust-store", required=True)
    validate_index.add_argument("--minimum-generation", type=int, default=0)
    validate_index.add_argument("--channel", action="append", default=[])
    versions = commands.add_parser("versions", help="Report core provenance and declared local app versions.")
    versions.add_argument("--manifest", action="append", default=[])
    return parser


def run_command(options):
    def trust():
        path = Path(options["trust_store"])
        if path.stat().st_size > MAX_METADATA_BYTES:
            _fail("Trust store is too large.")
        store = _json(path.read_bytes())
        if not isinstance(store, dict):
            _fail("Trust store must map publishers to operator public keys.")
        # Relative PEM paths are relative to operator config, never artifact.
        result = {}
        for publisher, value in store.items():
            if isinstance(value, str) and "-----BEGIN" not in value:
                value = str(path.parent / value) if not Path(value).is_absolute() else value
            elif isinstance(value, dict):
                value = dict(value)
                public = value.get("public_key")
                if isinstance(public, str) and "-----BEGIN" not in public:
                    value["public_key"] = str(path.parent / public) if not Path(public).is_absolute() else public
            result[publisher] = value
        return result
    operation = options["operation"]
    if operation == "keygen":
        private, public = initialize_keys(options["private_key"], options["public_key"])
        return {"private_key": str(private), "public_key": str(public)}
    if operation == "init":
        value = manifest_template(**{key: options[key] for key in ("app_id", "app_label", "package_name", "publisher", "version", "app_config_class")})
        with Path(options["output"]).open("xb") as handle:
            handle.write(canonical_json(value))
        return value
    if operation == "build":
        path = build_package(options["app_root"], options["manifest"], options["output"], options["private_key"], options["wheel"])
        return {"artifact": str(path)}
    if operation == "validate-manifest":
        return load_manifest(options["path"])
    if operation in ("validate", "inspect"):
        return inspect_package(options["path"], trust() if options.get("trust_store") else None)
    if operation == "sign":
        path = sign_package(options["path"], options["output"], options["private_key"], publisher=options.get("publisher"))
        return {"artifact": str(path)}
    if operation == "extract":
        roots = extract_package(validate_package(options["path"], trust()), options.get("destination"), wheels_destination=options.get("wheels_destination"))
        return {key: str(value) for key, value in roots.items()}
    if operation == "index":
        from urllib.parse import urljoin
        store = trust()
        entries = []
        for artifact in options["artifacts"]:
            package = validate_package(artifact, store)
            base = options["artifact_base"]
            reference = urljoin(base.rstrip("/") + "/", Path(artifact).name) if base else Path(artifact).name
            entries.append(catalog_entry(package, artifact=reference, channel=options["channel"]))
        path = build_catalog(entries, options["output"], options["private_key"], publisher=options["publisher"], generation=options["generation"], expires_at=options["expires_at"])
        return {"index": str(path), "entries": len(entries)}
    if operation == "validate-index":
        return validate_catalog(options["path"], trust(), minimum_generation=options["minimum_generation"], channels=options["channel"] or ("stable",))
    if operation == "versions":
        return {"core": core_version(), "apps": [{"app_id": m["app_id"], "version": m["version"], "origin": "project-local", "manifest": str(path)} for path in options["manifest"] for m in (load_manifest(path),) if m["kind"] == "app"]}
    _fail("Unknown package operation.")


def main(argv=None):
    import argparse
    parser = configure_parser(argparse.ArgumentParser(description=__doc__))
    options = vars(parser.parse_args(argv))
    try:
        result = run_command(options)
    except (PackageError, OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
