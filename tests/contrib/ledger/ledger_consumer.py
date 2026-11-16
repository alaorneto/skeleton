"""The consuming app of the ledger tests (#545, #550): the notes of the example app are the source table.

Imported by the rule and scenario tests, so the keys and rules are registered once per process.
"""

from skeleton.contrib.ledger.service_api import (
    AccountKey,
    Key,
    Param,
    PostingRule,
    RuleLine,
    register_account_key,
    register_posting_rule,
)

NOTE = "example_notes.note"
RULE = "example_notes.service_received"
register_account_key(AccountKey(code="example_notes.service_revenue", label="Receita de serviços", kind="revenue"))
register_account_key(AccountKey(code="example_notes.unbound", label="Sem vínculo", kind="revenue"))
register_posting_rule(
    PostingRule(
        code=RULE,
        label="Serviço recebido",
        source_table_key=NOTE,
        source_action="update",
        lines=(
            RuleLine(side="debit", account=Param("payment_account"), amount="total"),
            RuleLine(side="debit", account=Key("ledger.discounts_given"), amount="discount"),
            RuleLine(side="credit", account=Key("example_notes.service_revenue"), amount="gross"),
        ),
    )
)
register_posting_rule(
    PostingRule(
        code="example_notes.unbound_rule",
        label="Sem vínculo",
        source_table_key=NOTE,
        source_action="update",
        lines=(
            RuleLine(side="debit", account=Param("payment_account"), amount="total"),
            RuleLine(side="credit", account=Key("example_notes.unbound"), amount="total"),
        ),
    )
)
