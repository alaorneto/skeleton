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
- **Erros:** `InvalidInput` (janela inválida, sem fuso ou longa demais); `NotFound` (calendário ilegível em `calendar_ids`).
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

#### `free_busy(*, actor, user_ids, start, end) -> tuple[BusyBlock, ...]`
Horários ocupados das pessoas nos calendários pessoais delas, nunca títulos ou detalhes; até 50 pessoas.
- **Autorização:** TablePermission read em `skeleton_calendar.calendar`; só pessoas com vínculo ativo nesta organização.
- **Erros:** `Forbidden`; `InvalidInput`.
- **Efeitos:** nenhum.

## DTOs
- `CalendarInfo`: `sysid`, `name`, `kind`, `is_default`, `color`, `timezone`, `owner_id`, `revision`.
- `EventInfo`: `sysid`, `calendar_id`, `title`, `description`, `location`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status`, `organizer_id`, `revision`.
- `EventOccurrence`: `event_id`, `calendar_id`, `recurrence_id`, `title`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `status`.
- `BusyBlock`: `user_id`, `starts_at`, `ends_at`.

## Histórico de versões
- 0.5.0: `my_calendars`, `upcoming`, `create_event`, `update_event`, `cancel_event`, `free_busy`.
