# API de serviços: Agenda

## Visão geral
`CalendarService` é o contrato em processo da agenda (#111). Outras apps o usam para ler calendários e ocorrências, criar, alterar e cancelar eventos e consultar horários ocupados, sempre com a autorização do ator que está no contexto. As páginas da própria agenda usam o mesmo serviço.

## Dependência
`app_id`: `org.skeleton.calendar`. Faixa recomendada: `{"org.skeleton.calendar": ">=0.5.0,<0.6.0"}`. Importe só `skeleton.contrib.calendar.service_api`.

## Serviços

### `CalendarService`
Todos os métodos exigem a app ativa na organização (`Unavailable`), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden`). Nenhum abre `system_context`, faz commit ou executa efeito externo. O mascaramento vale também aqui: um evento privado, ou qualquer evento do calendário pessoal de outra pessoa sem delegação, sai como "Ocupado", sem título, descrição nem local.

#### `my_calendars(*, actor) -> tuple[CalendarInfo, ...]`
Calendários que o ator lê, o pessoal padrão primeiro (criado na primeira chamada).
- **Autorização:** TablePermission read em `skeleton_calendar.calendar`.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** cria o calendário pessoal padrão na primeira chamada.

#### `upcoming(*, actor, start, end, calendar_ids=None) -> tuple[EventOccurrence, ...]`
Ocorrências legíveis em `[start, end)`, no máximo 400 dias, com a recorrência expandida.
- **Autorização:** leitura do calendário ou convite.
- **Erros:** `InvalidInput` (janela inválida, sem fuso ou acima de 400 dias, conferida mesmo sem eventos; mais de 5000 ocorrências na janela, nunca cortadas em silêncio, #291); `NotFound` (calendário ilegível em `calendar_ids`).
- **Efeitos:** nenhum.

#### `create_event(*, actor, calendar_id, data, idempotency_key=None) -> EventInfo`
Cria um evento; o ator é o organizador. `data` aceita `title`, `description`, `location`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `is_private`, `status`, `rrule`, `rdates` e `exdates`.
- **Autorização:** update no calendário; no calendário pessoal de outra pessoa, só delegação.
- **Erros:** `NotFound` (calendário ilegível ou de outra organização); `Forbidden` (só leitura); `InvalidInput`, inclusive para `tzid` desconhecido, datas ou `UNTIL` fora de 1900–2200, regra sem nenhuma ocorrência e `rdates`/`exdates` que não são datas ISO 8601 (#236).
- **Efeitos:** grava o evento. A mesma `idempotency_key` do mesmo ator devolve o evento já criado.

#### `update_event(*, actor, event_id, data, expected_revision) -> EventInfo`
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict` (revisão desatualizada); `InvalidInput` (os mesmos motivos de `create_event`).
- **Efeitos:** grava, incrementa `sequence` e notifica os participantes.

#### `cancel_event(*, actor, event_id, expected_revision) -> EventInfo`
- **Autorização, erros:** como `update_event`.
- **Efeitos:** estado `cancelled` e notificação dos participantes.

#### `event(*, actor, event_id) -> EventInfo`
Um evento como o ator pode vê-lo (bloco "Ocupado" quando só vê o horário).
- **Autorização:** leitura do calendário ou convite.
- **Erros:** `NotFound` (inexistente, ilegível ou de outra organização, sempre a mesma resposta); `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `delete_event(*, actor, event_id, expected_revision) -> None`
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden` se for só legível; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o evento com lembretes, participantes e substituições e notifica o cancelamento (#292).

#### `edit_occurrence(*, actor, event_id, recurrence_id, mode, data, expected_revision) -> EventInfo`
`mode` é `this` (só esta ocorrência), `following` (esta e as seguintes, dividindo a série) ou `all` (a série inteira); `recurrence_id` é o início original da ocorrência, com fuso.
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict("stale_revision")` com a revisão da série desatualizada; `InvalidInput` (modo desconhecido, instante sem fuso, ocorrência que a série não tem).
- **Efeitos:** grava a substituição, a divisão ou o mestre, e a revisão da série muda (#294).

#### `cancel_occurrence(*, actor, event_id, recurrence_id, expected_revision) -> EventInfo`
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput`.
- **Efeitos:** acrescenta o EXDATE ao mestre e devolve o mestre.

#### `attendees(*, actor, event_id) -> tuple[AttendeeInfo, ...]`
Convites do evento, no máximo 500; vazio quando o ator só vê o evento como "Ocupado".
- **Autorização:** leitura do evento.
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `invite(*, actor, event_id, user_ids, optional=False) -> tuple[AttendeeInfo, ...]`
Convida até 100 pessoas ativas da organização; convites existentes são mantidos e só os novos voltam.
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `InvalidInput` (pessoa de outra organização, inativa, conta de serviço, lista vazia ou acima de 100).
- **Efeitos:** grava os convites e notifica os convidados novos.

#### `remove_attendee(*, actor, attendee_id, expected_revision) -> None`
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound` para convite inexistente ou de evento ilegível (a mesma resposta); `Forbidden`; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o convite; a pessoa deixa de ler o evento.

#### `respond(*, actor, event_id, response, expected_revision) -> AttendeeInfo`
Responde (`accepted`, `declined`, `tentative`, `needs_action`) ao convite do **próprio** ator.
- **Autorização:** leitura do evento e convite do ator.
- **Erros:** `NotFound` para evento ilegível ou sem convite do ator (igual a inexistente); `Conflict("stale_revision")` com a revisão do convite; `InvalidInput`.
- **Efeitos:** grava a resposta.

#### `reminders(*, actor, event_id) -> tuple[ReminderInfo, ...]`
- **Autorização:** leitura do evento; vazio quando o ator só o vê como "Ocupado".
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `add_reminder(*, actor, event_id, minutes_before) -> ReminderInfo`
De 0 a 40320 minutos (28 dias); um lembrete com a mesma antecedência é devolvido em vez de duplicado.
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `InvalidInput`.
- **Efeitos:** grava o lembrete.

#### `remove_reminder(*, actor, reminder_id, expected_revision) -> None`
- **Autorização:** update no calendário do evento.
- **Erros:** `NotFound` para lembrete inexistente ou de evento ilegível (a mesma resposta); `Forbidden`; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o lembrete.

#### `free_busy(*, actor, user_ids, start, end) -> tuple[BusyBlock, ...]`
Horários ocupados das pessoas nos calendários pessoais delas, nunca títulos ou detalhes; até 50 pessoas.
- **Autorização:** TablePermission read em `skeleton_calendar.calendar`; só pessoas com vínculo ativo nesta organização.
- **Erros:** `Forbidden`; `InvalidInput` (janela acima de 400 dias, mais de 50 pessoas ou ocorrências demais, #291).
- **Efeitos:** nenhum.

## DTOs
- `CalendarInfo`: `sysid`, `name`, `kind`, `is_default`, `color`, `timezone`, `owner_id`, `revision` e `writable` (se `create_event` aceitaria o calendário para o ator, #344).
- `EventInfo`: `sysid`, `calendar_id`, `title`, `description`, `location`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status`, `organizer_id`, `revision`.
- `EventOccurrence`: `event_id`, `calendar_id`, `recurrence_id`, `title`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `status`, e, só para dia inteiro, `start_date` (primeiro dia) e `end_date` (dia seguinte ao último, exclusivo); `None` em evento com hora (#312).
- `BusyBlock`: `user_id`, `starts_at`, `ends_at`. Um bloco de dia inteiro usa a meia-noite UTC das datas.
- `AttendeeInfo`: `sysid`, `event_id`, `user_id`, `response`, `optional`, `revision`.
- `ReminderInfo`: `sysid`, `event_id`, `minutes_before`, `channel_hint`, `revision`.

## Histórico de versões
- 0.5.0: `my_calendars`, `upcoming`, `create_event`, `update_event`, `cancel_event`, `free_busy`.
- 0.6.0 (próxima *minor*, com o núcleo): `EventOccurrence.start_date` e `end_date` (#312) e `CalendarInfo.writable`, campos com valor padrão; `event`, `delete_event`, `edit_occurrence`, `cancel_occurrence`, `attendees`, `invite`, `remove_attendee`, `respond`, `reminders`, `add_reminder` e `remove_reminder`, com `AttendeeInfo` e `ReminderInfo` (#344). Uma `idempotency_key` repetida em corrida devolve o evento já criado.
