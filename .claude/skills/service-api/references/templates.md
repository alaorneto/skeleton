# Modelos da API de serviços

Modelos para uma app hipotética `applications.tasks` (label `project_tasks`, app_id `com.example.tasks`) com o modelo `Task(SecuredRecord)`. Troque nomes e campos. Os imports de `skeleton.service_api` dependem da issue #97; até ela entrar, siga o passo 0 do skill.

## `service_api.py`

```python
"""API de serviços de tarefas: único ponto que outras apps podem importar."""

from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from skeleton.access.policies import authorized_queryset, require_record_action
from skeleton.common.exceptions import ConcurrentUpdate
from skeleton.service_api.base import ServiceAPI, service_method
from skeleton.service_api.dto import Page, paginate
from skeleton.service_api.errors import Conflict, NotFound

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
        rows = authorized_queryset(Task, user=actor, action="read").order_by("created_at", "sysid")
        if status is not None:
            rows = rows.filter(status=services.validated_status(status))
        return paginate(rows, cursor=cursor, page_size=page_size, convert=_info)

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
        try:
            return _info(services.close_task(task, actor=actor, expected_revision=expected_revision))
        except ConcurrentUpdate:
            raise Conflict(code="stale_revision") from None
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
from skeleton.service_api.errors import Unavailable


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
import pytest

from applications.tasks.service_api import TaskService
from skeleton.service_api.errors import Conflict, Forbidden, NotFound, Unavailable

pytestmark = pytest.mark.django_db


def test_task_is_isolated_between_organizations(tenant_a, tenant_b):
    task = tenant_a.create_task(title="A")
    with tenant_b.context() as actor:
        with pytest.raises(NotFound):
            TaskService().task(actor=actor, task_id=task.sysid)


def test_reader_without_table_permission_is_denied(tenant_a):
    with tenant_a.context(role="member_without_permissions") as actor:
        with pytest.raises(NotFound):
            TaskService().task(actor=actor, task_id=tenant_a.create_task(title="x").sysid)


def test_disabled_app_is_unavailable(tenant_a):
    tenant_a.disable_app("project_tasks")
    with tenant_a.context() as actor, pytest.raises(Unavailable):
        TaskService().tasks(actor=actor)


def test_actor_must_match_context(tenant_a, other_user):
    with tenant_a.context(), pytest.raises(Forbidden):
        TaskService().tasks(actor=other_user)


def test_stale_revision_conflicts(tenant_a):
    with tenant_a.context() as actor:
        info = TaskService().create_task(actor=actor, title="t")
        TaskService().close_task(actor=actor, task_id=info.sysid, expected_revision=info.revision)
        with pytest.raises(Conflict):
            TaskService().close_task(actor=actor, task_id=info.sysid, expected_revision=info.revision)


def test_idempotent_creation(tenant_a):
    with tenant_a.context() as actor:
        first = TaskService().create_task(actor=actor, title="t", idempotency_key="k")
        again = TaskService().create_task(actor=actor, title="t", idempotency_key="k")
        assert first.sysid == again.sysid
```

As fixtures `tenant_a`/`tenant_b` vêm do kit de contrato (#100). Até lá, use as fixtures de `tests/conftest.py` com `tenant_context` explícito.

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
