# Jobs: fila no banco e tipos registrados em código

O núcleo oferece uma fila de trabalho em segundo plano no próprio banco de dados (`skeleton/jobs`, label `sk_jobs`). O serviço enfileira o job **na mesma transação** da escrita de negócio: se a transação for desfeita, o job também deixa de existir. Esse desenho implementa um *outbox* transacional sem depender de Celery nem de um broker externo.

## Por que no banco

| Alternativa | Por que não foi adotada |
|---|---|
| Celery e Redis | Enfileiram fora da transação de negócio: um rollback pode deixar um job órfão, e um commit com o broker indisponível pode perder o job |
| Backport `django-tasks` | Não abre contexto de tenant por job e resolve o handler por caminho de importação, que poderia vir de dados |
| **PostgreSQL (escolhida)** | Garante enfileiramento atômico com a escrita de negócio, contexto de organização no worker e handlers restritos a código registrado |

A API segue o modelo de `django.tasks` (`JobType.enqueue(...)` devolve um recibo com `id` e `status`), facilitando um adaptador futuro.

## Tabela `sk_jobs.Job`

`Job` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) **global**, o que permite ao worker reivindicar jobs de várias organizações sem precisar de uma GUC especial.

Ela armazena os seguintes campos: `job_type`, `queue`, `job_organization` (FK `PROTECT` anulável, nunca chamada `organization`), `actor_id`, `payload`, `state` (`queued`, `running`, `succeeded`, `failed`, `dead` e `cancelled`; o estado `failed` é reservado — uma falha volta para `queued` ou termina em `dead`, e nenhum caminho atual grava `failed`), `attempts`, `max_attempts`, `run_after`, `locked_by`, `locked_until`, `last_error_class`, `last_error_code`, `idempotency_key` e `finished_at`.

- Tipo, organização, ator, payload e chave de idempotência são imutáveis (protegidos por guard no banco).
- No PostgreSQL, o guard de `INSERT` recusa um job de outra organização quando a GUC de tenant está definida (`cross-tenant insert`, SQLSTATE `42501`).
- O payload não carrega dados de negócio nem segredos — apenas identificadores e valores escalares curtos. O handler relê o que precisar no próprio contexto de execução.
- A tabela nunca entra no gateway genérico, e a exclusão física cabe apenas ao serviço de retenção declarado em código.

## Registrar um tipo

Todo tipo de job é registrado em código durante a inicialização da aplicação (`ready()`), associando um código a um handler chamável e a um esquema fechado de payload:

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

O check `skeleton.E021` reprova código duplicado, handler não chamável, valores desconhecidos para `run_as` ou `scope`, job de ator com escopo global e tipos de payload fora da lista permitida.

## Enfileirar

Apps enfileiram jobs pelo `JobService` ([API de serviços](service-api.md#jobservice-e-eventservice)): o tipo precisa pertencer à app chamadora, e a organização é tomada do contexto atual. Um tipo registrado sem `owner_app` não pertence a nenhuma app e é recusado pelo serviço (`Forbidden`) para qualquer chamador.

```python
from skeleton.service_api.jobs import JobService

receipt = JobService().enqueue(
    actor=request.user,
    job_type="tasks.remind",
    payload=(("task_id", str(task.pk)),),
    idempotency_key=f"remind:{task.pk}:{task.revision}",
)
```

A função interna do núcleo é `skeleton.jobs.services.enqueue(code, *, organization_id=None, …)`, que apps não importam diretamente. A tabela abaixo resume as regras dessa função interna; no `JobService`, tentativas sobre tipos de outra app devolvem `Forbidden` e falhas de validação devolvem `InvalidInput`.

| Regra | Resultado |
|---|---|
| Código desconhecido, chave extra ou ausente, tipo incompatível ou payload acima de 4 KB | `ValidationError` |
| `scope="organization"` sem `organization_id` | `ValidationError` |
| Dentro de um tenant, `organization_id` diferente do tenant atual | `ValidationError` (além do guard do PostgreSQL) |
| `scope="global"` com organização ou chamado de dentro de um tenant | `ValidationError` |
| `run_as="actor"` sem ator ou sem organização | `ValidationError` |
| `idempotency_key` repetida para o mesmo tipo e organização | Devolve o job existente (`created=False`), inserido em savepoint |
| A mesma chave usada por outra organização | Cria um job próprio da organização chamadora: a chave é única por organização (e entre os jobs globais), e o recibo nunca expõe um job alheio |

O recibo (`JobReceipt`) contém `id`, `job_type`, `state`, `run_after` e `created`, mas nunca o payload.

## Executar: `run_worker`

O comando `run_worker` consome a fila em segundo plano:

```sh
python manage.py run_worker [--once] [--queues default,mail] [--max-jobs N] [--worker-id ID]
```

| Etapa | Comportamento |
|---|---|
| Reivindicar | Usa `SELECT ... FOR UPDATE SKIP LOCKED` seguido de CAS em `state='queued'`, seguro com vários workers simultâneos. Cada reivindicação incrementa `attempts` e define o lease `locked_until` (`timeout_s` + 30 s) |
| Prazo | Em sistemas POSIX, o handler é interrompido ao atingir `timeout_s` (`SIGALRM` na thread principal do worker) e segue o caminho normal de falha, com `last_error_class=JobTimeout` e `last_error_code=timeout`; nada do que gravou permanece. A transação do job define `lock_timeout` igual a `timeout_s`. Fora da thread principal (ou no Windows) não há interrupção por sinal: handlers dependem dos próprios prazos de I/O, como o cliente HTTP e o SMTP (`timeout` nas `OPTIONS` do mailer, vindo de `EMAIL_TIMEOUT`) |
| Heartbeat | `WorkerHeartbeat` (operacional e global) registra `worker_id`, filas, estado e `last_seen`. O worker atualiza esse registro a cada volta do laço, ocupado ou ocioso, no máximo a cada 15 s (mudanças de estado gravam imediatamente). Um worker com heartbeat antigo, mas com job `running` de lease válido, ainda conta como vivo |
| Lease vencido | O reaper recoloca o job na fila uma vez (`last_error_code=lease_expired`); sem tentativas restantes, o job passa para `dead`. Um worker que perdeu o lease não consegue mais atualizar o estado do job |
| Contexto | Com `run_as="system"`, executa em `system_context(reason="job:<code>", organization_id=...)`, e a auditoria grava `source=system:job:<code>`; o handler só chama métodos com `allow_system=True` e `actor=None` (como `SequenceService.next_number` e as leituras e escritas de títulos do livro). Com `run_as="actor"`, abre `tenant_context` de topo como o ator, revalidando o vínculo, e entrega `actor` ao handler. Em `system_context`, `JobService.enqueue` aceita apenas `actor=None`: um consumidor de evento não enfileira jobs em nome do ator do evento ([decisão](events.md#reagir-numerando-e-abrindo-um-título)) |
| Sucesso | O handler e a marcação `succeeded` comitam na mesma transação: efeitos restritos ao banco acontecem exatamente uma vez |
| Falha | A transação do handler é desfeita; em uma nova transação, o job volta à fila com backoff exponencial (`backoff × 2^(tentativa−1)`, com jitter de 0,5 a 1,5) e registra `last_error_class` e `last_error_code`. Esgotado `max_attempts`, o estado passa para `dead`. Um handler que espera por um bloqueio além do `lock_timeout` da role runtime (10 s, [deployment](deployment.md#provisionamento-postgresql)) falha e segue a mesma política de retentativa |
| Acesso revogado | Um job de ator cujo vínculo não está mais ativo falha fechado, sem escrita, e vai direto para `dead` (`actor_unauthorized`) |
| Organização inativa | O job é pulado e marcado como `cancelled` (`organization_inactive`) |
| App dona (`owner_app`) desativada ou indisponível na organização | O job é pulado antes de executar o handler e fica `cancelled` (`application_disabled`). Tipos do núcleo (sem dono ou com dono que não é app registrada) não passam por essa verificação |
| Organização suspensa por inadimplência | Jobs de apps (com dono que é app registrada) são pulados e ficam `cancelled` (`organization_suspended`). Jobs do núcleo continuam rodando para manter a cobrança e os avisos funcionando ([cobrança](billing.md#ganchos-de-direito)) |
| `SIGTERM` ou `SIGINT` | Para de reivindicar novos jobs, conclui o job em andamento e encerra |
| Manutenção | Com o marcador `SKELETON_MAINTENANCE_MARKER` do deploy, o worker e o scheduler pausam (heartbeat `paused`). Em manutenções planejadas, pare os serviços `worker` e `scheduler` na infraestrutura ([manutenção](maintenance.md#manutenção-planejada-pela-infraestrutura)). O job em andamento termina, e a fila acumula para ser drenada ao sair da manutenção |
| Logs | Registra `job.claimed`, `job.succeeded`, `job.failed`, `job.dead` e `job.skipped` via `security_event`, com o UUID do job e nunca o payload. `job.failed` e `job.dead` incluem `error_stack` (`arquivo:linha:função`), nunca a mensagem da exceção nem o payload |
| Correlação | Cada execução recebe um `request_id` próprio gerado pelo worker, presente em `job.succeeded`, `job.failed`, `job.dead` e `job.skipped` e, com tracing ativo, no span `job <code>` e nos spans de SQL subordinados. Veja [deployment](deployment.md#tracing-opentelemetry-176) |

Efeitos externos (SMTP, HTTP, push) têm garantia **pelo menos uma vez** e exigem registro de entrega e chave de idempotência próprios.

Em testes, `skeleton.testing.jobs.run_pending_jobs()` executa os jobs vencidos usando os mesmos contextos e regras de retentativa do worker e devolve os resultados. Chame essa função fora de qualquer contexto, após o commit que enfileirou os jobs.

## Jobs periódicos

Tarefas recorrentes são registradas em código com `register_periodic`, especificando a cadência (`every` ou `daily_at` em UTC) e se o job roda uma vez por organização ou globalmente:

```python
from datetime import time, timedelta

from skeleton.jobs.periodic import register_periodic

register_periodic("tasks.digest", "tasks.send_digest", daily_at=time(6, 0))  # por organização, 06:00 UTC
register_periodic("files.reconcile", "files.reconcile_quota", every=timedelta(hours=1), per_organization=True)
register_periodic("events.retention", "events.retention", every=timedelta(hours=6))  # por organização (padrão)
register_periodic("jobs.retention", "jobs.retention", every=timedelta(hours=6), per_organization=False)
```

O comando `python manage.py run_scheduler [--once] [--interval 30]` verifica periodicamente as declarações e enfileira a **janela atual** de cada uma:

- A chave de idempotência segue o formato `periodic:<code>:<organização>:<início da janela>`. Vários agendadores podem rodar ao mesmo tempo sem eleição de líder: o banco garante exatamente um job por janela e organização.
- Um tipo que declara `window` (`"str"`) em `payload_schema` recebe o início da janela em UTC (`AAAAMMDDTHHMMSSZ`), permitindo decidir com base na janela agendada em vez do horário efetivo de execução; os demais recebem apenas o payload declarado, sem `window`.
- Antes de enfileirar, o agendador consulta de uma só vez as chaves da janela já presentes na fila e insere apenas as que faltam. Entre verificações, cada processo mantém em memória o que já agendou na janela em curso para evitar novas consultas. Uma organização criada no meio da janela recebe o job na verificação seguinte, e o `INSERT` em savepoint continua protegendo contra concorrência entre agendadores.
- Com `per_organization=True`, os IDs das organizações ativas e não excluídas são materializados antes do laço, e cada organização enfileira em sua própria transação `system_context`, sem transação global envolvendo o laço. Se a app dona do tipo estiver desativada ou indisponível em uma organização, o job não é agendado e não fica marcado na janela — reativar a app agenda a mesma janela no ciclo seguinte. A mesma regra vale para organizações suspensas por inadimplência em jobs de apps ([cobrança](billing.md#ganchos-de-direito)).
- Apenas a janela em curso é agendada: se o agendador ficar parado, ele recupera no máximo uma janela, evitando acúmulo de execuções atrasadas.
- Alterar a cadência de um agendamento passa a valer a partir da próxima janela calculada.
- Jobs periódicos rodam como sistema, e o tipo precisa declarar `scope` coerente com `per_organization` (verificado por `skeleton.E021`).

Além dos jobs de retenção de cada subsistema, o núcleo declara três jobs periódicos globais:

- **`appearance.orphans`** ([#596](https://github.com/alaorneto/skeleton/issues/596), [aparência](appearance.md#imagens)): global (`scope="global"`, `per_organization=False`, `owner_app="sk_appearance"`), a cada 6 horas, com três tentativas. Lista apenas `platform/branding/` no armazenamento privado e remove objetos que não correspondem às três chaves atuais da marca da instância (logo, logo escuro e ícone) e que excedem a carência de `files.orphans` (`SKELETON_FILES_ORPHAN_GRACE_SECONDS`). Nunca toca em `org/…`, assim como `files.orphans` nunca toca em `platform/branding/`.
- **`system.update_check`**: global (`scope="global"`, `per_organization=False`), uma tentativa por janela a cada `SKELETON_UPDATE_CHECK_HOURS` horas. Só é agendado quando `SKELETON_UPDATE_REPOSITORY` está definido e não há agente de deployment ([aviso sem agente](deployment.md#sandbox-no-coolify-369)). Tem payload vazio, não lê nem grava tabelas e guarda o resultado no cache.
- **`accounts.clear_sessions`**: global e diário (03:30 UTC). Remove de `django_session` as sessões com `expire_date` no passado, em lotes de 1 000 chaves e no máximo 100 000 por execução (o restante fica para o dia seguinte), evitando que a tabela de sessões no banco cresça indefinidamente.

## Operar a fila: `/platform/jobs/`

A administração da plataforma oferece uma tela dedicada à fila, exibindo contagens por estado, o atraso do job vencido mais antigo, os workers vivos e a lista de jobs filtrável por estado, fila e tipo. A tela mostra tipo, fila, organização, estado, tentativas, código e classe do erro e datas, mas **nunca o payload**.

| Regra | Comportamento |
|---|---|
| Acesso | Visitantes anônimos são redirecionados ao login; usuários sem papel de administrador da plataforma recebem 403, como no restante de `/platform/` |
| GET | Somente leitura; nenhuma ação de escrita ocorre via GET |
| Reenviar | Disponível apenas para jobs `dead`. O job volta para `queued` com `attempts=0`, `run_after=agora` e campos de erro limpos |
| Descartar | Disponível apenas para jobs `dead`. O job passa para `cancelled` com `last_error_code=discarded` e é removido depois pela retenção |
| Proteções | POST com CSRF, autenticação recente e verificação da revisão vista pelo operador (`updated_at`, via CAS): um segundo envio ou uma alteração concorrente não reenvia o job novamente, e a tela informa que ele mudou |
| Registro | Emite `job.resent` e `job.discarded` via `security_event`, com o UUID do job e a origem `platform:<usuário>` |

As operações ficam em `skeleton.jobs.operations` (`resend_dead_job`, `discard_dead_job` e `queue_health`), e o probe `manage.py check_jobs` utiliza as mesmas contagens. O vigia `notifications.platform_watch` também consulta esses números para avisar os administradores, na caixa e por email, sobre jobs esgotados ou atraso na fila ([alertas da plataforma](notifications.md#alertas-da-plataforma)).

Enquanto uma manutenção pausa a fila, `check_jobs` deixa de acusar `queue_lag` (pois a espera é intencional) e inclui `paused=1` na linha de contagens; jobs esgotados e ausência de workers continuam gerando alerta. Consulte [deployment](deployment.md#worker-de-jobs-e-agendador) para configurar o serviço do worker e a drenagem pelo agente.

## Métricas da fila

Com o extra `[metrics]` instalado e `SKELETON_METRICS_TOKEN` configurado, o endpoint `/metrics` expõe o estado da fila para o Prometheus. Os números são consultados **no momento da coleta** e reaproveitados por cada processo durante `SKELETON_METRICS_QUEUE_TTL_S` segundos (padrão 15): `queue_health()` (o mesmo usado por `check_jobs` e `/platform/jobs/`) e `job_type_state_counts()` (contagens agregadas por tipo e estado). Como o worker não mantém contadores em memória, os valores refletem o banco mesmo após reinícios.

| Métrica | O que mede |
|---|---|
| `skeleton_jobs{job_type,state}` | Total de jobs por tipo registrado e estado, somando todas as organizações; tipos não registrados aparecem como `other` |
| `skeleton_delivery_jobs{channel,state}` | Jobs de entrega: `webhooks.deliver` (`webhook`), `notifications.email` e `notifications.platform_email` (`email`) e `notifications.push` (`push`) |
| `skeleton_jobs_due` | Jobs `queued` cujo `run_after` já venceu |
| `skeleton_jobs_oldest_due_lag_seconds` | Atraso em segundos do job vencido mais antigo |
| `skeleton_jobs_dead` | Total de jobs esgotados |
| `skeleton_job_workers{state}` | Workers `live` e `stale`, conforme o heartbeat |

Os canais de `skeleton_delivery_jobs` vêm do registro de tipos: um job de entrega declara `JobType(..., channel="webhook" | "email" | "push")`. Os pipelines de entrega (webhooks, email e push) compartilham o auxiliar `skeleton.jobs.delivery` (`RetrySchedule` e `enqueue_delivery_attempt`), mas cada um mantém seus próprios estados, calendário de retentativas e formato de chave de idempotência.

Nenhum rótulo expõe organização, ator ou payload. Como alertas operacionais, recomenda-se monitorar atraso acima do `--max-lag` configurado em `check_jobs`, `skeleton_job_workers{state="live"} == 0` e crescimento de `skeleton_jobs_dead`. A configuração do endpoint e do Prometheus está em [deployment](deployment.md#métricas-prometheus-175).

## Jobs dos fluxos de processo

O motor de [fluxos](flows.md#execução) registra os jobs `flows.service` (etapa de serviço), `flows.timer`, `flows.wait_timeout`, `flows.task_reminder` e `flows.task_due`, todos executados como sistema e por organização. O payload carrega apenas o identificador da ativação ou da tarefa junto a um tíquete ou índice, de modo que um job atrasado, duplicado ou pertencente a uma instância suspensa não produz efeito. As retentativas de uma etapa de serviço são controladas pelo próprio motor (criando um novo job com espera crescente), não pelo worker.

## Retenção

O job periódico global `jobs.retention` roda a cada 6 horas e é o único caminho que remove fisicamente linhas de `Job`. A exclusão ocorre em lotes de 1 000 chaves — evitando que um único `DELETE` percorra todo o acúmulo — e usa o índice parcial `sk_jobs_job_finished` sobre `finished_at` (a listagem de `/platform/jobs/` usa `sk_jobs_job_created`). A contagem agregada por tipo e estado usada nas métricas lê a tabela inteira sem índice dedicado, pois o planejador prefere varredura sequencial e o resultado fica em cache durante o intervalo de coleta.

| Estado | Apagado quando `finished_at` ultrapassa |
|---|---|
| `succeeded`, `cancelled` | `SKELETON_JOB_SUCCEEDED_RETENTION_DAYS` (padrão 7) |
| `dead`, `failed` (estado reservado, não gravado atualmente) | `SKELETON_JOB_DEAD_RETENTION_DAYS` (padrão 30) |
| `queued`, `running` | Nunca, independentemente da idade |

Heartbeats de workers `stopped` há mais tempo que a janela de jobs esgotados também são removidos. Ambas as configurações fazem parte do catálogo da instância e podem ser editadas em `/platform/instance/`.
