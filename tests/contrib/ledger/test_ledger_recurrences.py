"""Recurring titles (#547): occurrence dates, the daily job, end, pause and resume, limits and A/B."""

import datetime as dt
import logging
from types import SimpleNamespace

import pytest
from django.utils import timezone

from skeleton.contrib.ledger import recurrence
from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.service_api import Conflict, InvalidInput, NotFound
from skeleton.tenancy.context import system_context


def rule(frequency, start, *, interval=1, day_of_month=None):
    return SimpleNamespace(frequency=frequency, start_date=start, interval=interval, day_of_month=day_of_month)


def dates(item, count):
    due, found = recurrence.first_due(item), []
    for _ in range(count):
        found.append(due)
        due = recurrence.next_date(item, due)
    return found


def test_the_next_dates_of_each_frequency():
    d = dt.date
    assert dates(rule("weekly", d(2027, 1, 4), interval=2), 3) == [d(2027, 1, 4), d(2027, 1, 18), d(2027, 2, 1)]
    assert dates(rule("monthly", d(2027, 1, 31)), 4) == [d(2027, 1, 31), d(2027, 2, 28), d(2027, 3, 31), d(2027, 4, 30)]
    assert dates(rule("monthly", d(2027, 1, 10), day_of_month=5), 2) == [d(2027, 2, 5), d(2027, 3, 5)]
    assert dates(rule("monthly", d(2027, 1, 2), day_of_month=31), 3) == [d(2027, 1, 31), d(2027, 2, 28), d(2027, 3, 31)]
    assert dates(rule("yearly", d(2028, 2, 29)), 5) == [
        d(2028, 2, 29),
        d(2029, 2, 28),
        d(2030, 2, 28),
        d(2031, 2, 28),
        d(2032, 2, 29),
    ]
    assert dates(rule("monthly", d(2027, 11, 30), interval=3), 2) == [d(2027, 11, 30), d(2028, 2, 29)]


# --------------------------------------------------------------------------- integration


def call(scenario, operation, organization=None, **kwargs):
    return scenario.caller(
        lambda actor: getattr(LedgerService(), operation)(actor=actor, **kwargs), organization=organization
    )()


def run(organization, today):
    with system_context(reason="test:ledger-recurrences", organization_id=organization.pk):
        return recurrence.generate_due(today=today)


def due_dates(scenario, organization=None):
    page = call(scenario, "titles", organization=organization, page_size=100)
    return sorted(item.due_date for item in page.items)


@pytest.fixture
def books(scenario):
    for organization in (scenario.a, scenario.b):
        call(scenario, "create_default_book", organization=organization)
        call(scenario, "apply_chart", organization=organization)


def create(scenario, key="r", organization=None, **changes):
    values = {
        "direction": "payable",
        "amount": 250000,
        "description": "Aluguel",
        "category_key": "ledger.expenses",
        "frequency": "monthly",
        "start_date": dt.date(2027, 1, 31),
        "idempotency_key": key,
        "counterparty_name": "Imobiliária Exemplo",
        "days_ahead": 10,
        **changes,
    }
    return call(scenario, "create_recurrence", organization=organization, **values)


def test_the_job_generates_up_to_days_ahead_once_per_occurrence(scenario, books):
    created = create(scenario)
    assert created.next_due == dt.date(2027, 1, 31)
    assert run(scenario.a, dt.date(2027, 1, 1)) == 0  # 31 Jan is beyond 10 days ahead.
    assert run(scenario.a, dt.date(2027, 1, 21)) == 1
    assert run(scenario.a, dt.date(2027, 1, 21)) == 0  # A repeated run creates nothing.
    assert run(scenario.a, dt.date(2027, 3, 25)) == 2  # A late scheduler catches up: 28 Feb and 31 Mar.
    assert due_dates(scenario) == [dt.date(2027, 1, 31), dt.date(2027, 2, 28), dt.date(2027, 3, 31)]
    page = call(scenario, "titles", page_size=100)
    assert {item.counterparty_name for item in page.items} == {"Imobiliária Exemplo"}
    assert create(scenario).sysid == created.sysid  # Idempotent creation.
    [listed] = call(scenario, "recurrences")
    assert (listed.generated_count, listed.next_due) == (3, dt.date(2027, 4, 30))


def test_end_date_and_occurrence_limit_end_the_rule(scenario, books):
    create(scenario, key="end", end_date=dt.date(2027, 2, 28))
    create(scenario, key="max", max_occurrences=2, start_date=dt.date(2027, 1, 15), day_of_month=15)
    assert run(scenario.a, dt.date(2027, 12, 31)) == 4
    rules = {item.description: item for item in call(scenario, "recurrences")}
    assert len(due_dates(scenario)) == 4
    assert all(item.ended and item.next_due is None for item in rules.values())
    assert run(scenario.a, dt.date(2028, 1, 31)) == 0


def test_pause_stops_and_resume_skips_what_fell_due_meanwhile(scenario, books):
    today = timezone.localdate()
    start = today - dt.timedelta(days=60)
    created = create(scenario, frequency="weekly", start_date=start, days_ahead=0)
    paused = call(scenario, "pause", recurrence_id=created.sysid, expected_revision=created.revision)
    assert not paused.is_active
    assert run(scenario.a, today) == 0
    with pytest.raises(Conflict) as error:
        call(scenario, "resume", recurrence_id=created.sysid, expected_revision=created.revision)
    assert error.value.code == "stale_revision"
    resumed = call(scenario, "resume", recurrence_id=created.sysid, expected_revision=paused.revision)
    assert resumed.is_active and resumed.next_due >= today
    assert run(scenario.a, today) <= 1  # Only an occurrence falling due today, never the 8 weeks behind.
    assert all(due >= today for due in due_dates(scenario))


def test_a_change_applies_to_the_next_occurrences_only(scenario, books):
    created = create(scenario, days_ahead=0)
    run(scenario.a, dt.date(2027, 1, 31))
    changed = call(
        scenario,
        "update_recurrence",
        recurrence_id=created.sysid,
        data={"amount": 300000, "description": "Aluguel reajustado"},
        expected_revision=call(scenario, "recurrences")[0].revision,
    )
    assert changed.amount == 300000
    run(scenario.a, dt.date(2027, 2, 28))
    page = call(scenario, "titles", page_size=100)
    assert [(item.amount, item.description) for item in page.items] == [
        (250000, "Aluguel"),
        (300000, "Aluguel reajustado"),
    ]
    with pytest.raises(InvalidInput):
        call(
            scenario,
            "update_recurrence",
            recurrence_id=created.sysid,
            data={"frequency": "weekly"},
            expected_revision=1,
        )


def test_extending_the_end_resumes_a_rule_that_ended(scenario, books):
    created = create(scenario, max_occurrences=1, days_ahead=0)
    run(scenario.a, dt.date(2027, 3, 31))
    ended = call(scenario, "recurrences")[0]
    assert ended.ended
    call(
        scenario,
        "update_recurrence",
        recurrence_id=created.sysid,
        data={"max_occurrences": 2},
        expected_revision=ended.revision,
    )
    assert run(scenario.a, dt.date(2027, 3, 31)) == 1
    assert due_dates(scenario) == [dt.date(2027, 1, 31), dt.date(2027, 2, 28)]


def test_the_run_limit_bounds_a_late_scheduler(scenario, books, monkeypatch):
    monkeypatch.setattr(recurrence, "RUN_LIMIT", 3)
    create(scenario, frequency="weekly", start_date=dt.date(2027, 1, 4), days_ahead=0)
    assert run(scenario.a, dt.date(2027, 6, 30)) == 3
    assert run(scenario.a, dt.date(2027, 6, 30)) == 3
    assert len(due_dates(scenario)) == 6


def test_one_failing_rule_keeps_the_others_and_logs_identifiers_only(scenario, books, caplog):
    accounts = {item.code: item for item in call(scenario, "accounts", page_size=100).items}
    broken = create(scenario, key="broken", days_ahead=0)
    create(scenario, key="fine", direction="receivable", category_key="ledger.revenue", days_ahead=0)
    expense = accounts["5.1"]
    call(scenario, "update_account", account_id=expense.sysid, expected_revision=expense.revision, is_active=False)
    with caplog.at_level(logging.WARNING, logger="skeleton.contrib.ledger"):
        assert run(scenario.a, dt.date(2027, 1, 31)) == 1
    [record] = [item for item in caplog.records if item.getMessage() == "ledger.recurrence_failed"]
    assert record.target_sysid == str(broken.sysid)
    assert "Aluguel" not in str(record.__dict__) and "Imobiliária" not in str(record.__dict__)
    rules = {item.sysid: item for item in call(scenario, "recurrences")}
    assert rules[broken.sysid].generated_count == 0  # Retried on the next run.


def test_tenant_isolation_and_authorization(scenario, books):
    created = create(scenario)
    assert run(scenario.b, dt.date(2027, 1, 31)) == 0
    assert call(scenario, "recurrences", organization=scenario.b) == ()
    with pytest.raises(NotFound):
        call(
            scenario, "pause", organization=scenario.b, recurrence_id=created.sysid, expected_revision=created.revision
        )
    with scenario.as_denied() as actor, pytest.raises(NotFound):
        LedgerService().recurrences(actor=actor)


def test_a_disabled_ledger_generates_nothing_and_is_not_scheduled(scenario, books):
    from skeleton.jobs.models import Job
    from skeleton.jobs.periodic import schedule_due
    from skeleton.system.applications import application_state, configure_application
    from skeleton.testing.service_api import acting_as

    create(scenario, days_ahead=0)

    def configure(admin, organization, enabled):
        with acting_as(admin, organization):
            state = application_state("skeleton_ledger")
            configure_application(
                "skeleton_ledger",
                user=admin,
                enabled=enabled,
                configuration=state["configuration"],
                revision=state["revision"],
            )

    configure(scenario.admin_a, scenario.a, False)
    assert run(scenario.a, dt.date(2027, 1, 31)) == 0
    schedule_due(now=timezone.now())
    with system_context(reason="test:ledger-recurrences-jobs"):
        organizations = set(
            Job.all_objects.filter(job_type=recurrence.JOB).values_list("job_organization_id", flat=True)
        )
    assert scenario.a.pk not in organizations and scenario.b.pk in organizations
    configure(scenario.admin_a, scenario.a, True)
    assert run(scenario.a, dt.date(2027, 1, 31)) == 1


def test_the_worker_cancels_the_job_of_an_inactive_organization(scenario, books):
    from skeleton.jobs.models import Job
    from skeleton.jobs.services import enqueue
    from skeleton.tenancy.models import Organization
    from skeleton.testing.jobs import run_pending_jobs

    create(scenario, days_ahead=0)
    with system_context(reason="test:ledger-recurrences-enqueue", organization_id=scenario.a.pk):
        receipt = enqueue(recurrence.JOB, organization_id=scenario.a.pk, payload={})
    with system_context(reason="test:ledger-recurrences-deactivate"):
        organization = Organization.objects.get(pk=scenario.a.pk)
        organization.is_active = False
        organization.save(update_fields={"is_active"})
    run_pending_jobs()
    with system_context(reason="test:ledger-recurrences-jobs"):
        job = Job.all_objects.get(pk=receipt.id)
    assert (job.state, job.last_error_code) == ("cancelled", "organization_inactive")


def test_invalid_schedules_are_refused(scenario, books):
    for changes in (
        {"frequency": "daily"},
        {"interval": 0},
        {"day_of_month": 32},
        {"frequency": "weekly", "day_of_month": 3},
        {"end_date": dt.date(2026, 1, 1)},
        {"max_occurrences": 0},
        {"days_ahead": 400},
    ):
        with pytest.raises(InvalidInput):
            create(scenario, key=str(changes), **changes)
