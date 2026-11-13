"""System checks of the core, one module per subject (#252).

Importing the package registers every check once. ``skeleton.system.checks`` stays the single
import point: the names below are re-exported for the core and for derived projects.
"""

from . import (
    contract,  # noqa: F401 - registers its checks
    encryption,  # noqa: F401 - registers its checks
    environment,  # noqa: F401 - registers its checks
    extensions,  # noqa: F401 - registers its checks
    frozen,  # noqa: F401 - registers its checks
    schema,  # noqa: F401 - registers its checks
    service_api,  # noqa: F401 - registers its checks
    sqltext,  # noqa: F401 - registers its checks
)
from .contract import infrastructure_identifier_checks, record_contract_checks
from .encryption import KEYRINGS, encrypted_secret_checks, stored_secret_key_checks, stored_secrets_without_keys
from .environment import POSTGRESQL_ENGINE, postgresql_only_checks, shared_cache_checks
from .extensions import extension_consistency_checks, undeclared_migration_dependencies, undeclared_relations
from .frozen import Capture, _expected_sql, _frozen_operations, expected_sql, frozen_operations
from .schema import _check_lifecycle, check_deployed_schema, expected_policies
from .service_api import _boundary_providers, import_boundary_checks, service_api_checks
from .sqltext import _canonical, _sql_tokens, _trigger_tokens

__all__ = [
    "Capture",
    "KEYRINGS",
    "POSTGRESQL_ENGINE",
    "_boundary_providers",
    "_canonical",
    "_check_lifecycle",
    "_expected_sql",
    "_frozen_operations",
    "_sql_tokens",
    "_trigger_tokens",
    "check_deployed_schema",
    "encrypted_secret_checks",
    "expected_policies",
    "expected_sql",
    "extension_consistency_checks",
    "frozen_operations",
    "import_boundary_checks",
    "infrastructure_identifier_checks",
    "postgresql_only_checks",
    "record_contract_checks",
    "service_api_checks",
    "shared_cache_checks",
    "stored_secret_key_checks",
    "stored_secrets_without_keys",
    "undeclared_migration_dependencies",
    "undeclared_relations",
]
