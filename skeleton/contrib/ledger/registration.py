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
    from skeleton.system.registry import SealPolicy

    from .models import Entry, EntryLine

    register(
        TableSpec(
            model=Entry,
            description=gettext_lazy("Lançamentos em partidas dobradas, selados ao nascer; corrigidos só por estorno."),
            label=gettext_lazy("Lançamentos"),
            scope="organization",
            read_fields=("book", "year", "number", "date", "description", "kind", "rule", "reverses", "total"),
            write_fields=(),
            sensitive_fields=(),
            expose_api=False,
            seal=SealPolicy(attachments="open", comments="open"),
        )
    )
    register(
        TableSpec(
            model=EntryLine,
            description=gettext_lazy("Partidas dos lançamentos, congeladas com o selo do lançamento."),
            label=gettext_lazy("Partidas"),
            scope="organization",
            read_fields=(
                "entry",
                "account",
                "date",
                "debit",
                "credit",
                "memo",
                "cost_center",
                "party_ref",
                "party_kind",
            ),
            write_fields=(),
            sensitive_fields=(),
            expose_api=False,
        )
    )
    from .models import RecurringTitle, Settlement, Title

    register(
        TableSpec(
            model=RecurringTitle,
            description=gettext_lazy("Recorrências que geram títulos a pagar e a receber, por um job diário."),
            label=gettext_lazy("Recorrências"),
            scope="organization",
            read_fields=(
                "book",
                "direction",
                "description",
                "category_account",
                "cost_center",
                "amount",
                "frequency",
                "interval",
                "day_of_month",
                "start_date",
                "end_date",
                "max_occurrences",
                "next_due",
                "generated_count",
                "days_ahead",
                "is_active",
            ),
            write_fields=(),
            sensitive_fields=(),
            personal_fields=("counterparty_name",),
            expose_api=False,
        )
    )

    register(
        TableSpec(
            model=Title,
            description=gettext_lazy(
                "Títulos a pagar e a receber dos livros; a autorização vem do livro ou da origem."
            ),
            label=gettext_lazy("Títulos"),
            scope="organization",
            read_fields=(
                "book",
                "direction",
                "description",
                "category_account",
                "cost_center",
                "amount",
                "amount_settled",
                "issue_date",
                "due_date",
                "status",
                "installment_number",
                "installment_count",
            ),
            write_fields=(),
            sensitive_fields=(),
            # The counterparty name is personal data: readable by who reads the book, redacted in the audit.
            personal_fields=("counterparty_name",),
            expose_api=False,
        )
    )
    register(
        TableSpec(
            model=Settlement,
            description=gettext_lazy("Baixas de títulos (recebimentos e pagamentos) e o lançamento de cada uma."),
            label=gettext_lazy("Baixas"),
            scope="organization",
            read_fields=("title", "date", "amount", "discount", "interest", "fee", "payment_account", "method"),
            write_fields=(),
            sensitive_fields=(),
            expose_api=False,
        )
    )
    from .registry import register_base_keys

    register_base_keys()
