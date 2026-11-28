# Eventos de domínio

Notificações, webhooks e lembretes precisam reagir a mudanças de negócio de forma confiável. Para isso, o núcleo oferece **eventos de domínio explícitos** (`skeleton/events`, label `sk_events`): o serviço que realiza a escrita publica o evento **na mesma transação**. Se a transação sofrer rollback, nenhum evento é persistido.

## Eventos × auditoria

| | Auditoria (`AuditEvent`) | Evento de domínio (`DomainEvent`) |
|---|---|---|
| Origem | Trigger do banco a cada linha alterada | Chamada explícita do serviço (`publish_event`) |
| Conteúdo | Todas as colunas alteradas, com campos sensíveis redigidos | Apenas os campos declarados no tipo, legíveis e não sensíveis |
| Intenção | Não possui: registra apenas o que mudou | Explícita: "nota criada", "tarefa concluída" |
| Papel | Fonte de verdade do histórico | Gatilho para reações (consumidores) |

Signals de `save` do Django continuam proibidos como fonte de eventos. Cada evento carrega `record_revision` e `request_id` para correlação direta com o `AuditEvent` gerado na mesma escrita.

## Tabela `sk_events.DomainEvent`

`DomainEvent` é uma [tabela operacional](data-model.md#tabelas-operacionais-adr) organizacional com RLS de tenant: a organização B nunca lê eventos da organização A.

Os campos `event_type`, `version`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `source`, `data` e `occurred_at` são todos imutáveis. A tabela não entra no gateway genérico, e a exclusão física fica restrita ao serviço de retenção.

## Declarar um tipo

Cada tipo de evento é registrado em código, declarando a tabela associada, os campos permitidos no payload e a versão do formato:

```python
from skeleton.events.registry import EventType, register_event_type

register_event_type(
    EventType("tasks.task_closed", table_key="tasks.task", payload_fields=("code", "status"), version=1)
)
```

O check `skeleton.E022` reprova tipos duplicados, tabelas sem `TableSpec`, campos de payload ausentes de `read_fields` e campos marcados em `sensitive_fields`. A versão do payload é exigida antes de expor o evento a webhooks.

A tabela de um evento pode ser uma [tabela derivada](access-control.md#tabelas-derivadas): um registro filho publica eventos como qualquer outra tabela. `EventService.publish` exige que o ator tenha leitura sobre o filho (decidida pela âncora), o consumidor recebe o `table_key` e o `record_sysid` do filho, e a entrega por webhook reautoriza a leitura do filho pelo principal do endpoint segundo a mesma regra.

## Publicar

Apps publicam eventos pelo `EventService` ([API de serviços](service-api.md#jobservice-e-eventservice)), que exige que a app chamadora seja a dona do tipo e que o ator possa ler o registro. Um tipo registrado sem `owner_app` não pertence a nenhuma app e é recusado (`Forbidden`) para qualquer chamador.

No [portal](portal.md#superfícies-do-núcleo-para-os-recursos-da-própria-app-741), a app pode publicar seus próprios tipos de evento dentro de um método `allow_external`, após autorizar a pessoa pela ligação dela: basta o registro existir na organização, e o evento registra a pessoa externa em `actor_id` com `source="http:portal"`. Fora de um método `allow_external`, a chamada responde `Forbidden("external_not_allowed")`.

```python
from skeleton.service_api.events import EventService

EventService().publish(
    actor=request.user,
    event_type="tasks.task_closed",
    record_id=task.pk,
    data=(("code", task.code), ("status", task.status)),
)
```

A função interna do núcleo é `skeleton.events.services.publish_event(code, *, record, data=None)`, que apps não importam diretamente. A tabela abaixo resume as regras dessa função interna; no `EventService`, essas recusas chegam como `ContextRequired`, `InvalidInput` ou `NotFound`.

| Regra | Resultado |
|---|---|
| Fora de contexto tenant (ou `system_context` com organização) | `MissingTenantContext` |
| Registro de outra tabela ou de outra organização | `ValidationError` |
| Chave de `data` não declarada no tipo | `ValidationError` |
| Valor não escalar | `ValidationError` (UUID, data e `Decimal` são convertidos para texto) |
| Rollback do chamador | Nenhum evento é gravado |

## Consumidores

Consumidores reagem aos eventos de forma assíncrona e são registrados em código com `register_consumer`:

```python
from skeleton.events.registry import register_consumer

register_consumer(
    "tasks.task_closed", "notifications.task_closed", on_task_closed, owner_app="tasks"
)  # ou "*" para todos
register_consumer(
    "*", "audit.mirror", mirror, owner_app="tasks", accepts=lambda event_type: event_type.owner_app == "tasks"
)
```

Todo consumidor declara `owner_app` (o `app_label` da app dona, ou o prefixo `sk_*` para consumidores do núcleo) e um `code` de até 157 caracteres — limite necessário para caber em `Job.idempotency_key` com o prefixo `event:<uuid>:`. Um consumidor curinga (`"*"`) pode declarar `accepts(event_type)` para filtrar eventos antes do enfileiramento; tipos recusados não geram job de entrega.

Por exemplo, o fan-out de webhooks usa `accepts` para receber apenas tipos com `webhook=True`, evitando enfileirar eventos de vínculo ou de ativação de apps. Já o consumidor `flows.event_waits` do núcleo aceita apenas eventos das apps (excluindo os `sk_*`) para avançar [esperas por evento](flows.md#etapas) nos fluxos.

- `publish_event` enfileira, na mesma transação, **um job `events.consume` por consumidor**, com a chave de idempotência `event:<id>:<consumidor>`. A falha de um consumidor é retentada pelo [worker](jobs.md#executar-run_worker) sem afetar os demais.
- Em `consume()`, consumidores de apps registradas são pulados (`skipped`, evento `event.consumer_skipped`) quando a app não está efetiva na organização ou quando a organização está suspensa por cobrança; consumidores do núcleo continuam executando normalmente.
- O handler recebe um `EventEnvelope` imutável (`event_id`, `event_type`, `version`, `organization_id`, `table_key`, `record_sysid`, `record_revision`, `actor_id`, `request_id`, `occurred_at` e `data` somente leitura) e roda em `system_context` da organização. Ele precisa **reautorizar** tudo o que ler: o envelope não concede acesso a nenhum dado.
- A entrega é pelo menos uma vez, portanto o consumidor precisa ser idempotente. Reenfileirar o mesmo evento devolve os jobs já existentes.
- Se o evento já tiver sido removido pela retenção, ou se o consumidor tiver sido retirado do código, a entrega torna-se inócua.
- O check `skeleton.E022` reprova consumidor de evento desconhecido, handler não chamável, `owner_app` ausente ou desconhecido e `code` com mais de 157 caracteres.

### Reagir numerando e abrindo um título

Como o consumidor roda sem ator, ele só chama métodos marcados com `allow_system=True`, passando `actor=None`. Para reagir a um evento de outra app — por exemplo, abrir uma cobrança quando um pedido é concluído —, a app gera o número pela própria sequência e cria o título no livro contábil na mesma transação da entrega:

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

- **Idempotência.** A entrega é pelo menos uma vez, e o job inteiro executa em uma única transação: número, registro e título comitam juntos ou nenhum deles é gravado. Verifique primeiro se a reação já ocorreu (`titles_for_source`, ou um registro próprio da app com o identificador do evento) antes de numerar; o `idempotency_key` derivado de `event_id` funciona como segunda barreira no livro contábil.
- **Numeração.** Em `system_context`, `next_number` continua restrito à app dona da sequência e à organização do evento. Se a entrega falhar, o rollback devolve o número e a sequência permanece sem lacunas ([sequências](sequences.md#numerar)).
- **Leitura do livro.** `title`, `titles_for_source` e `settlements` aceitam o contexto de sistema com a mesma regra das escritas: quando há origem, o registro de origem precisa estar vivo na organização. O `expected_revision` exigido por `cancel_title` e `settle_title` vem de `title(...).revision` ([livro contábil](../skeleton/contrib/ledger/docs/service-api.md)).
- **Sem acesso novo.** O contexto de sistema do consumidor limita-se à organização do evento: ele não lê nem escreve dados de outra organização, e métodos sem `allow_system` (que exigem autorização de ator) continuam recusando `actor=None`. O envelope não concede acesso; o consumidor lê apenas pelos métodos permitidos acima.

**Decisão.** A alternativa de permitir que o consumidor enfileirasse um job `run_as="actor"` usando o `actor_id` do envelope foi descartada. Isso transformaria o contexto de sistema em um emissor de jobs em nome de qualquer pessoa da organização (a partir de um dado gravado, e não de uma autenticação) e ainda faria a reação depender de a pessoa que disparou o evento ter permissões no livro contábil — algo que uma automação da organização não deve exigir. As superfícies liberadas aqui já pertenciam à app dona (a sequência) ou já aceitavam o contexto de sistema na escrita (o livro).

## Eventos do núcleo

Os serviços de lifecycle do núcleo publicam quatro tipos de evento (`skeleton/events/core_events.py`), sempre na transação da mudança e **um por alteração efetiva**: repetir o estado atual ou alterar apenas a configuração de uma app não publica evento, e um rollback não deixa rastro.

| Tipo | Tabela | `data` | Quando é publicado |
|---|---|---|---|
| `membership.created` | `sk_tenancy.membership` | `user` | O vínculo passou a ativo: criado, restaurado ou reativado por `provision_membership` (convite, designação, bootstrap), `create_membership`, `update_membership` ou pela criação de uma conta de serviço |
| `membership.deactivated` | `sk_tenancy.membership` | `user` | Um vínculo ativo foi desativado (`update_membership`, `deactivate_service_account`), removido (`delete_membership`) ou deixado (`leave_organization`) |
| `app.enabled` | `sk_system.appinstallation` | `app_label` | A organização ativou a app (`configure_application`, pela interface ou pelo `ApplicationAdministrationService`, ou via `manage.py set_app_availability --enable`), ou a [cobrança](billing.md#ciclo-de-vida) a ativou ao conceder o direito após o pagamento (`activate_for_entitlement`) |
| `app.disabled` | `sk_system.appinstallation` | `app_label` | A organização desativou a app, ou a app foi retirada com preservação de dados (first-party em `SKELETON_BUNDLED_RETAINED` ou `schema_retained` no inventário do agente): um evento para cada organização em que ela estava ativa |

- O payload contém apenas identificadores: `user` é o UUID da identidade e `record_sysid` é o vínculo ou a ativação. O consumidor relê e reautoriza o restante. Os `TableSpec` dessas duas tabelas declaram apenas esses campos em `read_fields`.
- Restaurar um vínculo já inativo não publica `membership.created`, assim como remover um vínculo inativo não publica `membership.deactivated`.
- O upgrade de ativação (`upgrade_app_activation`) não publica `app.enabled`, pois apenas materializa um estado que já vigorava.
- Esses tipos **não** ficam disponíveis para webhooks (`webhook=False`): a entrega de webhooks exige reautorizar a leitura do registro pelo principal do endpoint, e essas tabelas não têm autorização por registro.

```python
from skeleton.events.registry import register_consumer

register_consumer(
    "membership.created", "tasks.welcome_member", welcome, owner_app="tasks"
)  # welcome(envelope) relê o vínculo
```

Use o código do evento como texto (primeira coluna da tabela acima); o módulo `skeleton.events.core_events` é interno.

Os [avisos padrão da organização](notifications.md#avisos-padrão-da-organização) são emitidos no mesmo ponto em que os eventos de vínculo são publicados, dentro da mesma transação, e não por meio de um consumidor.

## Eventos da cobrança

A [cobrança](billing.md#eventos) publica `billing.invoice_finalized` e `billing.invoice_paid` sobre `sk_billing.invoice` (`number`, `status`, `currency`, `total`), `billing.subscription_changed` sobre `sk_billing.subscription` (`status`), `billing.access_state_changed` sobre `sk_billing.billingaccount` (`access_state`) e `billing.payment_refunded` e `billing.payment_duplicated` sobre `sk_billing.payment` (`invoice`, `status`, `provider`, `amount`, `currency`), todos na transação do motor. Dados de contato da conta nunca entram no payload. Esses tipos não vão para webhooks; um emissor fiscal externo pode consumir `billing.invoice_paid` em processo via `register_consumer`.

## Eventos de fluxos de processo

O motor de [fluxos](flows.md#integrações) publica `flows.instance_started`, `flows.instance_completed`, `flows.instance_cancelled` e `flows.instance_failed` sobre `sk_flows.flowinstance`, na transação do comando, levando `flow_key`, `flow_version`, `status`, `outcome`, `subject_table` e `subject_sysid`. Dados de formulário nunca entram no payload.

Esses eventos não são expostos a webhooks; um fluxo pode mapear esses mesmos momentos para tipos de evento da própria app sobre o registro de negócio (`subject_events`), que funcionam como eventos comuns e alcançam webhooks. O motor também consome `app.enabled` para reagendar o que os fluxos da app aguardavam.

## Retenção

O job periódico `events.retention` roda a cada 6 horas por organização e é o único caminho de exclusão física de `DomainEvent`. Ele remove, em lotes de 1 000 registros por organização, os eventos cujo `occurred_at` excede `SKELETON_EVENT_RETENTION_DAYS` (30 dias por padrão). Como o volume de jobs gerados é proporcional ao produto de eventos por consumidores, a retenção limita o crescimento do outbox.

## Testes

Use `skeleton.testing.events.capture_events()` para coletar a lista de eventos publicados dentro de um bloco e `skeleton.testing.jobs.run_pending_jobs()` para executar as entregas pendentes.

## Decisão: auditoria × eventos (ADR)

**Contexto.** Reações a mudanças de negócio exigem intenção explícita e um payload seguro. Signals de `save` são proibidos (criariam uma segunda fonte de eventos), e o `AuditEvent` armazena valores redigidos sem semântica de negócio.

**Decisão.** O serviço publica eventos de domínio explícitos na mesma transação da escrita. A auditoria por trigger permanece como fonte de verdade do histórico, enquanto o evento atua apenas como gatilho de reações, carregando campos declarados e não sensíveis junto de `record_revision` e `request_id` para correlação. A entrega utiliza um job por par de evento e consumidor.

**Consequências.** Esquecer de publicar um evento não compromete o histórico de auditoria, apenas a reação associada. Um job por consumidor isola falhas individuais ao custo de mais linhas na fila, enquanto a retenção controla o crescimento do outbox.
