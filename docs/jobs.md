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

## Executar: `run_worker`

```sh
python manage.py run_worker [--once] [--queues default,mail] [--max-jobs N] [--worker-id ID]
```

| Etapa | Comportamento |
|---|---|
| Reivindicar | PostgreSQL: `SELECT ... FOR UPDATE SKIP LOCKED` seguido de CAS `state='queued'`, seguro com vários workers. SQLite: o mesmo CAS, com um worker só. Cada claim incrementa `attempts` e define o lease `locked_until` (`timeout_s` + 30 s) |
| Heartbeat | `WorkerHeartbeat` (operacional, global) guarda `worker_id`, filas, estado e `last_seen` |
| Lease vencido | O reaper recoloca na fila uma vez (`last_error_code=lease_expired`); sem tentativas restantes, o job vira `dead`. Um worker que perdeu o lease não consegue mais marcar o job |
| Contexto | `run_as="system"` executa em `system_context(reason="job:<code>", organization_id=...)`, e a auditoria das escritas grava `source=system:job:<code>`. `run_as="actor"` abre `tenant_context` de topo como o ator, que revalida o vínculo; o handler recebe `actor` |
| Sucesso | O handler e a marca `succeeded` comitam na mesma transação: efeitos só no banco acontecem exatamente uma vez |
| Falha | A transação do handler é desfeita; numa transação nova o job volta à fila com backoff exponencial (`backoff × 2^(tentativa−1)`, com jitter de 0,5 a 1,5) e `last_error_class/code`. Esgotado `max_attempts`, fica `dead` |
| Acesso revogado | Job de ator sem vínculo ativo falha fechado, sem escrita, e vai direto para `dead` (`actor_unauthorized`) |
| Organização inativa | O job é pulado e fica `cancelled` (`organization_inactive`) |
| SIGTERM/SIGINT | Para de reivindicar, termina o job atual e sai |
| Manutenção | Com o marcador `SKELETON_MAINTENANCE_MARKER`, o worker pausa (heartbeat `paused`) |
| Logs | `job.claimed`, `job.succeeded`, `job.failed`, `job.dead` e `job.skipped` via `security_event`, com o UUID do job e nunca o payload |

Efeitos externos (SMTP, HTTP, push) são **pelo menos uma vez**: precisam de registro de entrega e chave de idempotência próprios.

Em testes, `skeleton.testing.jobs.run_pending_jobs()` executa os jobs vencidos com os mesmos contextos e retentativas do worker e devolve os resultados. Chame-o fora de qualquer contexto, depois do commit que enfileirou.
