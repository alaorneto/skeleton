# Eventos de domínio

Notificações, webhooks e lembretes precisam reagir a mudanças de negócio. O núcleo oferece **eventos de domínio explícitos** (`skeleton/events`, label `sk_events`): o serviço que faz a escrita publica o evento **na mesma transação**. Um rollback não deixa evento.

## Eventos × auditoria

| | Auditoria (`AuditEvent`) | Evento de domínio (`DomainEvent`) |
|---|---|---|
| Fonte | Trigger do banco em cada linha alterada | Chamada explícita do serviço (`publish_event`) |
| Conteúdo | Todas as colunas alteradas, sensíveis redigidas | Só os campos declarados no tipo, lidos e não sensíveis |
| Intenção | Não tem: registra o que mudou | Tem: "nota criada", "tarefa concluída" |
| Papel | Fonte de verdade do histórico | Gatilho para reações (consumidores) |

Signals de save continuam proibidos como fonte de eventos. Cada evento carrega `record_revision` e `request_id` para correlação com o `AuditEvent` da mesma escrita.

## Tabela `sk_events.DomainEvent`

`DomainEvent` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) organizacional, com RLS tenant: a organização B não lê eventos de A. Campos: `event_type`, `version`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `source`, `data` e `occurred_at`, todos imutáveis. Não entra no gateway, e a exclusão física fica para o serviço de retenção.

## Declarar um tipo

```python
from skeleton.events.registry import EventType, register_event_type

register_event_type(
    EventType("tasks.task_closed", table_key="tasks.task", payload_fields=("code", "status"), version=1)
)
```

O check `skeleton.E022` reprova tipo duplicado, tabela sem `TableSpec`, campo de payload fora de `read_fields` e campo de `sensitive_fields`. A versão do payload existe antes de expor o evento a webhooks.

## Publicar

```python
from skeleton.events.services import publish_event

publish_event("tasks.task_closed", record=task, data={"code": task.code, "status": task.status})
```

| Regra | Resultado |
|---|---|
| Fora de contexto tenant (ou `system_context` com organização) | `MissingTenantContext` |
| Registro de outra tabela ou de outra organização | `ValidationError` |
| Chave de `data` não declarada no tipo | `ValidationError` |
| Valor não escalar | `ValidationError` (UUID, data e `Decimal` viram texto) |
| Rollback do chamador | Nenhum evento |

## Consumidores

```python
from skeleton.events.registry import register_consumer

register_consumer("tasks.task_closed", "notifications.task_closed", on_task_closed)  # ou "*" para todos
```

- `publish_event` enfileira, na mesma transação, **um job `events.consume` por consumidor**, com a chave `event:<id>:<consumidor>`. A falha de um consumidor é retentada pelo [worker](jobs.md#executar-run_worker) sem afetar os outros.
- O handler recebe um `EventEnvelope` imutável (`event_id`, `event_type`, `version`, `organization_id`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `occurred_at` e `data` somente leitura) e roda em `system_context` da organização. Ele **reautoriza** o que for ler: o envelope não concede acesso a nada.
- A entrega é pelo menos uma vez: o consumidor precisa ser idempotente. Reenfileirar o mesmo evento devolve os jobs existentes.
- Um evento já apagado pela retenção, ou um consumidor retirado do código, torna a entrega inócua.
- O check `skeleton.E022` reprova consumidor de evento desconhecido e handler que não é chamável.

## Retenção

O job periódico `events.retention` (a cada 6 horas, por organização) apaga fisicamente os eventos com `occurred_at` além de `SKELETON_EVENT_RETENTION_DAYS` (30 por padrão). É o único caminho de exclusão de `DomainEvent`. O volume de jobs é proporcional a eventos × consumidores.

## Testes

`skeleton.testing.events.capture_events()` devolve a lista dos eventos publicados no bloco; `skeleton.testing.jobs.run_pending_jobs()` executa as entregas.

## Decisão: auditoria × eventos (ADR)

**Contexto.** Reações a mudanças de negócio precisam de intenção e de payload seguro. Signals de save são proibidos (seriam uma segunda fonte de eventos), e o `AuditEvent` guarda valores redigidos e sem intenção.

**Decisão.** O serviço publica eventos de domínio explícitos na transação da escrita. A auditoria por trigger continua a fonte de verdade do histórico; o evento é só o gatilho de reações, com campos declarados e não sensíveis e com `record_revision`/`request_id` para correlação. A entrega usa um job por par evento/consumidor.

**Consequências.** Esquecer de publicar não perde histórico, só a reação. Um job por consumidor isola falhas ao custo de mais linhas na fila; a retenção limita o crescimento do outbox.
