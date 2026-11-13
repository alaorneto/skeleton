"""Token engine of the flows (docs/flows.md, "Execução" and "Concorrência").

An ``Execution`` works on one **locked** ``FlowInstance`` row: every command takes
``SELECT ... FOR UPDATE`` on the instance before reading runs or tasks, so two branches that
finish at the same moment are processed one after the other and a join fires exactly once.
The lock order is always instance, then task.

The engine reads the frozen ``FlowDefinition``, never the code that declared the flow.
Gateways, joins, start and end are resolved here; the kinds that wait for something (tasks,
approvals, services, timers, waits) plug in through ``register_behavior``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from django.utils import timezone

from . import expressions
from .errors import FlowFailure
from .models import FlowActivity, FlowInstance, FlowStepRun, FlowSubmission
from .registry import FlowContext, get_condition
from .spec import FlowSpec, Step, Transition

MAX_ACTIVATIONS = 500
OPEN_RUN_STATES = ("active", "waiting")
_specs: dict[str, FlowSpec] = {}


def _noop(execution: Execution, run: FlowStepRun, step: Step) -> None:
    return None


@dataclass(frozen=True)
class Behavior:
    """How a waiting step kind enters, is cancelled and is rescheduled after a pause."""

    enter: Callable[[Execution, FlowStepRun, Step], None]
    cancel: Callable[[Execution, FlowStepRun, Step], None] = _noop
    resume: Callable[[Execution, FlowStepRun, Step], None] = _noop


_behaviors: dict[str, Behavior] = {}


def register_behavior(kind: str, behavior: Behavior) -> None:
    _behaviors[kind] = behavior


def specification_of(definition: Any) -> FlowSpec:
    """The frozen specification of a definition row, parsed once per digest."""
    spec = _specs.get(definition.digest)
    if spec is None:
        spec = FlowSpec.from_json(definition.specification)
        if len(_specs) > 512:
            _specs.clear()
        _specs[definition.digest] = spec
    return spec


def lock_instance(instance_id: Any) -> FlowInstance | None:
    """The instance row under ``FOR UPDATE``: the single serialization point of every command."""
    return FlowInstance.objects.select_for_update().select_related("definition").filter(pk=instance_id).first()


class Execution:
    def __init__(self, instance: FlowInstance) -> None:
        self.instance = instance
        self.spec = specification_of(instance.definition)
        self.now = timezone.now()
        self.moments: list[str] = []
        self._dirty: set[str] = set()
        self._outcomes: dict[str, str] | None = None
        self._activations = 0

    # Instance bookkeeping.
    def touch(self, **changes: Any) -> None:
        for name, value in changes.items():
            setattr(self.instance, name, value)
            self._dirty.add(name)

    def save(self) -> None:
        if self._dirty:
            self.instance.save(update_fields=set(self._dirty))
            self._dirty.clear()

    def finish(self) -> None:
        """Persist the instance and publish the lifecycle events of this command."""
        from .events import publish_moment

        self.save()
        moments, self.moments = self.moments, []
        for moment in moments:
            publish_moment(self, moment)
        if self.instance.parent_run_id is not None and any(
            item in ("completed", "failed", "cancelled") for item in moments
        ):
            from .subflows import notify_parent

            notify_parent(self.instance)  # A subflow ended: its parent learns it through a job (#412).

    def log(
        self,
        kind: str,
        *,
        step_key: str = "",
        run: FlowStepRun | None = None,
        task: Any = None,
        detail: Mapping[str, Any] | None = None,
        comment: str = "",
    ) -> None:
        self.touch(activity_count=self.instance.activity_count + 1)
        FlowActivity(
            organization_id=self.instance.organization_id,
            instance=self.instance,
            sequence=self.instance.activity_count,
            kind=kind,
            step_key=step_key or (run.step_key if run is not None else ""),
            step_run=run,
            task=task,
            detail=dict(detail or {}),
            comment=comment,
        ).save()

    # Data.
    def apply_values(self, values: Mapping[str, Any], sensitive: Mapping[str, Any]) -> None:
        if values:
            self.touch(data={**self.instance.data, **values})
        if sensitive:
            self.touch(sensitive_data={**self.instance.sensitive_data, **sensitive})

    def submit(
        self,
        origin: str,
        values: Mapping[str, Any],
        sensitive: Mapping[str, Any],
        *,
        run: FlowStepRun | None = None,
        task: Any = None,
        transition_key: str = "",
    ) -> None:
        """Keep one accepted form as it was sent and merge it into the instance data."""
        if not values and not sensitive:
            return
        FlowSubmission(
            organization_id=self.instance.organization_id,
            instance=self.instance,
            step_run=run,
            task=task,
            step_key=run.step_key if run is not None else "",
            transition_key=transition_key,
            origin=origin,
            values=dict(values),
            sensitive_values=dict(sensitive),
        ).save()
        self.apply_values(values, sensitive)

    def outcomes(self) -> dict[str, str]:
        """The latest outcome of each completed step."""
        if self._outcomes is None:
            rows = (
                FlowStepRun.objects.filter(instance=self.instance, status="completed")
                .order_by("sequence")
                .values_list("step_key", "outcome")
            )
            self._outcomes = dict(rows)
        return self._outcomes

    def context(self, step_key: str) -> FlowContext:
        instance = self.instance
        return FlowContext(
            organization_id=instance.organization_id,
            instance_id=instance.pk,
            flow_key=instance.flow_key,
            flow_version=instance.flow_version,
            step_key=step_key,
            subject_table=instance.subject_table,
            subject_id=instance.subject_sysid,
            starter_id=instance.starter_id,
            data=MappingProxyType(dict(instance.data)),
            sensitive=MappingProxyType(dict(instance.sensitive_data)),
            outcomes=MappingProxyType(dict(self.outcomes())),
        )

    def holds(self, condition: Any, step_key: str) -> bool:
        """Evaluate a transition condition; a missing registered name is a failure, never ``True``."""
        if condition is None:
            return True

        def call(name: str) -> bool:
            try:
                entry = get_condition(name)
            except KeyError:
                raise FlowFailure("condition_missing", step_key) from None
            if entry.owner_app != self.spec.owner_app:
                raise FlowFailure("condition_missing", step_key)
            return bool(entry.function(self.context(step_key)))

        data = {**self.instance.data, **self.instance.sensitive_data}
        evaluation = expressions.Evaluation(
            fields=self.spec.data_fields(), data=data, outcomes=self.outcomes(), call=call
        )
        return expressions.evaluate(condition, evaluation)

    # Tokens.
    def open_runs(self) -> list[FlowStepRun]:
        return list(FlowStepRun.objects.filter(instance=self.instance, status__in=OPEN_RUN_STATES).order_by("sequence"))

    def _new_run(self, step: Step, *, scope: Any, branch: str, via: str, status: str = "active") -> FlowStepRun:
        self._activations += 1
        if self._activations > MAX_ACTIVATIONS:
            raise FlowFailure("loop_limit", step.key)
        self.touch(run_count=self.instance.run_count + 1)
        run = FlowStepRun(
            organization_id=self.instance.organization_id,
            instance=self.instance,
            step_key=step.key,
            kind=step.kind,
            status=status,
            sequence=self.instance.run_count,
            scope_id=scope,
            branch=branch,
            via=via,
        )
        run.save()
        return run

    def activate(self, step_key: str, *, scope: Any = None, branch: str = "", via: str = "") -> None:
        """Put a token on a step and keep moving until something has to be waited for."""
        step = self.spec.step(step_key)
        if step.kind == "join":
            self._arrive(step, scope=scope, branch=branch, via=via)
            return
        run = self._new_run(step, scope=scope, branch=branch, via=via)
        if step.kind == "start":
            self.complete(run)
        elif step.kind == "end":
            self._close(run, step.outcome)
            self._finish_instance(step)
        elif step.kind == "exclusive":
            chosen = next(
                (
                    item
                    for item in self.spec.outgoing(step.key)
                    if not item.default and self.holds(item.condition, step.key)
                ),
                None,
            ) or next(item for item in self.spec.outgoing(step.key) if item.default)
            self.log("gateway", run=run, detail={"taken": [chosen.target]})
            self.complete(run, transition=chosen)
        elif step.kind in ("parallel", "inclusive"):
            self._fork(run, step)
        else:
            behavior = _behaviors.get(step.kind)
            if behavior is None:
                raise FlowFailure("unsupported_step", step.key)
            self.log("step_entered", run=run, detail={"label": step.title, "kind": step.kind})
            behavior.enter(self, run, step)

    def _fork(self, run: FlowStepRun, step: Step) -> None:
        out = self.spec.outgoing(step.key)
        if step.kind == "inclusive":
            taken = [item for item in out if not item.default and self.holds(item.condition, step.key)]
            taken = taken or [item for item in out if item.default]
        else:
            taken = list(out)
        run.state = {"branches": [item.ident for item in taken]}
        self._close(run, "", extra={"state"})
        self.log("gateway", run=run, detail={"taken": [item.target for item in taken]})
        for item in taken:
            self.activate(item.target, scope=run.pk, branch=item.ident, via=item.ident)

    def _arrive(self, step: Step, *, scope: Any, branch: str, via: str) -> None:
        """A branch reaches its join: count it and go on only when every activated branch is there."""
        fork = FlowStepRun.objects.filter(instance=self.instance, pk=scope).first() if scope else None
        if fork is None or self.spec.step(fork.step_key).join != step.key:
            raise FlowFailure("join_without_fork", step.key)
        expected = set(fork.state.get("branches", ()))
        run = FlowStepRun.objects.filter(
            instance=self.instance, step_key=step.key, scope_id=fork.pk, status="waiting"
        ).first()
        if run is None:
            run = self._new_run(step, scope=fork.pk, branch="", via=via, status="waiting")
            arrived = {branch}
        else:
            arrived = set(run.state.get("arrived", ())) | {branch}
        run.state = {"arrived": sorted(arrived)}
        if not expected <= arrived:
            run.save(update_fields={"state"})
            return
        self._close(run, "", extra={"state"})
        self.log("join", run=run, detail={"branches": sorted(arrived)})
        [onward] = self.spec.outgoing(step.key)
        # The token leaves the block: it belongs again to whatever enclosed the fork.
        self.activate(onward.target, scope=fork.scope_id, branch=fork.branch, via=onward.ident)

    def _close(
        self, run: FlowStepRun, outcome: str, *, status: str = "completed", extra: set[str] | None = None
    ) -> None:
        run.status, run.outcome, run.finished_at = status, outcome[:64], self.now
        run.save(update_fields={"status", "outcome", "finished_at", *(extra or ())})
        if status == "completed" and self._outcomes is not None:
            self._outcomes[run.step_key] = run.outcome

    def route(self, step: Step, outcome: str) -> Transition:
        """The transition a result of a waiting step triggers; no route is a definition failure."""
        out = self.spec.outgoing(step.key)
        exact = [item for item in out if item.on == outcome and outcome]
        if exact:
            return exact[0]
        plain = [item for item in out if not item.on]
        if (
            plain
            and step.kind in ("start", "timer", "service", "join")
            or (plain and step.kind == "wait" and outcome == "received")
        ):
            return plain[0]
        raise FlowFailure("no_route", step.key)

    def complete(self, run: FlowStepRun, outcome: str = "", *, transition: Transition | None = None) -> None:
        """Finish a step with a result and follow the transition it triggers."""
        step = self.spec.step(run.step_key)
        if transition is None:
            transition = self.route(step, outcome)
        if run.status in OPEN_RUN_STATES:
            self._close(run, outcome)
        if step.kind in _behaviors:
            self.log("step_completed", run=run, detail={"outcome": run.outcome, "to": transition.target})
        self.activate(transition.target, scope=run.scope_id, branch=run.branch, via=transition.ident)

    # Lifecycle.
    def cancel_runs(self, *, status: str = "cancelled") -> None:
        for run in self.open_runs():
            step = self.spec.step(run.step_key)
            behavior = _behaviors.get(step.kind)
            if behavior is not None:
                behavior.cancel(self, run, step)
            self._close(run, "", status=status)

    def resume_runs(self) -> None:
        """Reschedule whatever the active steps were waiting for (after a pause, a retry or a re-enable)."""
        for run in self.open_runs():
            step = self.spec.step(run.step_key)
            behavior = _behaviors.get(step.kind)
            if behavior is not None and run.status == "active":
                behavior.resume(self, run, step)

    def _finish_instance(self, step: Step) -> None:
        self.cancel_runs()  # The validation leaves no other token; this is defense in depth.
        self.touch(status="completed", outcome=step.outcome, finished_at=self.now)
        self.log("instance_completed", step_key=step.key, detail={"outcome": step.outcome})
        self.moments.append("completed")

    def fail(self, code: str, *, step_key: str = "", run: FlowStepRun | None = None, error_class: str = "") -> None:
        """Mark the instance failed; the open runs stay, so ``retry`` can resume them."""
        if run is not None and run.status == "active":
            run.status = "failed"
            run.save(update_fields={"status"})
        self.touch(status="failed")
        detail = {"code": code}
        if error_class:
            detail["error_class"] = error_class[:200]  # Never the message: it may carry data.
        self.log("instance_failed", step_key=step_key or (run.step_key if run else ""), run=run, detail=detail)
        self.moments.append("failed")
