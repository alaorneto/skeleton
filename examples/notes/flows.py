"""The publication flow of the Notes example (docs/flows.md): how an app declares a process.

A note is published after a review. The author prepares the request in a form (with a
sensitive remark), an exclusive gateway routes by audience, a public note needs two
approvals in parallel, a rejection returns the request to the author and a service step
registers the publication. The core only runs it: every rule here belongs to the app.
"""

from skeleton.flows import registry as flows

APP_LABEL = "example_notes"
PUBLICATION = "example_notes.publication"
PUBLISH_HANDLER = "example_notes.publish"

REQUEST = flows.Form(
    flows.Field("audience", "choice", label="Público", required=True, choices=("internal", "public")),
    flows.Field("summary", "str", label="Resumo para os revisores", max_length=200),
    flows.Field("remark", "text", label="Observação reservada", sensitive=True, max_length=2000),
)
WITHDRAWAL = flows.Form(flows.Field("withdrawal_reason", "text", label="Motivo da desistência", max_length=500))
PUBLISHED = flows.Form(flows.Field("published_reference", "str", label="Referência da publicação"))

PUBLICATION_FLOW = flows.FlowSpec(
    key=PUBLICATION,
    version=1,
    label="Publicação de anotação",
    owner_app=APP_LABEL,
    subject_table="example_notes.note",
    steps=(
        flows.start("start"),
        flows.task("prepare", label="Preparar o pedido", assignees=(flows.Assignment.starter(),), form=REQUEST),
        flows.exclusive("route", label="Público externo?"),
        flows.approval("review", label="Revisão", assignees=(flows.Assignment.role("organization_admin"),)),
        flows.parallel("fork", label="Revisões em paralelo", join="joined"),
        flows.approval("content", label="Revisão de conteúdo", assignees=(flows.Assignment.role("note_reviewer"),)),
        flows.approval(
            "compliance",
            label="Revisão de conformidade",
            assignees=(flows.Assignment.role("organization_admin"),),
            sla=flows.Sla(due_seconds=3 * 86400, remind_before_seconds=(86400,)),
        ),
        flows.join("joined"),
        flows.exclusive("verdict", label="As duas aprovaram?"),
        flows.service("publish", label="Registrar a publicação", handler=PUBLISH_HANDLER, outputs=PUBLISHED),
        flows.end("published", outcome="published"),
        flows.end("withdrawn", outcome="withdrawn"),
    ),
    transitions=(
        flows.Transition("start", "prepare"),
        flows.Transition("prepare", "route", key="submit", label="Enviar para revisão"),
        flows.Transition("prepare", "withdrawn", key="withdraw", label="Desistir", form=WITHDRAWAL),
        flows.Transition("route", "fork", condition=flows.eq("audience", "public")),
        flows.Transition("route", "review", default=True),
        flows.Transition("review", "publish", on="approved"),
        flows.Transition("review", "prepare", on="rejected"),
        flows.Transition("fork", "content"),
        flows.Transition("fork", "compliance"),
        flows.Transition("content", "joined", on="approved"),
        flows.Transition("content", "joined", on="rejected"),
        flows.Transition("compliance", "joined", on="approved"),
        flows.Transition("compliance", "joined", on="rejected"),
        flows.Transition("joined", "verdict"),
        flows.Transition(
            "verdict",
            "publish",
            condition=flows.all_of(
                flows.eq(flows.outcome("content"), "approved"), flows.eq(flows.outcome("compliance"), "approved")
            ),
        ),
        flows.Transition("verdict", "prepare", default=True),
        flows.Transition("publish", "published"),
    ),
)


def publish(context):
    """Service step: runs in a job as the system. Here it only produces the publication reference.

    A real app would re-read the note in this context and write its own tables; an external
    effect (an email, an HTTP call) would go to a job of the app, never inline.
    """
    return flows.StepResult(data={"published_reference": f"PUB-{str(context.subject_id)[:8]}"})


def register():
    flows.register_flow_handler(PUBLISH_HANDLER, publish, owner_app=APP_LABEL)
    flows.register_flow(PUBLICATION_FLOW)
