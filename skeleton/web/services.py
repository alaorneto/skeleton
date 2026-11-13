"""Historical import path of the record gateway; the code lives in ``skeleton.records.services``."""

from skeleton.records.services import (  # noqa: F401
    ACCESS_FIELDS,
    SYSTEM_FIELDS,
    assert_fields,
    change_record_access,
    coerce_fields,
    create_record,
    delete_record,
    exposed_spec,
    filter_records,
    readable_specs,
    record_data,
    record_queryset,
    records_data,
    require_access_management,
    restore_record,
    update_record,
)
