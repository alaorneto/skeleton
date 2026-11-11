# API de serviços: Agenda

## Visão geral
`CalendarService` é o contrato em processo da agenda. Outras apps o usam para ler os calendários do ator na organização atual, sempre com a autorização do ator que está no contexto.

## Dependência
`app_id`: `org.skeleton.calendar`. Faixa recomendada: `{"org.skeleton.calendar": ">=0.5.0,<0.6.0"}`. Importe só `skeleton.contrib.calendar.service_api`.

## Serviços

### `CalendarService`
Todos os métodos exigem a app ativa na organização (`Unavailable`), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden`).

#### `my_calendars(*, actor) -> tuple[CalendarInfo, ...]`
Calendários que o ator lê, o pessoal padrão primeiro (criado na primeira chamada).
- **Autorização:** `TablePermission` read em `skeleton_calendar.calendar`.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** cria o calendário pessoal padrão na primeira chamada.

## DTOs
`CalendarInfo`: `sysid`, `name`, `kind`, `is_default`, `color`, `timezone`, `owner_id`, `revision`.

## Histórico de versões
- 0.5.0: `my_calendars`.
