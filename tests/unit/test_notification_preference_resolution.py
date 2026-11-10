"""Resolution of notification preferences without a database (#53)."""

from types import SimpleNamespace

from skeleton.notifications.preferences import resolve
from skeleton.notifications.registry import NotificationType

KIND = NotificationType("tasks.assigned", "Tarefa", default_channels=("in_app", "email"))
MANDATORY = NotificationType("security.alert", "Alerta", mandatory=True)


def row(mode, hour=8):
    return SimpleNamespace(mode=mode, digest_hour=hour)


def test_without_rows_the_type_defaults_apply():
    assert resolve(KIND, "in_app", {}).mode == "immediate"
    assert resolve(KIND, "email", {}).mode == "immediate"
    assert resolve(KIND, "push", {}).mode == "off"
    assert resolve(KIND, "push", {}).source == "type"


def test_own_row_wins_over_the_default_row():
    rows = {("*", "email"): row("daily", 7), ("tasks.assigned", "email"): row("off")}
    assert (resolve(KIND, "email", rows).mode, resolve(KIND, "email", rows).source) == ("off", "own")
    other = NotificationType("tasks.closed", "Fechada", default_channels=("in_app",))
    effective = resolve(other, "email", rows)
    assert (effective.mode, effective.digest_hour, effective.source) == ("daily", 7, "default")


def test_mandatory_type_always_reaches_the_inbox():
    rows = {("*", "in_app"): row("off"), ("security.alert", "in_app"): row("off")}
    effective = resolve(MANDATORY, "in_app", rows)
    assert (effective.mode, effective.source) == ("immediate", "mandatory")
    assert resolve(MANDATORY, "email", rows).mode == "off"
