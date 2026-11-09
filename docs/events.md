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
