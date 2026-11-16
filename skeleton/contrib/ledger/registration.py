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
    from .models import Account, AccountBinding, CostCenter

    for model, label, description, fields in (
        (
            Account,
            gettext_lazy("Contas"),
            gettext_lazy("Plano de contas de cada livro; a autorização vem do livro."),
            ("book", "code", "name", "kind", "parent", "is_group", "is_active"),
        ),
        (
            AccountBinding,
            gettext_lazy("Vínculos de chaves de conta"),
            gettext_lazy("Qual conta do livro cada chave registrada em código usa."),
            ("book", "key", "account"),
        ),
        (
            CostCenter,
            gettext_lazy("Centros de custo"),
            gettext_lazy("Centros de custo de cada livro."),
            ("book", "code", "name", "is_active"),
        ),
    ):
        register(
            TableSpec(
                model=model,
                description=description,
                label=label,
                scope="organization",
                read_fields=fields,
                write_fields=(),
                sensitive_fields=(),
                expose_api=False,
            )
        )
    from .registry import register_base_keys

    register_base_keys()
