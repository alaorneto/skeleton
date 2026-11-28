"""Read-only report of invoices whose storage overage diverges from period-based recalculation (#882)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

from django.core.management.base import BaseCommand, CommandError

from skeleton.billing.models import Invoice, InvoiceLine
from skeleton.billing.quota import storage_overage_charge
from skeleton.tenancy.context import system_context
from skeleton.tenancy.models import Organization


def _parse_since(raw: str) -> datetime:
    try:
        day = date.fromisoformat(raw.strip())
    except ValueError as exc:
        raise CommandError(f"Data inválida para --since (use YYYY-MM-DD): {raw!r}.") from exc
    return datetime(day.year, day.month, day.day, tzinfo=UTC)


def find_divergent_overages(*, since: datetime) -> list[dict[str, Any]]:
    """Inspect invoices issued since ``since`` across active organizations without modifying data."""
    with system_context(reason="command:billing_overage_report"):
        organizations = list(
            Organization.all_objects.filter(deleted_at__isnull=True).order_by("slug").values_list("pk", "slug")
        )
    findings: list[dict[str, Any]] = []
    for organization_id, slug in organizations:
        with system_context(reason="command:billing_overage_report", organization_id=organization_id):
            invoices = (
                Invoice.objects.filter(subscription__isnull=False, issued_at__gte=since)
                .select_related("subscription")
                .prefetch_related("lines")
                .order_by("issued_at", "sysid")
            )
            for invoice in invoices:
                overage_rows = [
                    line for line in invoice.lines.all() if line.kind == InvoiceLine.Kind.OVERAGE and not line.metric
                ]
                if not overage_rows:
                    continue
                closing_start = overage_rows[0].period_start
                closing_end = overage_rows[0].period_end
                if closing_start is None or closing_end is None:
                    continue
                billed = sum(line.amount for line in overage_rows)
                _, _, expected = storage_overage_charge(invoice.subscription, closing_start, closing_end)
                if billed != expected:
                    findings.append(
                        {
                            "organization_id": str(organization_id),
                            "organization_slug": slug,
                            "invoice_id": str(invoice.pk),
                            "invoice_number": invoice.number,
                            "currency": invoice.currency,
                            "period_start": closing_start.isoformat(),
                            "period_end": closing_end.isoformat(),
                            "billed_overage": billed,
                            "expected_overage": expected,
                            "difference": billed - expected,
                        }
                    )
    return findings


class Command(BaseCommand):
    help = "Lista faturas emitidas desde --since cujo excedente de armazenamento diverge do recálculo por vigência."

    def add_arguments(self, parser):
        parser.add_argument("--since", required=True, help="Data inicial UTC (YYYY-MM-DD).")
        parser.add_argument("--format", choices=("text", "json"), default="text", dest="output_format")

    def handle(self, *args, **options):
        since = _parse_since(options["since"])
        findings = find_divergent_overages(since=since)
        if options["output_format"] == "json":
            self.stdout.write(json.dumps(findings, ensure_ascii=False))
            return
        if not findings:
            self.stdout.write("Nenhuma divergência de excedente de armazenamento encontrada.")
            return
        for row in findings:
            self.stdout.write(
                f"{row['organization_slug']} {row['invoice_number']} "
                f"billed={row['billed_overage']} expected={row['expected_overage']} "
                f"diff={row['difference']} {row['currency']}"
            )
