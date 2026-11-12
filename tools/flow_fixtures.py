"""Flow specifications used by the tests of ``skeleton.flows`` (never loaded by the application).

``review_spec`` is a valid flow that exercises every step kind: a task with a form, an
exclusive gateway, a parallel block with two approvals, an inclusive block with a service
and a timer, a wait and two ends. Tests mutate ``review_steps``/``review_transitions`` to
build the invalid variants.
"""

from __future__ import annotations

from skeleton.flows import registry as flows

OWNER = "example_notes"
SUBJECT = "example_notes.note"
KEY = "example_notes.review"

PREPARE = flows.Form(
    flows.Field("audience", "choice", label="Público", required=True, choices=("internal", "public")),
    flows.Field("amount", "decimal", label="Valor", minimum=0),
    flows.Field("remark", "text", label="Observação reservada", sensitive=True, max_length=500),
    flows.Field("reviewer", "user", label="Revisor"),
    flows.Field("notify", "bool", label="Avisar a equipe"),
)
RETURN = flows.Form(flows.Field("reason", "text", label="Motivo", required=True, max_length=500))
PUBLISHED = flows.Form(flows.Field("reference", "str", label="Referência"))


def review_steps() -> dict[str, flows.Step]:
    steps = (
        flows.start("start"),
        flows.task("prepare", label="Preparar", assignees=(flows.Assignment.starter(),), form=PREPARE),
        flows.exclusive("route", label="Público externo?"),
        flows.approval("single", label="Revisão", assignees=(flows.Assignment.role("note_reviewer"),)),
        flows.parallel("fork", join="joined"),
        flows.approval(
            "legal", label="Jurídico", assignees=(flows.Assignment.group("legal", expand=True),), policy="all"
        ),
        flows.approval("editor", label="Editor", assignees=(flows.Assignment.field("reviewer"),)),
        flows.join("joined"),
        flows.inclusive("extras", join="extras_done"),
        flows.service("announce", label="Anunciar", handler="example_notes.announce"),
        flows.timer("cool", label="Aguardar", delay_seconds=60),
        flows.join("extras_done"),
        flows.exclusive("verdict"),
        flows.service("publish", label="Publicar", handler="example_notes.publish", outputs=PUBLISHED),
        flows.wait("confirm", label="Confirmação", signal="confirmed", timeout_seconds=3600),
        flows.end("published", outcome="published"),
        flows.end("withdrawn", outcome="withdrawn"),
    )
    return {item.key: item for item in steps}


def review_transitions() -> list[flows.Transition]:
    approved = flows.all_of(flows.eq(flows.outcome("legal"), "approved"), flows.eq(flows.outcome("editor"), "approved"))
    return [
        flows.Transition("start", "prepare"),
        flows.Transition("prepare", "route", key="submit", label="Enviar"),
        flows.Transition("prepare", "withdrawn", key="withdraw", label="Desistir", form=RETURN),
        flows.Transition("route", "fork", condition=flows.eq("audience", "public")),
        flows.Transition("route", "single", default=True),
        flows.Transition("single", "publish", on="approved"),
        flows.Transition("single", "prepare", on="rejected"),
        flows.Transition("fork", "legal"),
        flows.Transition("fork", "editor"),
        flows.Transition("legal", "joined", on="approved"),
        flows.Transition("legal", "joined", on="rejected"),
        flows.Transition("editor", "joined", on="approved"),
        flows.Transition("editor", "joined", on="rejected"),
        flows.Transition("joined", "extras"),
        flows.Transition("extras", "announce", condition=flows.truthy("notify")),
        flows.Transition("extras", "cool", default=True),
        flows.Transition("announce", "extras_done"),
        flows.Transition("cool", "extras_done"),
        flows.Transition("extras_done", "verdict"),
        flows.Transition("verdict", "publish", condition=approved),
        flows.Transition("verdict", "prepare", default=True),
        flows.Transition("publish", "confirm"),
        flows.Transition("confirm", "published", on="received"),
        flows.Transition("confirm", "withdrawn", on="timeout"),
    ]


def review_spec(steps=None, transitions=None, **overrides) -> flows.FlowSpec:
    values = {
        "key": KEY,
        "version": 1,
        "label": "Revisão de anotação",
        "owner_app": OWNER,
        "subject_table": SUBJECT,
        "steps": tuple((steps if steps is not None else review_steps()).values()),
        "transitions": tuple(transitions if transitions is not None else review_transitions()),
    }
    values.update(overrides)
    return flows.FlowSpec(**values)
