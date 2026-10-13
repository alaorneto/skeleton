"""Primitive migration-owned contracts, exported by the separate migrator.

Runtime schema verification does not import retained migration packages.
"""
import hashlib
import json
from pathlib import Path
import re

from skeleton.common.lifecycle_operation import InstallUserLifecycleGuard
from skeleton.common.migration_operations import InstallRecordInfrastructure
from skeleton.common.technical_operations import GrantTechnicalTablePrivileges


_LABEL = re.compile(r'[a-zA-Z_][a-zA-Z_0-9]*\Z')


def export_inventory():
    from .checks import _frozen_operations
    records, technical, lifecycle = _frozen_operations(from_migrations=True)
    return {
        'format': 1,
        'records': [{'app_label': operation.app_label, 'model_name': operation.model_name,
                     'specification': operation.specification}
                    for _, operation in sorted(records.items())],
        'technical': {name: sorted(actions) for name, actions in sorted(technical.items())},
        'lifecycle': lifecycle is not None,
    }


def load_inventory(reference):
    if not isinstance(reference, dict) or set(reference) != {'path', 'sha256'}:
        raise ValueError('Frozen infrastructure requires an immutable digest reference.')
    with Path(reference['path']).open('rb') as stream:
        raw = stream.read(8 * 1024 * 1024 + 1)
    if len(raw) > 8 * 1024 * 1024 or hashlib.sha256(raw).hexdigest() != reference['sha256']:
        raise ValueError('Frozen infrastructure inventory digest mismatch.')
    document = json.loads(raw)
    if (not isinstance(document, dict) or set(document) != {'format', 'records', 'technical', 'lifecycle'}
            or document['format'] != 1 or not isinstance(document['records'], list)
            or not isinstance(document['technical'], dict) or type(document['lifecycle']) is not bool):
        raise ValueError('Invalid frozen infrastructure inventory.')
    records = {}
    for item in document['records']:
        if not isinstance(item, dict) or set(item) != {'app_label', 'model_name', 'specification'}:
            raise ValueError('Invalid frozen record operation.')
        app, model = item['app_label'], item['model_name']
        if not isinstance(app, str) or not isinstance(model, str) or not _LABEL.fullmatch(app) or not _LABEL.fullmatch(model):
            raise ValueError('Invalid frozen model identity.')
        if not isinstance(item['specification'], dict):
            raise ValueError('Invalid frozen record specification.')
        key = f'{app}.{model.lower()}'
        if key in records:
            raise ValueError('Duplicate frozen record operation.')
        records[key] = InstallRecordInfrastructure(app, model, item['specification'])
    if any(not isinstance(name, str) or not _LABEL.fullmatch(name) for name in document['technical']):
        raise ValueError('Invalid frozen technical table.')
    operation = GrantTechnicalTablePrivileges(document['technical'])
    return records, {name: set(actions) for name, actions in operation.tables.items()}, InstallUserLifecycleGuard() if document['lifecycle'] else None
