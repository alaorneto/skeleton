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

A tabela do evento pode ser uma [tabela derivada](access-control.md#tabelas-derivadas) (#562): o filho de um contêiner publica eventos como qualquer tabela. `EventService.publish` exige que o ator leia o filho, o que se decide pela âncora; o consumidor recebe o `table_key` e o `record_sysid` do filho; e a entrega por webhook reautoriza a leitura do filho pelo principal do endpoint, com a mesma regra.

## Publicar

Apps publicam pelo `EventService` ([API de serviços](service-api.md#jobservice-e-eventservice)), que exige que a app chamadora seja a dona do tipo e que o ator leia o registro. Um tipo registrado sem `owner_app` não pertence a ninguém: o serviço o recusa (`Forbidden`) para qualquer chamador (#272). No [portal](portal.md#superfícies-do-núcleo-para-os-recursos-da-própria-app-741) (#741), a app publica o próprio tipo de dentro de um método `allow_external`, depois de autorizar pela ligação dela: basta o registro existir na organização, e o evento leva a pessoa externa em `actor_id` e `source="http:portal"`. Fora de um método assim, `Forbidden("external_not_allowed")`.

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

register_consumer(
    "tasks.task_closed", "notifications.task_closed", on_task_closed, owner_app="tasks"
)  # ou "*" para todos
register_consumer(
    "*", "audit.mirror", mirror, owner_app="tasks", accepts=lambda event_type: event_type.owner_app == "tasks"
)
```

Um consumidor `"*"` pode declarar `accepts(event_type)`: os tipos recusados não geram job de entrega. Todo consumidor declara `owner_app` (o `app_label` da app dona, ou o label `sk_*` para consumidores do núcleo) e um `code` de até 157 caracteres (para caber em `Job.idempotency_key` com o prefixo `event:<uuid>:`, #876). Em `consume()`, consumidores de apps registradas são pulados (`skipped`, evento `event.consumer_skipped`) quando a app não está efetiva na organização ou quando a organização está suspensa por cobrança; consumidores do núcleo continuam rodando. O fan-out de webhooks usa `accepts` para receber só os tipos `webhook=True`, de modo que os eventos de vínculo e de ativação de apps não enfileiram nada para ele. O consumidor `flows.event_waits` do núcleo aceita só os eventos das apps (não os `sk_*`) e avança as [esperas por evento](flows.md#etapas) dos fluxos (#411).

- `publish_event` enfileira, na mesma transação, **um job `events.consume` por consumidor**, com a chave `event:<id>:<consumidor>`. A falha de um consumidor é retentada pelo [worker](jobs.md#executar-run_worker) sem afetar os outros.
- O handler recebe um `EventEnvelope` imutável (`event_id`, `event_type`, `version`, `organization_id`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `occurred_at` e `data` somente leitura) e roda em `system_context` da organização. Ele **reautoriza** o que for ler: o envelope não concede acesso a nada.
- A entrega é pelo menos uma vez: o consumidor precisa ser idempotente. Reenfileirar o mesmo evento devolve os jobs existentes.
- Um evento já apagado pela retenção, ou um consumidor retirado do código, torna a entrega inócua.
- O check `skeleton.E022` reprova consumidor de evento desconhecido, handler que não é chamável, `owner_app` ausente ou desconhecido e `code` acima de 157 caracteres (#876).

### Reagir numerando e abrindo um título

Um consumidor roda sem ator, então só chama métodos marcados `allow_system=True` e passa `actor=None`. Para reagir a um evento de outra app (abrir uma cobrança quando um pedido é concluído, por exemplo), a app numera com a própria sequência e abre o título no livro contábil, tudo na transação da entrega (#577):

```python
from skeleton.contrib.ledger.service_api import LedgerService
from skeleton.service_api.sequences import SequenceService


def open_charge(envelope):
    """Consumidor de ``orders.order_completed``: um título a receber por pedido, numerado pela app."""
    ledger = LedgerService()
    if ledger.titles_for_source(actor=None, table_key=envelope.table_key, record_id=envelope.record_sysid):
        return  # entrega repetida: o pedido já tem cobrança
    number = SequenceService().next_number(actor=None, key="billing_app.charge")
    ledger.create_title(
        actor=None,
        direction="receivable",
        amount=envelope.data["total"],
        due_date=envelope.occurred_at.date(),
        description=f"Cobrança {number.formatted}",
        category_key="billing_app.revenue",
        idempotency_key=f"charge:{envelope.event_id}",
        source_table=envelope.table_key,
        source_id=envelope.record_sysid,
    )
```

- **Idempotência.** A entrega é pelo menos uma vez, e o job inteiro é uma transação: número, registro e título comitam juntos ou nenhum deles. Confira primeiro se a reação já aconteceu (`titles_for_source`, ou um registro próprio da app com o identificador do evento) e só então numere; o `idempotency_key` derivado de `event_id` é a segunda barreira do livro.
- **Numeração.** `next_number` em `system_context` continua só da app dona da sequência e da organização do evento; o rollback de uma entrega que falha devolve o número, e a numeração não tem lacunas ([sequências](sequences.md#numerar)).
- **Leitura do livro.** `title`, `titles_for_source` e `settlements` aceitam o contexto de sistema com a regra das escritas: com origem, o registro de origem precisa estar vivo na organização. É de `title(...).revision` que sai o `expected_revision` de `cancel_title` e `settle_title` ([livro contábil](../skeleton/contrib/ledger/docs/service-api.md)).
- **Sem acesso novo.** O contexto de sistema do consumidor é o da organização do evento; ele não lê nem escreve outra organização, e os métodos sem `allow_system` (os que autorizam um ator) continuam recusando `actor=None`. O envelope não concede acesso: o que o consumidor lê, ele lê pelos métodos acima.

**Decisão (#577).** A alternativa era deixar o consumidor enfileirar um job `run_as="actor"` com o `actor_id` do envelope. Ela foi descartada: transformaria o contexto de sistema num emissor de jobs em nome de qualquer pessoa da organização (o identificador vem de um dado gravado, não de uma autenticação), e a reação ainda dependeria de a pessoa que disparou o evento ter as permissões do livro, o que uma reação da organização não deve exigir. As superfícies abertas aqui já eram da app dona (a sequência) ou já aceitavam o sistema na escrita (o livro).

## Eventos do núcleo

Os serviços de lifecycle do núcleo publicam quatro tipos (`skeleton/events/core_events.py`, #43), sempre na transação da mudança e **um por mudança efetiva**: repetir o estado atual, ou mudar só a configuração de uma app, não publica nada, e um rollback não deixa evento.

| Tipo | Tabela | `data` | Quando |
|---|---|---|---|
| `membership.created` | `sk_tenancy.membership` | `user` | O vínculo passou a ativo: criado, restaurado ou reativado por `provision_membership` (convite, designação, bootstrap), `create_membership`, `update_membership` ou pela criação de uma conta de serviço |
| `membership.deactivated` | `sk_tenancy.membership` | `user` | Um vínculo ativo foi desativado (`update_membership`, `deactivate_service_account`), removido (`delete_membership`) ou deixado (`leave_organization`) |
| `app.enabled` | `sk_system.appinstallation` | `app_label` | A organização ativou a app (`configure_application`, pela tela ou pelo `ApplicationAdministrationService`, ou `manage.py set_app_availability --enable`, #282), ou a [cobrança](billing.md#ciclo-de-vida) a ativou quando o pagamento deu o direito (`activate_for_entitlement`, #396) |
| `app.disabled` | `sk_system.appinstallation` | `app_label` | A organização desativou a app, ou a app foi retirada com os dados preservados (first-party em `SKELETON_BUNDLED_RETAINED` ou `schema_retained` no inventário do agente): um evento por organização em que ela estava ativa (#282) |

- O payload só tem identificadores: `user` é o UUID da identidade, e `record_sysid` é o vínculo ou a ativação. O consumidor relê e reautoriza o resto. Os `TableSpec` dessas duas tabelas declaram só esses campos em `read_fields`.
- Restaurar um vínculo já inativo não publica `membership.created`; remover um vínculo inativo não publica `membership.deactivated`.
- O upgrade de ativação de #22 (`upgrade_app_activation`) não publica `app.enabled`: ele só materializa um estado que já valia.
- Estes tipos **não** são oferecidos a webhooks (`webhook=False`): a entrega reautoriza a leitura do registro pelo principal do endpoint, e essas tabelas não têm autorização por registro.

```python
from skeleton.events.registry import register_consumer

register_consumer(
    "membership.created", "tasks.welcome_member", welcome, owner_app="tasks"
)  # welcome(envelope) relê o vínculo
```

Use o código do evento como texto (primeira coluna da tabela acima); o módulo `skeleton.events.core_events` é interno.

Os [avisos padrão da organização](notifications.md#avisos-padrão-da-organização) (#786) saem no mesmo ponto em que esses dois eventos são publicados, na transação, e não por um consumidor.

## Eventos da cobrança

A [cobrança](billing.md#eventos) publica `billing.invoice_finalized` e `billing.invoice_paid` sobre `sk_billing.invoice` (`number`, `status`, `currency`, `total`), `billing.subscription_changed` sobre `sk_billing.subscription` (`status`), `billing.access_state_changed` sobre `sk_billing.billingaccount` (`access_state`) e `billing.payment_refunded` / `billing.payment_duplicated` sobre `sk_billing.payment` (`invoice`, `status`, `provider`, `amount`, `currency`, #885), na transação do motor. Dados de contato da conta nunca entram no payload. Esses tipos não vão para webhooks; um emissor fiscal externo consome `billing.invoice_paid` em processo (`register_consumer`).

## Eventos de fluxos de processo

O motor de [fluxos](flows.md#integrações) publica `flows.instance_started`, `flows.instance_completed`, `flows.instance_cancelled` e `flows.instance_failed` sobre `sk_flows.flowinstance`, na transação do comando, com `flow_key`, `flow_version`, `status`, `outcome`, `subject_table` e `subject_sysid`. Dados de formulário nunca entram no payload. Esses tipos não vão para webhooks; um fluxo pode mapear os mesmos momentos para tipos de evento da própria app sobre o registro de negócio (`subject_events`), que são eventos comuns e alcançam webhooks. O motor também consome `app.enabled`, para reagendar o que os fluxos da app esperavam.

## Retenção

O job periódico `events.retention` (a cada 6 horas, por organização) apaga fisicamente os eventos com `occurred_at` além de `SKELETON_EVENT_RETENTION_DAYS` (30 por padrão). A exclusão é feita em lotes de 1 000 eventos da organização da rodada (#308). É o único caminho de exclusão de `DomainEvent`. O volume de jobs é proporcional a eventos × consumidores.

## Testes

`skeleton.testing.events.capture_events()` devolve a lista dos eventos publicados no bloco; `skeleton.testing.jobs.run_pending_jobs()` executa as entregas.

## Decisão: auditoria × eventos (ADR)

**Contexto.** Reações a mudanças de negócio precisam de intenção e de payload seguro. Signals de save são proibidos (seriam uma segunda fonte de eventos), e o `AuditEvent` guarda valores redigidos e sem intenção.

**Decisão.** O serviço publica eventos de domínio explícitos na transação da escrita. A auditoria por trigger continua a fonte de verdade do histórico; o evento é só o gatilho de reações, com campos declarados e não sensíveis e com `record_revision`/`request_id` para correlação. A entrega usa um job por par evento/consumidor.

**Consequências.** Esquecer de publicar não perde histórico, só a reação. Um job por consumidor isola falhas ao custo de mais linhas na fila; a retenção limita o crescimento do outbox.
