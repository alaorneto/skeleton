"""Tables, gateway operations, plan metrics and extensions of the ledger, registered at startup."""

from django.utils.translation import gettext_lazy


def register_ledger_tables() -> None:
    from skeleton.service_api.billing import register_quota_metric
    from skeleton.system.record_operations import RecordOperations, register_record_operations
    from skeleton.system.registry import TableSpec, register

    from . import services
    from .models import Book

    register(
        TableSpec(
            model=Book,
            description=gettext_lazy(
                "Livros contábeis da organização, cada um numa moeda e num regime; tudo do livro herda a autorização dele."
            ),
            label=gettext_lazy("Livros"),
            scope="organization",
            read_fields=("name", "currency", "basis", "is_default", "locked_until", "owner", "visibility"),
            # Currency, basis and the period lock change only through the ledger services.
            write_fields=("name",),
            sensitive_fields=(),
            expose_api=True,
        )
    )
    register_record_operations(
        "skeleton_ledger.book",
        RecordOperations(
            create=services.create_book_record,
            update=services.update_book_record,
            delete=services.delete_book_record,
            restore=services.restore_book_record,
        ),
    )
    register_quota_metric("skeleton_ledger.books", services.book_count)
