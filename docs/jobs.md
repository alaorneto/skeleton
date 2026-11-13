# Jobs: fila no banco e tipos registrados em código

O núcleo tem uma fila de trabalho em segundo plano no próprio banco (`skeleton/jobs`, label `sk_jobs`). Um serviço enfileira o job **na mesma transação** da escrita de negócio: se o chamador desfizer a transação, o job também some. Isso dá o outbox transacional sem Celery nem broker externo.

## Por que no banco

| Alternativa | Por que não |
|---|---|
| Celery/Redis | Enqueue fora da transação do negócio; um rollback deixaria o job órfão, e um commit sem broker perderia o job |
| Backport `django-tasks` | Não abre contexto tenant por job e resolve o handler por import path, que poderia vir de dados |
| **PostgreSQL (escolhida)** | Enqueue atômico com o negócio, contexto por organização no worker, handler só de código registrado |

A API imita a de `django.tasks` (`JobType.enqueue(...)` devolve um recibo com `id` e `status`), para um adaptador futuro.

## Tabela `sk_jobs.Job`

`Job` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) **global**: o worker reivindica jobs de várias organizações sem GUC especial. Campos: `job_type`, `queue`, `job_organization` (FK PROTECT nula, nunca `organization`), `actor_id`, `payload`, `state` (`queued`, `running`, `succeeded`, `failed`, `dead`, `cancelled`; `failed` é reservado: uma falha volta a `queued` ou termina `dead`, e nenhum caminho atual grava `failed`), `attempts`, `max_attempts`, `run_after`, `locked_by`, `locked_until`, `last_error_class`, `last_error_code`, `idempotency_key` e `finished_at`.

- Tipo, organização, ator, payload e chave de idempotência são imutáveis (guard no banco).
- No PostgreSQL, o guard de INSERT recusa um job de outra organização quando a GUC tenant está definida (`cross-tenant insert`, SQLSTATE 42501).
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

Apps enfileiram pelo `JobService` ([API de serviços](service-api.md#jobservice-e-eventservice)): o tipo precisa pertencer à app chamadora e a organização é a do contexto. Um tipo registrado sem `owner_app` não pertence a ninguém: o serviço o recusa (`Forbidden`) para qualquer chamador (#272).

```python
from skeleton.service_api.jobs import JobService

receipt = JobService().enqueue(
    actor=request.user,
    job_type="tasks.remind",
    payload=(("task_id", str(task.pk)),),
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
| A mesma chave usada por outra organização | cria um job próprio da organização chamadora: a chave é única por organização (e entre os jobs globais), e o recibo nunca traz o job alheio (#241) |

A função interna do núcleo é `skeleton.jobs.services.enqueue(code, *, organization_id=None, …)`; apps não a importam. A tabela acima descreve a função interna; o serviço devolve `Forbidden` para tipo de outra app e `InvalidInput` para as recusas de validação.

O recibo (`JobReceipt`) traz `id`, `job_type`, `state`, `run_after` e `created`, nunca o payload.

## Executar: `run_worker`

```sh
python manage.py run_worker [--once] [--queues default,mail] [--max-jobs N] [--worker-id ID]
```

| Etapa | Comportamento |
|---|---|
| Reivindicar | `SELECT ... FOR UPDATE SKIP LOCKED` seguido de CAS `state='queued'`, seguro com vários workers. Cada claim incrementa `attempts` e define o lease `locked_until` (`timeout_s` + 30 s) |
| Prazo | Em POSIX, o handler é interrompido em `timeout_s` (`SIGALRM` na thread principal do worker) e segue o caminho normal de falha, com `last_error_class=JobTimeout` e `last_error_code=timeout`; nada do que ele gravou permanece. A transação do job tem `lock_timeout` igual a `timeout_s`. Fora da thread principal (ou no Windows) não há interrupção: handlers usam prazos próprios de I/O, como o cliente HTTP (#224) e o SMTP (`EMAIL_TIMEOUT`, #297) |
| Heartbeat | `WorkerHeartbeat` (operacional, global) guarda `worker_id`, filas, estado e `last_seen`; o worker o grava a cada volta do laço, ocupado ou ocioso, no máximo a cada 15 s (mudança de estado grava na hora). Um worker com heartbeat antigo mas com job `running` de lease válido conta como vivo (#243) |
| Lease vencido | O reaper recoloca na fila uma vez (`last_error_code=lease_expired`); sem tentativas restantes, o job vira `dead`. Um worker que perdeu o lease não consegue mais marcar o job |
| Contexto | `run_as="system"` executa em `system_context(reason="job:<code>", organization_id=...)`, e a auditoria das escritas grava `source=system:job:<code>`. `run_as="actor"` abre `tenant_context` de topo como o ator, que revalida o vínculo; o handler recebe `actor` |
| Sucesso | O handler e a marca `succeeded` comitam na mesma transação: efeitos só no banco acontecem exatamente uma vez |
| Falha | A transação do handler é desfeita; numa transação nova o job volta à fila com backoff exponencial (`backoff × 2^(tentativa−1)`, com jitter de 0,5 a 1,5) e `last_error_class/code`. Esgotado `max_attempts`, fica `dead` |
| Acesso revogado | Job de ator sem vínculo ativo falha fechado, sem escrita, e vai direto para `dead` (`actor_unauthorized`) |
| Organização inativa | O job é pulado e fica `cancelled` (`organization_inactive`) |
| SIGTERM/SIGINT | Para de reivindicar, termina o job atual e sai |
| Manutenção | Com o marcador `SKELETON_MAINTENANCE_MARKER`, o worker pausa (heartbeat `paused`) |
| Logs | `job.claimed`, `job.succeeded`, `job.failed`, `job.dead` e `job.skipped` via `security_event`, com o UUID do job e nunca o payload; `job.failed` e `job.dead` trazem `error_stack` (arquivo:linha:função), nunca a mensagem da exceção nem o payload (#243) |
| Correlação | Cada execução recebe um `request_id` próprio, gerado pelo worker: vai em `job.succeeded`, `job.failed`, `job.dead` e `job.skipped` e, com tracing ligado, no span `job <code>` e nos spans de SQL abaixo dele (#176). Veja [deployment](deployment.md#tracing-opentelemetry-176) |

Efeitos externos (SMTP, HTTP, push) são **pelo menos uma vez**: precisam de registro de entrega e chave de idempotência próprios.

Em testes, `skeleton.testing.jobs.run_pending_jobs()` executa os jobs vencidos com os mesmos contextos e retentativas do worker e devolve os resultados. Chame-o fora de qualquer contexto, depois do commit que enfileirou.

## Jobs periódicos

```python
from datetime import time, timedelta

from skeleton.jobs.periodic import register_periodic

register_periodic("tasks.digest", "tasks.send_digest", daily_at=time(6, 0))  # por organização, 06:00 UTC
register_periodic("files.reconcile", "files.reconcile_quota", every=timedelta(hours=1), per_organization=True)
register_periodic("events.retention", "events.retention", every=timedelta(hours=6))  # por organização (padrão)
register_periodic("jobs.retention", "jobs.retention", every=timedelta(hours=6), per_organization=False)
```

`python manage.py run_scheduler [--once] [--interval 30]` enfileira, a cada verificação, a **janela atual** de cada declaração:

- A chave de idempotência é `periodic:<code>:<organização>:<início da janela>`. Vários agendadores podem rodar ao mesmo tempo, sem eleição de líder: o banco guarda exatamente um job por janela e organização.
- Um tipo que declara `window` (`"str"`) no `payload_schema` recebe o início da janela em UTC (`AAAAMMDDTHHMMSSZ`), para decidir pela janela agendada e não pela hora em que roda; os demais recebem o payload declarado, sem `window` (#276).
- Antes de enfileirar, o agendador lê de uma vez as chaves da janela que já estão na fila e só insere as que faltam; entre verificações, cada processo lembra o que já agendou na janela em curso e nem consulta de novo. Uma organização criada no meio da janela recebe o job na verificação seguinte. O `INSERT` em savepoint continua como proteção quando dois agendadores correm ao mesmo tempo (#240).
- Com `per_organization=True`, os IDs das organizações ativas e não excluídas são materializados antes, e cada organização enfileira na própria transação `system_context`. Nenhuma transação global envolve o laço.
- Só a janela em curso é agendada: um agendador parado recupera no máximo uma janela, nunca um acúmulo.
- Mudar o agendamento vale a partir da próxima janela calculada com a nova cadência.
- Jobs periódicos rodam como sistema; o tipo precisa ter `scope` coerente com `per_organization` (check `skeleton.E021`).

## Operar a fila: `/platform/jobs/`

A administração da plataforma tem uma tela da fila (#40), com contagens por estado, atraso do job vencido mais antigo, workers vivos e a lista dos jobs filtrável por estado, fila e tipo. A tela mostra tipo, fila, organização, estado, tentativas, código e classe do erro e datas; **nunca o payload**.

| Regra | Comportamento |
|---|---|
| Acesso | Anônimo vai ao login; quem não é administrador da plataforma recebe 403, como no resto de `/platform/` |
| GET | Só lê. Nenhuma ação acontece por GET |
| Reenviar | Só um job `dead`. Volta para `queued` com `attempts=0`, `run_after=agora` e erro limpo |
| Descartar | Só um job `dead`. Fica `cancelled` com `last_error_code=discarded`, e a retenção o apaga depois |
| Proteções | POST com CSRF, autenticação recente e o `updated_at` que a pessoa viu (CAS): um segundo envio ou uma mudança concorrente não reenvia de novo, e a tela avisa que o job mudou |
| Registro | `job.resent` e `job.discarded` em `security_event`, com o UUID do job e a origem `platform:<usuário>` |

As operações ficam em `skeleton.jobs.operations` (`resend_dead_job`, `discard_dead_job`, `queue_health`), e o probe `manage.py check_jobs` usa as mesmas contagens. Veja [deployment](deployment.md#worker-de-jobs-e-agendador) para o serviço do worker e a drenagem pelo agente.

## Métricas da fila

Com o extra `[metrics]` e `SKELETON_METRICS_TOKEN` definidos (#175), `/metrics` expõe a fila para o Prometheus. Os números são lidos **na coleta**, e cada processo reutiliza o resultado por `SKELETON_METRICS_QUEUE_TTL_S` segundos (padrão 15, #275): `queue_health()` (o mesmo de `check_jobs` e de `/platform/jobs/`) e `job_type_state_counts()`, contagens agregadas por tipo e estado. O worker não incrementa contadores, então os números batem com o banco mesmo depois de reinícios.

| Métrica | O que mede |
|---|---|
| `skeleton_jobs{job_type,state}` | Jobs por tipo registrado e estado, todas as organizações juntas; tipos não registrados entram como `other` |
| `skeleton_delivery_jobs{channel,state}` | Entregas: `webhooks.deliver` (`webhook`), `notifications.email` (`email`) e `notifications.push` (`push`) |
| `skeleton_jobs_due` | Jobs `queued` com `run_after` vencido |
| `skeleton_jobs_oldest_due_lag_seconds` | Atraso do job vencido mais antigo |
| `skeleton_jobs_dead` | Jobs esgotados |
| `skeleton_job_workers{state}` | Workers `live` e `stale`, pelo heartbeat |

Os canais de `skeleton_delivery_jobs` vêm do registro de tipos: um tipo de job de entrega declara `JobType(..., channel="webhook" | "email" | "push")`. Os pipelines de entrega (webhooks, email, push) usam o mesmo auxiliar `skeleton.jobs.delivery` (`RetrySchedule` e `enqueue_delivery_attempt`); cada um mantém os próprios estados, agenda e formato de chave de idempotência (#346).

Nenhum rótulo traz organização, ator ou payload. Alertas sugeridos: atraso acima do `--max-lag` usado em `check_jobs`, `skeleton_job_workers{state="live"} == 0` e crescimento de `skeleton_jobs_dead`. Configuração do endpoint e do Prometheus em [deployment](deployment.md#métricas-prometheus-175).

## Jobs dos fluxos de processo

O motor de [fluxos](flows.md#execução) registra `flows.service` (etapa de serviço), `flows.timer`, `flows.wait_timeout`, `flows.task_reminder` e `flows.task_due`, todos como sistema e por organização. O payload leva o identificador da ativação ou da tarefa e um tíquete ou índice: um job atrasado, duplicado ou de uma instância suspensa não faz nada. As retentativas de uma etapa de serviço são do motor (novo job com espera crescente), não do worker.

## Retenção

O job periódico global `jobs.retention` roda a cada 6 horas e é o único caminho que apaga fisicamente linhas de `Job`:

| Estado | Apagado depois de `finished_at` além de |
|---|---|
| `succeeded`, `cancelled` | `SKELETON_JOB_SUCCEEDED_RETENTION_DAYS` (padrão 7) |
| `dead`, `failed` (estado reservado, que nenhum caminho atual grava) | `SKELETON_JOB_DEAD_RETENTION_DAYS` (padrão 30) |
| `queued`, `running` | nunca, qualquer que seja a idade |

Heartbeats de workers `stopped` há mais que a janela dos esgotados também são apagados. As duas chaves estão no catálogo da instância e podem ser editadas em `/platform/instance/`.
