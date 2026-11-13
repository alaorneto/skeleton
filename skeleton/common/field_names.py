"""The single source of the field lists every surface and check uses (#310).

Free of Django so the packaging CLI and the deployment agent import it without settings.
"""

# Inherited by every own table (SystemRecord); never externally writable. The order is the order
# of the projected columns.
SYSTEM_FIELD_NAMES: tuple[str, ...] = (
    "sysid",
    "created_at",
    "updated_at",
    "created_by",
    "updated_by",
    "deleted_at",
    "deleted_by",
    "revision",
)
# Access control of SecuredRecord; changed only by authorized access management.
ACCESS_FIELD_NAMES: tuple[str, ...] = ("owner", "visibility", "owning_group")
