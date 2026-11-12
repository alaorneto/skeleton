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

Apps publicam pelo `EventService` ([API de serviços](service-api.md#jobservice-e-eventservice)), que exige que a app chamadora seja a dona do tipo e que o ator leia o registro.

```python
from skeleton.service_api.events import EventService

EventService().publish(
    actor=request.user,
    event_type="tasks.task_closed",
    record_id=task.pk,
    data=(("code", task.code), ("status", task.status)),
)
```

Regras da função interna (o serviço as devolve como `ContextRequired`, `InvalidInput` ou `NotFound`):

| Regra | Resultado |
|---|---|
| Fora de contexto tenant (ou `system_context` com organização) | `MissingTenantContext` |
| Registro de outra tabela ou de outra organização | `ValidationError` |
| Chave de `data` não declarada no tipo | `ValidationError` |
| Valor não escalar | `ValidationError` (UUID, data e `Decimal` viram texto) |
| Rollback do chamador | Nenhum evento |

A função interna do núcleo é `skeleton.events.services.publish_event(code, *, record, data=None)`; apps não a importam.

## Consumidores

```python
from skeleton.events.registry import register_consumer

register_consumer("tasks.task_closed", "notifications.task_closed", on_task_closed)  # ou "*" para todos
register_consumer("*", "audit.mirror", mirror, accepts=lambda event_type: event_type.owner_app == "tasks")
```

Um consumidor `"*"` pode declarar `accepts(event_type)`: os tipos recusados não geram job de entrega. O fan-out de webhooks usa `accepts` para receber só os tipos `webhook=True`, de modo que os eventos de vínculo e de ativação de apps não enfileiram nada para ele.

- `publish_event` enfileira, na mesma transação, **um job `events.consume` por consumidor**, com a chave `event:<id>:<consumidor>`. A falha de um consumidor é retentada pelo [worker](jobs.md#executar-run_worker) sem afetar os outros.
- O handler recebe um `EventEnvelope` imutável (`event_id`, `event_type`, `version`, `organization_id`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `occurred_at` e `data` somente leitura) e roda em `system_context` da organização. Ele **reautoriza** o que for ler: o envelope não concede acesso a nada.
- A entrega é pelo menos uma vez: o consumidor precisa ser idempotente. Reenfileirar o mesmo evento devolve os jobs existentes.
- Um evento já apagado pela retenção, ou um consumidor retirado do código, torna a entrega inócua.
- O check `skeleton.E022` reprova consumidor de evento desconhecido e handler que não é chamável.

## Eventos do núcleo

Os serviços de lifecycle do núcleo publicam quatro tipos (`skeleton/events/core_events.py`, #43), sempre na transação da mudança e **um por mudança efetiva**: repetir o estado atual, ou mudar só a configuração de uma app, não publica nada, e um rollback não deixa evento.

| Tipo | Tabela | `data` | Quando |
|---|---|---|---|
| `membership.created` | `sk_tenancy.membership` | `user` | O vínculo passou a ativo: criado, restaurado ou reativado por `provision_membership` (convite, designação, bootstrap), `create_membership`, `update_membership` ou pela criação de uma conta de serviço |
| `membership.deactivated` | `sk_tenancy.membership` | `user` | Um vínculo ativo foi desativado (`update_membership`, `deactivate_service_account`), removido (`delete_membership`) ou deixado (`leave_organization`) |
| `app.enabled` | `sk_system.appinstallation` | `app_label` | A organização ativou a app (`configure_application`, pela tela ou pelo `ApplicationAdministrationService`) |
| `app.disabled` | `sk_system.appinstallation` | `app_label` | A organização desativou a app |

- O payload só tem identificadores: `user` é o UUID da identidade, e `record_sysid` é o vínculo ou a ativação. O consumidor relê e reautoriza o resto. Os `TableSpec` dessas duas tabelas declaram só esses campos em `read_fields`.
- Restaurar um vínculo já inativo não publica `membership.created`; remover um vínculo inativo não publica `membership.deactivated`.
- O upgrade de ativação de #22 (`upgrade_app_activation`) não publica `app.enabled`: ele só materializa um estado que já valia.
- Estes tipos **não** são oferecidos a webhooks (`webhook=False`): a entrega reautoriza a leitura do registro pelo principal do endpoint, e essas tabelas não têm autorização por registro.

```python
from skeleton.events.registry import register_consumer

register_consumer("membership.created", "tasks.welcome_member", welcome)  # welcome(envelope) relê o vínculo
```

Use o código do evento como texto (primeira coluna da tabela acima); o módulo `skeleton.events.core_events` é interno.

## Eventos de fluxos de processo

O motor de [fluxos](flows.md#integrações) publica `flows.instance_started`, `flows.instance_completed`, `flows.instance_cancelled` e `flows.instance_failed` sobre `sk_flows.flowinstance`, na transação do comando, com `flow_key`, `flow_version`, `status`, `outcome`, `subject_table` e `subject_sysid`. Dados de formulário nunca entram no payload. Esses tipos não vão para webhooks; um fluxo pode mapear os mesmos momentos para tipos de evento da própria app sobre o registro de negócio (`subject_events`), que são eventos comuns e alcançam webhooks. O motor também consome `app.enabled`, para reagendar o que os fluxos da app esperavam.

## Retenção

O job periódico `events.retention` (a cada 6 horas, por organização) apaga fisicamente os eventos com `occurred_at` além de `SKELETON_EVENT_RETENTION_DAYS` (30 por padrão). É o único caminho de exclusão de `DomainEvent`. O volume de jobs é proporcional a eventos × consumidores.

## Testes

`skeleton.testing.events.capture_events()` devolve a lista dos eventos publicados no bloco; `skeleton.testing.jobs.run_pending_jobs()` executa as entregas.

## Decisão: auditoria × eventos (ADR)

**Contexto.** Reações a mudanças de negócio precisam de intenção e de payload seguro. Signals de save são proibidos (seriam uma segunda fonte de eventos), e o `AuditEvent` guarda valores redigidos e sem intenção.

**Decisão.** O serviço publica eventos de domínio explícitos na transação da escrita. A auditoria por trigger continua a fonte de verdade do histórico; o evento é só o gatilho de reações, com campos declarados e não sensíveis e com `record_revision`/`request_id` para correlação. A entrega usa um job por par evento/consumidor.

**Consequências.** Esquecer de publicar não perde histórico, só a reação. Um job por consumidor isola falhas ao custo de mais linhas na fila; a retenção limita o crescimento do outbox.
