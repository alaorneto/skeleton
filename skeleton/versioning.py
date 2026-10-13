"""Release identities and compatibility, usable without Django configuration."""
from importlib import metadata
from pathlib import Path
import re
import tomllib

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version


def normalize_version(value):
    """Accept SemVer (including -rc.N) and return its PEP 440 equivalent."""
    if not isinstance(value, str) or not value:
        raise ValueError('Version must be a nonempty string.')
    converted = re.sub(r'-(alpha|beta|rc)\.?([0-9]+)', lambda m: {'alpha': 'a', 'beta': 'b', 'rc': 'rc'}[m[1]] + m[2], value)
    try:
        parsed = Version(converted)
    except InvalidVersion as exc:
        raise ValueError('Invalid component version.') from exc
    if len(parsed.release) != 3 or parsed.epoch:
        raise ValueError('Component versions require major.minor.patch.')
    return str(parsed)


def validate_specifier(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('Compatibility requires an explicit version range.')
    try:
        return str(SpecifierSet(value))
    except InvalidSpecifier as exc:
        raise ValueError('Invalid compatibility range.') from exc


def version_satisfies(version, specifier):
    return Version(str(version)) in SpecifierSet(validate_specifier(specifier))


def core_version():
    """Installed distribution metadata is authoritative; checkout is explicitly source."""
    try:
        distribution = metadata.distribution('django-skeleton')
    except metadata.PackageNotFoundError:
        distribution = None
    module = Path(__file__).resolve()
    if distribution is not None:
        installed = Path(distribution.locate_file('skeleton/versioning.py')).resolve()
        if installed == module:
            return {'version': normalize_version(distribution.version), 'origin': 'installed'}
    project = module.parent.parent / 'pyproject.toml'
    if project.is_file():
        data = tomllib.loads(project.read_text(encoding='utf-8'))
        if data.get('project', {}).get('name') == 'django-skeleton':
            return {'version': normalize_version(data['project']['version']), 'origin': 'source'}
    return {'version': None, 'origin': 'unknown'}
