"""Historical import path of the REST shapes; the code lives in ``skeleton.records`` and ``skeleton.audit``."""

from skeleton.access.resources import DEDICATED, LINKS, dedicated_model  # noqa: F401
from skeleton.audit.presentation import audit_data  # noqa: F401
from skeleton.records.dedicated import (  # noqa: F401
    DEDICATED_EXTRA,
    DEDICATED_REQUIRED,
    dedicated_data,
    dedicated_input,
    dedicated_required,
)
