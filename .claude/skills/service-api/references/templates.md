# Modelos da API de serviços

Modelos para uma app hipotética `applications.tasks` (label `project_tasks`, app_id `com.example.tasks`) com o modelo `Task(SecuredRecord)`. Troque nomes e campos. O exemplo executável do repositório é `examples/notes/service_api.py`, com testes em `tests/integration/test_notes_service_api.py`.

## `service_api.py`

```python
"""API de serviços de tarefas: único ponto que outras apps podem importar."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from skeleton.access.policies import authorized_queryset, require_record_action
from skeleton.service_api import NotFound, Page, ServiceAPI, paginate, service_method

from . import services
from .models import Task

APP_LABEL = "project_tasks"


@dataclass(frozen=True, slots=True)
class TaskInfo:
    """Tarefa como vista por outras apps."""

    sysid: UUID
    code: str
    title: str
    status: str
    due_at: datetime | None
    revision: int


def _info(task: Task) -> TaskInfo:
    return TaskInfo(task.pk, task.code, task.title, task.status, task.due_at, task.revision)


class TaskService(ServiceAPI):
    """Consulta e criação de tarefas da organização atual."""

    app_label = APP_LABEL

    @service_method(read_only=True, since="1.0")
    def task(self, *, actor, task_id: UUID) -> TaskInfo:
        """Devolve uma tarefa legível pelo ator.

        Autorização: read em project_tasks.task e acesso ao registro.
        Erros: NotFound se inexistente, de outra organização ou não legível; Unavailable se a app estiver desativada.
        Efeitos: nenhum.
        """
        task = authorized_queryset(Task, user=actor, action="read").filter(pk=task_id).first()
        if task is None:
            raise NotFound()
        return _info(task)

    @service_method(read_only=True, since="1.0")
    def tasks(
        self, *, actor, status: str | None = None, cursor: str | None = None, page_size: int = 25
    ) -> Page[TaskInfo]:
        """Lista tarefas legíveis, ordenadas por criação.

        Autorização: read em project_tasks.task; filtra antes de paginar.
        Erros: InvalidInput para status desconhecido ou page_size > 100.
        Efeitos: nenhum.
        """
        rows = authorized_queryset(Task, user=actor, action="read")
        if status is not None:
            rows = rows.filter(status=services.validated_status(status))
        # key: keyset pages over immutable columns, the last one unique (docs/service-api.md, #305).
        return paginate(rows, cursor=cursor, page_size=page_size, convert=_info, key=("created_at", "sysid"))

    @service_method(since="1.0")
    def create_task(
        self, *, actor, title: str, due_at: datetime | None = None, idempotency_key: str | None = None
    ) -> TaskInfo:
        """Cria uma tarefa privada do ator.

        Autorização: create em project_tasks.task.
        Erros: InvalidInput para título vazio ou longo; Forbidden sem permissão de criação.
        Efeitos: grava Task (auditada); publica o evento tasks.task.created quando eventos existirem.
        """
        return _info(services.create_task(actor=actor, title=title, due_at=due_at, idempotency_key=idempotency_key))

    @service_method(since="1.0")
    def close_task(self, *, actor, task_id: UUID, expected_revision: int) -> TaskInfo:
        """Conclui uma tarefa.

        Autorização: update em project_tasks.task e acesso de escrita ao registro.
        Erros: NotFound; Forbidden sem escrita; Conflict(stale_revision) se a revisão mudou.
        Efeitos: atualiza Task; notifica o dono quando notificações existirem.
        """
        task = authorized_queryset(Task, user=actor, action="read").filter(pk=task_id).first()
        if task is None:
            raise NotFound()
        require_record_action(task, user=actor, action="update")
        # ConcurrentUpdate vira Conflict("stale_revision") no @service_method; não capture aqui.
        return _info(services.close_task(task, actor=actor, expected_revision=expected_revision))
```

## Manifesto

```json
"dependencies": {"org.skeleton.notifications": ">=1.0,<2.0"},
"runtime": {
  "service_api": {"module": "applications.tasks.service_api", "classes": ["TaskService"]}
}
```

## Consumidor

```python
from applications.tasks.service_api import TaskService
from skeleton.service_api import Unavailable


def schedule_followup(*, actor, invoice):
    try:
        return TaskService().create_task(
            actor=actor, title=f"Cobrar fatura {invoice.code}", idempotency_key=f"invoice:{invoice.pk}"
        )
    except Unavailable:
        return None
```

## Testes

```python
from uuid import uuid4

import pytest

from applications.tasks.models import Task
from applications.tasks.service_api import TaskService
from skeleton.service_api import Forbidden
from skeleton.testing.service_api import (
    assert_cas_conflict,
    assert_denied_as_not_found,
    assert_rejects_other_actor,
    assert_service_contract,
    assert_tenant_isolated,
    assert_unavailable_when_disabled,
    build_service_scenario,
)


@pytest.fixture
def scenario(skeleton_db):  # a fixture de banco isolado do projeto (PostgreSQL com a role runtime)
    return build_service_scenario(Task)


def test_contract():
    assert_service_contract(TaskService)


def test_isolated_between_organizations(scenario):
    foreign = scenario.create(scenario.b, code="b-1", title="Tarefa de B")
    assert_tenant_isolated(scenario.caller(lambda actor: TaskService().tasks(actor=actor)), foreign=[foreign, "b-1"])
    assert_denied_as_not_found(
        scenario.caller(lambda actor: TaskService().task(actor=actor, task_id=foreign.pk)),
        missing=scenario.caller(lambda actor: TaskService().task(actor=actor, task_id=uuid4())),
    )


def test_default_deny(scenario):
    with pytest.raises(Forbidden):
        scenario.caller(lambda actor: TaskService().tasks(actor=actor), user=scenario.denied)()


def test_disabled_app_is_unavailable(scenario):
    assert_unavailable_when_disabled(
        scenario.caller(lambda actor: TaskService().tasks(actor=actor)), disabled=scenario.application_disabled
    )


def test_actor_must_match_context(scenario):
    assert_rejects_other_actor(scenario.caller(lambda actor: TaskService().tasks(actor=scenario.outsider)))


def test_stale_revision_conflicts(scenario):
    task = scenario.create(scenario.a, code="t-1", title="t")

    def close():
        return scenario.caller(
            lambda actor: TaskService().close_task(actor=actor, task_id=task.pk, expected_revision=task.revision)
        )

    assert_cas_conflict(close(), close())


def test_idempotent_creation(scenario):
    create = scenario.caller(lambda actor: TaskService().create_task(actor=actor, title="t", idempotency_key="k"))
    assert create().sysid == create().sysid
```

`scenario.caller` abre o contexto de cada chamada: contextos não aninham nem trocam de ator. O cenário grava dados confirmados, então use-o só na fixture de banco descartável do projeto.

## `docs/service-api.md` da app

```markdown
# API de serviços: Tarefas

## Visão geral
<para que outras apps usam esta API>

## Dependência
`app_id`: `com.example.tasks`. Faixa recomendada no manifesto do consumidor: `">=1.0,<2.0"`.

## Serviços

### `TaskService`

#### `task(*, actor, task_id) -> TaskInfo`
<descrição>
- **Autorização:** <...>
- **Erros:** <...>
- **Efeitos:** <...>
- **Exemplo:** `TaskService().task(actor=request.user, task_id=uuid)`

## DTOs

### `TaskInfo`
| Campo | Tipo | Significado |
|---|---|---|
| `sysid` | UUID | Identificador estável |

## Eventos publicados
<tipo, payload | Não se aplica>

## Histórico de versões
| Versão | Mudança |
|---|---|
| 1.0 | Primeira versão: `task`, `tasks`, `create_task`, `close_task` |
```
