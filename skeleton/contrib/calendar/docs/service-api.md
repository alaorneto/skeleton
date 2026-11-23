# API de serviços: Agenda

## Visão geral
`CalendarService` é o contrato em processo da agenda (#111). Outras apps o usam para ler calendários e ocorrências, criar, alterar e cancelar eventos e consultar horários ocupados, sempre com a autorização do ator que está no contexto. As páginas da própria agenda usam o mesmo serviço.

## Dependência
`app_id`: `org.skeleton.calendar`. Faixa recomendada: `{"org.skeleton.calendar": ">=0.6.0,<0.7.0"}`. Importe só `skeleton.contrib.calendar.service_api`.

## Serviços

### `CalendarService`
Todos os métodos exigem a app ativa na organização (`Unavailable`), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden`); só os três métodos de eventos gerenciados e `claim_calendar` também aceitam `actor=None` dentro de `system_context` da organização. Nenhum abre `system_context`, faz commit ou executa efeito externo. O mascaramento vale também aqui: um evento privado, ou qualquer evento do calendário pessoal de outra pessoa sem delegação, sai como "Ocupado", sem título, descrição nem local.

#### `my_calendars(*, actor) -> tuple[CalendarInfo, ...]`
Calendários que o ator lê, o pessoal padrão primeiro (criado na primeira chamada).
- **Autorização:** TablePermission read em `skeleton_calendar.calendar`.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** cria o calendário pessoal padrão na primeira chamada, para qualquer ator com `TablePermission` de leitura, inclusive conta de serviço (#345).

#### `calendars(*, actor) -> tuple[CalendarInfo, ...]`
Todos os calendários que o ator lê na organização, sem criar nenhum (#539); `my_calendars` é quem cria o pessoal padrão.
- **Autorização:** TablePermission read em `skeleton_calendar.calendar` e visibilidade dos registros.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** nenhum.

#### `create_shared_calendar(*, actor, name, owner_user_id, timezone, writer_group_ids=(), reader_group_ids=(), idempotency_key, writer_user_ids=(), reader_user_ids=(), source_app="", source_path="") -> CalendarInfo`
Cria um calendário de atendimento (#539): compartilhado, **privado**, de propriedade de `owner_user_id` (membro ativo), com `read` e `update` concedidos por `RecordGrant` aos grupos de `writer_group_ids` e `read` aos de `reader_group_ids`, pelo `AccessService.grant_record_access` do núcleo; `writer_user_ids` e `reader_user_ids` fazem o mesmo para pessoas (#670). São até 20 grupos e pessoas ao todo. Assim a equipe de apoio escreve na agenda de quem atende sem abrir o calendário pessoal dessa pessoa. O membro do grupo ainda precisa da `TablePermission` da agenda (default deny).

Com `source_app` (#670), o calendário é **gerenciado** pela app que o criou: as telas da agenda o mostram com "gerenciado por <app>" e "abrir na origem" (`source_path`, dentro de `apps/<source_app>/`) e nunca o renomeiam, mudam o acesso ou excluem; o gateway da tabela recusa alterar ou excluir. A app que espelha o acesso do calendário em regras próprias passa o próprio label, e só ela muda o calendário depois.
- **Autorização:** `create` em `skeleton_calendar.calendar` **e** a capacidade `access.manage`.
- **Erros:** `Forbidden` sem uma das duas; `InvalidInput` (nome, fuso, dono que não é membro ativo, grupo de outra organização, pessoa que não é membro ativo, grupos e pessoas demais, chave inválida, app de origem desconhecida, inativa ou a própria agenda, caminho fora de `apps/<source_app>/`).
- **Efeitos:** cria o calendário e as concessões na transação do chamador; a mesma `idempotency_key` do mesmo ator devolve o calendário já criado (o `sysid` é derivado da organização, do ator e da chave). O administrador continua sem escrever em calendário **pessoal** alheio.

#### `claim_calendar(*, actor, calendar_id, source_app, source_path="", expected_revision) -> CalendarInfo`
Marca como gerenciado pela app um calendário compartilhado que ela criou antes do marcador existir (#670), sem migration de dados do lado dela: a app percorre os calendários que guardou e chama o método para cada um. Só a app que criou o calendário passa o próprio label (a API é em processo: conferido em revisão de código). Repetir com a mesma app e o mesmo caminho devolve o calendário sem mudar nada, qualquer que seja a revisão; a mesma app com outro caminho atualiza o caminho.
- **Autorização:** `update` no calendário e a capacidade `access.manage`; em `system_context` da organização (`actor=None`), aceito.
- **Erros:** `NotFound` (calendário inexistente, ilegível ou de outra organização); `Forbidden` sem `update` ou sem a capacidade; `Conflict("managed_calendar")` para calendário de outra app; `Conflict("stale_revision")`; `InvalidInput` (calendário pessoal, app de origem desconhecida, inativa ou a própria agenda, caminho fora de `apps/<source_app>/`).
- **Efeitos:** grava a origem (auditada) na transação do chamador.

#### `calendar_links(*, actor, calendar_id) -> CalendarLinks`
Os caminhos, relativos à organização, da página do calendário e da página do link de assinatura dele (#669, #670), para uma app levar a pessoa até lá. Prefixe com o endereço da organização (`/o/<org>/` ou o host da organização). Na página do link, a própria pessoa cria o link dela para aquele calendário.
- **Autorização:** leitura do calendário.
- **Erros:** `NotFound` (calendário inexistente, ilegível ou de outra organização).
- **Efeitos:** nenhum.

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

#### `free_busy(*, actor, user_ids, start, end, include_attending=False) -> tuple[BusyBlock, ...]`
Horários ocupados das pessoas nos calendários pessoais delas, nunca títulos ou detalhes; até 50 pessoas. Com `include_attending=True` (#538), somam-se os eventos de qualquer calendário da organização em que a pessoa é participante com resposta diferente de `declined` (`accepted`, `tentative` ou sem resposta); a recorrência é expandida, e evento cancelado ou ocorrência excluída não contam. Quem é dono do calendário e também participante conta uma vez. O padrão `False` devolve exatamente o que devolvia antes.
- **Autorização:** TablePermission read em `skeleton_calendar.calendar`; só pessoas com vínculo ativo nesta organização. Exige ator: não aceita `system_context`.
- **Erros:** `Forbidden`; `InvalidInput` (janela acima de 400 dias, mais de 50 pessoas ou ocorrências demais, #291).
- **Efeitos:** nenhum. O número de consultas não cresce com o número de pessoas.

#### `calendar_busy(*, actor, calendar_ids, start, end) -> tuple[CalendarBusyBlock, ...]`
Horários ocupados de calendários que o ator lê (salas, equipamentos, calendários de atendimento), só horários (#538); de 1 a 50 calendários, na mesma janela máxima de 400 dias.
- **Autorização:** leitura de cada calendário. Exige ator.
- **Erros:** `NotFound` (algum calendário ilegível, inexistente ou de outra organização); `InvalidInput` (janela, quantidade de calendários ou ocorrências demais).
- **Efeitos:** nenhum. O número de consultas não cresce com o número de calendários.

#### `put_managed_event(*, actor, source_app, source_key, calendar_id, data, attendee_user_ids=(), source_path="") -> EventInfo`
Cria ou atualiza o **evento gerenciado** que projeta um registro de outra app (#537): o único evento vivo de `(source_app, source_key)`. `data` aceita só `title`, `location`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status` e `is_private` (padrão **verdadeiro**); `description` é sempre recusada. Evento gerenciado não tem recorrência nem lembrete.
- **Autorização:** update no calendário (as regras de `create_event`); em `system_context` da organização (`actor=None`), aceito, e o organizador é o dono do calendário. Só a app dona do registro passa o próprio label: a API é em processo e o núcleo não consegue provar quem chama, então a regra vale por revisão de código.
- **No portal (desde 0.6.0, #741):** só de dentro de um método `allow_external` de `source_app`, conferido pela cadeia de chamadas da API de serviços, e só num calendário compartilhado que ela gerencia (`CalendarInfo.source_app`); a pessoa externa é o ator da auditoria e o organizador é o dono do calendário. Os participantes ficam como estão: `attendee_user_ids` precisa ser vazio. Um evento da chave que esteja num calendário que a app não gerencia não é movido (`Forbidden`). A chamada direta de uma view, ou de um método sem a marca, responde `Forbidden("external_not_allowed")`.
- **Erros:** `NotFound` (calendário ilegível, de outra organização ou, no portal, não gerenciado pela app); `Forbidden` (só leitura; no portal, `source_app` que não é a app que chama); `InvalidInput` para participantes no portal, `description`, campo desconhecido, `source_app` desconhecida, inativa nesta organização ou igual a `skeleton_calendar`, `source_key` fora de 1 a 200 letras, dígitos e `: . _ -`, `source_path` fora de `apps/<source_app>/` (com `..`, esquema, barra inicial ou invertida), horários inválidos e participante que não é membro ativo (ou é conta de serviço).
- **Efeitos:** repetir com os mesmos dados não muda revisão nem `sequence`; dados diferentes atualizam e incrementam os dois. Outro `calendar_id` (ou outro `source_path`) exclui o evento antigo e cria outro, porque calendário e caminho são imutáveis. Os participantes ficam exatamente em `attendee_user_ids`, todos `accepted`, e **nenhuma** notificação é criada. Duas chamadas simultâneas com a mesma chave deixam um evento só: a perdedora relê e atualiza.

#### `remove_managed_event(*, actor, source_app, source_key) -> None`
- **Autorização:** update no calendário do evento; em `system_context` da organização, aceito; no portal (desde 0.6.0, #741), só de um método `allow_external` de `source_app`, e só num calendário que ela gerencia.
- **Erros:** `Forbidden` (evento só legível; no portal, `source_app` que não é a app que chama, ou `external_not_allowed` fora de um método `allow_external` de app); `InvalidInput` (app de origem ou chave inválidas). Sem evento, com um evento que o ator não lê ou, no portal, fora de um calendário da app, nada muda.
- **Efeitos:** exclusão lógica do evento e dos participantes, sem notificação; idempotente.

#### `managed_event(*, actor, source_app, source_key) -> EventInfo | None`
- **Autorização:** leitura do calendário ou convite; em `system_context` da organização, aceito; no portal (desde 0.6.0, #741), só de um método `allow_external` de `source_app`, e só o evento num calendário que ela gerencia (fora dele, `None`).
- **Erros:** `InvalidInput` (app de origem ou chave inválidas); no portal, `Forbidden` para `source_app` que não é a app que chama e `external_not_allowed` fora de um método `allow_external` de app.
- **Efeitos:** nenhum.

Os métodos gerais (`update_event`, `cancel_event`, `delete_event`, `edit_occurrence`, `cancel_occurrence`, `invite`, `remove_attendee`, `respond`, `add_reminder`, `remove_reminder`) respondem `Conflict("managed_event")` para um evento gerenciado: ele muda só pelo serviço da app de origem.

**Contrato de privacidade dos eventos gerenciados.** O título descreve o tipo do compromisso ("Atendimento", "Reunião"), nunca identifica um terceiro; o detalhe fica na app de origem, alcançado por `source_path`. O evento é privado por padrão: quem não é o dono do calendário nem o organizador vê "Ocupado", e o ICS sai com `CLASS:PRIVATE`. O feed ICS nunca exporta `source_path` nem o label da app.

## DTOs
- `CalendarInfo`: `sysid`, `name`, `kind`, `is_default`, `color`, `timezone`, `owner_id`, `revision`, `writable` (se `create_event` aceitaria o calendário para o ator, #344) e `source_app`/`source_path` (#670; vazios num calendário que nenhuma app gerencia).
- `CalendarLinks`: `calendar_path` e `subscription_path`, relativos à organização (#670).
- `EventInfo`: `sysid`, `calendar_id`, `title`, `description`, `location`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status`, `organizer_id`, `revision`, e `source_app`/`source_path` (#537; vazios em evento comum e para quem só vê "Ocupado").
- `EventOccurrence`: `event_id`, `calendar_id`, `recurrence_id`, `title`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `status`, e, só para dia inteiro, `start_date` (primeiro dia) e `end_date` (dia seguinte ao último, exclusivo); `None` em evento com hora (#312); `source_app` e `source_path` como em `EventInfo`.
- `BusyBlock`: `user_id`, `starts_at`, `ends_at`. Um bloco de dia inteiro usa a meia-noite UTC das datas.
- `CalendarBusyBlock`: `calendar_id`, `starts_at`, `ends_at` (#538).
- `AttendeeInfo`: `sysid`, `event_id`, `user_id`, `response`, `optional`, `revision`.
- `ReminderInfo`: `sysid`, `event_id`, `minutes_before`, `channel_hint`, `revision`.

## Histórico de versões
- 0.1.0 (primeira versão do núcleo, #569): `my_calendars`, `upcoming`, `create_event`, `update_event`, `cancel_event`, `free_busy`.
- 0.1.0, também: `EventOccurrence.start_date` e `end_date` (#312) e `CalendarInfo.writable`, campos com valor padrão; `event`, `delete_event`, `edit_occurrence`, `cancel_occurrence`, `attendees`, `invite`, `remove_attendee`, `respond`, `reminders`, `add_reminder` e `remove_reminder`, com `AttendeeInfo` e `ReminderInfo` (#344). Uma `idempotency_key` repetida em corrida devolve o evento já criado. Eventos gerenciados (#537): `put_managed_event`, `remove_managed_event`, `managed_event`, `EventInfo.source_app`/`source_path` e `EventOccurrence.source_app`/`source_path` (campos com valor padrão), `Conflict("managed_event")` nos métodos gerais. Disponibilidade (#538): `free_busy(include_attending=...)`, com padrão compatível, e `calendar_busy` com `CalendarBusyBlock`. Calendários de atendimento (#539): `calendars` e `create_shared_calendar`.
- 0.6.0: eventos gerenciados pelo portal (#741): `put_managed_event`, `remove_managed_event` e `managed_event` aceitam o contexto externo só de um método `allow_external` da app que gerencia o calendário; nada muda fora do portal.
- 0.4.0: calendários gerenciados (#670): `create_shared_calendar(writer_user_ids=..., reader_user_ids=..., source_app=..., source_path=...)`, com padrões compatíveis, `claim_calendar`, `calendar_links` com `CalendarLinks` e `CalendarInfo.source_app`/`source_path` (campos com valor padrão).
