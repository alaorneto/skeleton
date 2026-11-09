# Jobs: fila no banco e tipos registrados em código

O núcleo tem uma fila de trabalho em segundo plano no próprio banco (`skeleton/jobs`, label `sk_jobs`). Um serviço enfileira o job **na mesma transação** da escrita de negócio: se o chamador desfizer a transação, o job também some. Isso dá o outbox transacional sem Celery nem broker externo.

## Por que no banco

| Alternativa | Por que não |
|---|---|
| Celery/Redis | Enqueue fora da transação do negócio; um rollback deixaria o job órfão, e um commit sem broker perderia o job |
| Backport `django-tasks` | Não abre contexto tenant por job e resolve o handler por import path, que poderia vir de dados |
| **PostgreSQL/SQLite (escolhida)** | Enqueue atômico com o negócio, contexto por organização no worker, handler só de código registrado |

A API imita a de `django.tasks` (`JobType.enqueue(...)` devolve um recibo com `id` e `status`), para um adaptador futuro.

## Tabela `sk_jobs.Job`

`Job` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) **global**: o worker reivindica jobs de várias organizações sem GUC especial. Campos: `job_type`, `queue`, `job_organization` (FK PROTECT nula, nunca `organization`), `actor_id`, `payload`, `state` (`queued`, `running`, `succeeded`, `failed`, `dead`, `cancelled`), `attempts`, `max_attempts`, `run_after`, `locked_by`, `locked_until`, `last_error_class`, `last_error_code`, `idempotency_key` e `finished_at`.

- Tipo, organização, ator, payload e chave de idempotência são imutáveis (guard no banco).
- No PostgreSQL, o guard de INSERT recusa um job de outra organização quando a GUC tenant está definida (`cross-tenant insert`, SQLSTATE 42501). No SQLite a mesma regra vale em Python.
- O payload não carrega dado de negócio nem segredo: só identificadores e escalares curtos. O handler relê o que precisar no próprio contexto.
- A tabela nunca entra no gateway, e a exclusão física fica só para o serviço de retenção declarado em código.

## Registrar um tipo

```python
# apps.py da app (ou do subsistema do núcleo)
from skeleton.jobs.registry import JobType, register_job_type


def ready(self):
    register_job_type(
        JobType(
            code="tasks.remind",
            handler=remind,  # callable(**payload)
            payload_schema={"task_id": "uuid"},  # uuid, int, bool ou str (até 200 caracteres)
            run_as="system",  # ou "actor"
            max_attempts=5,
            backoff=30.0,
            timeout_s=300,
            queue="default",
            scope="organization",
        )
    )
```

O check `skeleton.E021` reprova código duplicado, handler que não é chamável, `run_as`/`scope` desconhecidos, job de ator global e tipos de payload fora da lista.

## Enfileirar

```python
from skeleton.jobs.services import enqueue

receipt = enqueue(
    "tasks.remind",
    organization_id=org_id,
    payload={"task_id": str(task.pk)},
    idempotency_key=f"remind:{task.pk}:{task.revision}",
)
```

| Regra | Resultado |
|---|---|
| Código desconhecido, chave extra ou ausente, tipo errado, payload acima de 4 KB | `ValidationError` |
| `scope="organization"` sem `organization_id` | `ValidationError` |
| Dentro de um tenant, `organization_id` diferente do tenant atual | `ValidationError` (e o guard do PostgreSQL) |
| `scope="global"` com organização ou de dentro de um tenant | `ValidationError` |
| `run_as="actor"` sem ator ou sem organização | `ValidationError` |
| `idempotency_key` repetida para o mesmo tipo e organização | devolve o job existente (`created=False`), inserido em savepoint |
| A mesma chave usada por outra organização | `ValidationError`, sem revelar o job alheio |

O recibo (`JobReceipt`) traz `id`, `job_type`, `state`, `run_after` e `created`, nunca o payload.
