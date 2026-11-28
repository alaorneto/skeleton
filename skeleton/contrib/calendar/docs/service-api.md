# API de serviços: Agenda

## Visão geral
`CalendarService` é o contrato em processo da agenda. Outras apps o utilizam para consultar calendários e ocorrências, criar, alterar e cancelar eventos e verificar horários ocupados, sempre respeitando a autorização do ator no contexto. As telas da própria agenda consomem esse mesmo serviço.

## Dependência
`app_id`: `org.skeleton.calendar`. Faixa recomendada: `{"org.skeleton.calendar": ">=0.10.0,<0.11.0"}`. Importe exclusivamente `skeleton.contrib.calendar.service_api`.

A agenda é uma biblioteca first-party: ela é instalada automaticamente com a app que a declara, não tem disponibilidade nem ativação próprias e fica efetiva em uma organização enquanto houver uma app ativa ali que dependa dela.

## Serviços

### `CalendarService`
Todos os métodos exigem que a agenda esteja efetiva na organização — ou seja, que exista uma app ativa ali dependendo dela (caso contrário, `Unavailable`) —, que o contexto tenant tenha sido aberto pelo chamador e que `actor` coincida com o ator do contexto (`Forbidden`). Apenas os três métodos de eventos gerenciados e `claim_calendar` também aceitam `actor=None` dentro de `system_context` da organização.

Nenhum método abre `system_context`, faz commit ou dispara efeito externo. O mascaramento de privacidade também vale aqui: eventos privados, ou qualquer evento no calendário pessoal de outra pessoa sem delegação, são devolvidos apenas como "Ocupado", sem título, descrição nem local.

#### `my_calendars(*, actor) -> tuple[CalendarInfo, ...]`
Devolve os calendários que o ator pode ler, começando pelo calendário pessoal padrão (criado automaticamente na primeira chamada).
- **Autorização:** `TablePermission` de `read` em `skeleton_calendar.calendar`.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** cria o calendário pessoal padrão na primeira chamada para qualquer ator com `TablePermission` de leitura, inclusive contas de serviço.

#### `calendars(*, actor) -> tuple[CalendarInfo, ...]`
Devolve todos os calendários que o ator pode ler na organização, sem criar nenhum (a criação do pessoal padrão cabe a `my_calendars`).
- **Autorização:** `TablePermission` de `read` em `skeleton_calendar.calendar` e visibilidade dos registros.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** nenhum.

#### `create_shared_calendar(*, actor, name, owner_user_id, timezone, writer_group_ids=(), reader_group_ids=(), idempotency_key, writer_user_ids=(), reader_user_ids=(), source_app="", source_path="") -> CalendarInfo`
Cria um calendário compartilhado de atendimento, com visibilidade **privada**, pertencente a `owner_user_id` (membro ativo). Concede `read` e `update` por `RecordGrant` aos grupos de `writer_group_ids` e às pessoas de `writer_user_ids`, e `read` aos grupos de `reader_group_ids` e às pessoas de `reader_user_ids`, usando `AccessService.grant_record_access` do núcleo, até o limite total de 20 grupos e pessoas. Assim, a equipe de apoio pode operar a agenda de quem atende sem acessar o calendário pessoal dessa pessoa. Mesmo com a concessão, cada membro continua precisando da `TablePermission` da agenda (default deny).

Quando `source_app` é informado, o calendário nasce **gerenciado** pela app criadora: as telas da agenda exibem "gerenciado por <app>" e "abrir na origem" (`source_path` dentro de `apps/<source_app>/`), impedindo renomear, alterar compartilhamento ou excluir; o gateway da tabela também recusa alterações e exclusões. A app que espelha o acesso em regras próprias informa seu próprio label e passa a ser a única a gerenciar o calendário.
- **Autorização:** `create` em `skeleton_calendar.calendar` **e** capacidade `access.manage`.
- **Erros:** `Forbidden` se faltar uma das duas permissões; `InvalidInput` para nome ou fuso inválidos, dono que não seja membro ativo, grupo de outra organização, pessoa que não seja membro ativo, excesso de grupos e pessoas, chave inválida, app de origem desconhecida, inativa ou igual à própria agenda, ou caminho fora de `apps/<source_app>/`.
- **Efeitos:** cria o calendário e as concessões na transação do chamador. Repetir a mesma `idempotency_key` com o mesmo ator devolve o calendário já criado (o `sysid` é derivado da organização, do ator e da chave). O administrador continua impedido de escrever em calendários **pessoais** alheios.

#### `claim_calendar(*, actor, calendar_id, source_app, source_path="", expected_revision) -> CalendarInfo`
Marca como gerenciado pela app um calendário compartilhado criado antes da existência desse marcador, sem exigir migration de dados na app consumidora: ela apenas percorre os calendários guardados e chama o método para cada um. Como a API é em processo, apenas a app dona do calendário deve passar o próprio label (verificado em revisão de código). Repetir a chamada com a mesma app e o mesmo caminho devolve o calendário sem alterações, independentemente da revisão; informar outro caminho pela mesma app atualiza o caminho.
- **Autorização:** `update` no calendário e capacidade `access.manage`; também aceito em `system_context` da organização (`actor=None`).
- **Erros:** `NotFound` (calendário inexistente, ilegível ou de outra organização); `Forbidden` sem `update` ou sem a capacidade; `Conflict("managed_calendar")` se o calendário pertencer a outra app; `Conflict("stale_revision")`; `InvalidInput` (calendário pessoal, app de origem desconhecida, inativa ou igual à própria agenda, ou caminho fora de `apps/<source_app>/`).
- **Efeitos:** grava a origem (auditada) na transação do chamador.

#### `calendar_links(*, actor, calendar_id) -> CalendarLinks`
Devolve os caminhos relativos à organização para a página do calendário e para a página de criação do link de assinatura, permitindo que outra app direcione a pessoa até lá. Prefixe os caminhos com o endereço da organização (`/o/<org>/` ou o host da organização). Na página do link, a própria pessoa gera o link individual daquele calendário.
- **Autorização:** leitura do calendário.
- **Erros:** `NotFound` (calendário inexistente, ilegível ou de outra organização).
- **Efeitos:** nenhum.

#### `upcoming(*, actor, start, end, calendar_ids=None) -> tuple[EventOccurrence, ...]`
Devolve as ocorrências legíveis no intervalo `[start, end)`, com janela máxima de 400 dias e recorrências expandidas.
- **Autorização:** leitura do calendário ou convite no evento.
- **Erros:** `InvalidInput` (janela inválida, sem fuso ou superior a 400 dias — validada mesmo quando não há eventos —, ou mais de 5.000 ocorrências na janela, nunca truncadas em silêncio); `NotFound` (algum calendário ilegível em `calendar_ids`).
- **Efeitos:** nenhum.

#### `create_event(*, actor, calendar_id, data, idempotency_key=None) -> EventInfo`
Cria um evento tendo o ator como organizador. `data` aceita `title`, `description`, `location`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `is_private`, `status`, `rrule`, `rdates` e `exdates`.
- **Autorização:** `update` no calendário; no calendário pessoal de outra pessoa, somente via delegação.
- **Erros:** `NotFound` (calendário ilegível ou de outra organização); `Forbidden` (acesso somente leitura); `InvalidInput`, inclusive para `tzid` desconhecido, datas ou `UNTIL` fora do intervalo 1900–2200, regra sem nenhuma ocorrência válida ou `rdates`/`exdates` fora do formato ISO 8601.
- **Efeitos:** grava o evento. Repetir a mesma `idempotency_key` com o mesmo ator devolve o evento já criado.

#### `update_event(*, actor, event_id, data, expected_revision) -> EventInfo`
Atualiza os dados de um evento.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict` (revisão desatualizada); `InvalidInput` (mesmos critérios de `create_event`).
- **Efeitos:** grava as alterações, incrementa `sequence` e notifica os participantes.

#### `cancel_event(*, actor, event_id, expected_revision) -> EventInfo`
Marca um evento como cancelado.
- **Autorização e erros:** os mesmos de `update_event`.
- **Efeitos:** altera o estado para `cancelled` e notifica os participantes.

#### `event(*, actor, event_id) -> EventInfo`
Devolve um evento conforme a visibilidade do ator (como bloco "Ocupado" quando ele tem acesso apenas ao horário).
- **Autorização:** leitura do calendário ou convite no evento.
- **Erros:** `NotFound` (inexistente, ilegível ou de outra organização — sempre a mesma resposta); `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `delete_event(*, actor, event_id, expected_revision) -> None`
Exclui logicamente um evento e seus dependentes.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden` se o acesso for somente leitura; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o evento junto com lembretes, participantes e substituições da série, e notifica o cancelamento.

#### `edit_occurrence(*, actor, event_id, recurrence_id, mode, data, expected_revision) -> EventInfo`
Edita uma ocorrência de um evento recorrente. `mode` aceita `this` (apenas esta ocorrência), `following` (esta e as seguintes, dividindo a série) ou `all` (a série inteira); `recurrence_id` é o início original da ocorrência, com fuso horário.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict("stale_revision")` com a revisão da série desatualizada; `InvalidInput` (modo desconhecido, instante sem fuso ou ocorrência inexistente na série).
- **Efeitos:** grava a substituição, a divisão ou o mestre atualizado, avançando a revisão da série.

#### `cancel_occurrence(*, actor, event_id, recurrence_id, expected_revision) -> EventInfo`
Cancela uma ocorrência específica de uma série recorrente.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput`.
- **Efeitos:** adiciona o `EXDATE` ao mestre e devolve o mestre atualizado.

#### `attendees(*, actor, event_id) -> tuple[AttendeeInfo, ...]`
Lista até 500 convites do evento; devolve tupla vazia quando o ator vê o evento apenas como "Ocupado".
- **Autorização:** leitura do evento.
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `invite(*, actor, event_id, user_ids, optional=False) -> tuple[AttendeeInfo, ...]`
Convida até 100 pessoas ativas da organização. Convites já existentes são preservados e apenas os novos são retornados.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `InvalidInput` (pessoa de outra organização, inativa, conta de serviço, lista vazia ou acima de 100 pessoas).
- **Efeitos:** grava os novos convites e notifica apenas os recém-convidados.

#### `remove_attendee(*, actor, attendee_id, expected_revision) -> None`
Remove um participante do evento.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound` para convite inexistente ou de evento ilegível (mesma resposta); `Forbidden`; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o convite; a pessoa perde leitura do evento.

#### `respond(*, actor, event_id, response, expected_revision) -> AttendeeInfo`
Registra a resposta (`accepted`, `declined`, `tentative` ou `needs_action`) ao convite do **próprio** ator.
- **Autorização:** leitura do evento e existência de convite para o ator.
- **Erros:** `NotFound` para evento ilegível ou ausência de convite para o ator (igual a inexistente); `Conflict("stale_revision")` para revisão desatualizada do convite; `InvalidInput`.
- **Efeitos:** grava a resposta.

#### `reminders(*, actor, event_id) -> tuple[ReminderInfo, ...]`
Lista os lembretes do evento; devolve tupla vazia quando o ator vê o evento apenas como "Ocupado".
- **Autorização:** leitura do evento.
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela.
- **Efeitos:** nenhum.

#### `add_reminder(*, actor, event_id, minutes_before) -> ReminderInfo`
Adiciona um lembrete com antecedência entre 0 e 40.320 minutos (28 dias). Se já houver lembrete com a mesma antecedência, ele é devolvido sem duplicação.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound`; `Forbidden`; `InvalidInput`.
- **Efeitos:** grava o lembrete.

#### `remove_reminder(*, actor, reminder_id, expected_revision) -> None`
Remove um lembrete do evento.
- **Autorização:** `update` no calendário do evento.
- **Erros:** `NotFound` para lembrete inexistente ou de evento ilegível (mesma resposta); `Forbidden`; `Conflict("stale_revision")`.
- **Efeitos:** exclui logicamente o lembrete.

#### `free_busy(*, actor, user_ids, start, end, include_attending=False) -> tuple[BusyBlock, ...]`
Devolve os blocos de horários ocupados nos calendários pessoais de até 50 pessoas, sem jamais expor títulos ou detalhes. Com `include_attending=True`, inclui também os eventos de qualquer calendário da organização em que a pessoa seja participante com resposta diferente de `declined` (`accepted`, `tentative` ou sem resposta), com recorrência expandida e desconsiderando eventos cancelados ou ocorrências excluídas. Quem é dono do calendário e participante do mesmo evento entra uma única vez. O padrão `False` mantém o comportamento original.
- **Autorização:** `TablePermission` de `read` em `skeleton_calendar.calendar`; considera apenas pessoas com vínculo ativo na organização. Exige ator (não aceita `system_context`).
- **Erros:** `Forbidden`; `InvalidInput` (janela superior a 400 dias, mais de 50 pessoas ou excesso de ocorrências).
- **Efeitos:** nenhum. A quantidade de consultas não cresce com o número de pessoas.

#### `calendar_busy(*, actor, calendar_ids, start, end) -> tuple[CalendarBusyBlock, ...]`
Devolve apenas os horários ocupados de 1 a 50 calendários legíveis pelo ator (como salas, equipamentos ou calendários de atendimento), respeitando a janela máxima de 400 dias.
- **Autorização:** leitura de cada calendário informado. Exige ator.
- **Erros:** `NotFound` (algum calendário ilegível, inexistente ou de outra organização); `InvalidInput` (janela, quantidade de calendários ou excesso de ocorrências).
- **Efeitos:** nenhum. A quantidade de consultas não cresce com o número de calendários.

#### `put_managed_event(*, actor, source_app, source_key, calendar_id, data, attendee_user_ids=(), source_path="") -> EventInfo`
Cria ou atualiza o **evento gerenciado** que projeta um registro de outra app — existe um único evento vivo para cada par `(source_app, source_key)`. `data` aceita somente `title`, `location`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status` e `is_private` (verdadeiro por padrão); `description` é sempre recusada. Eventos gerenciados não possuem recorrência nem lembretes.
- **Autorização:** `update` no calendário (seguindo as regras de `create_event`). Também é aceito em `system_context` da organização (`actor=None`), caso em que o dono do calendário atua como organizador. Como a API é em processo, apenas a app dona do registro deve informar o próprio label (regra verificada em revisão de código).
- **No portal (desde 0.6.0):** permitido apenas a partir de um método `allow_external` da própria `source_app` (verificado pela cadeia de chamadas da API de serviços) e somente em um calendário compartilhado gerenciado por ela (`CalendarInfo.source_app`). A pessoa externa figura como ator na auditoria e o dono do calendário como organizador. Os participantes permanecem inalterados (`attendee_user_ids` deve ser vazio). Um evento da chave localizado em calendário não gerenciado pela app não pode ser movido (`Forbidden`). Chamadas diretas de uma view ou de método sem `allow_external` respondem `Forbidden("external_not_allowed")`.
- **Erros:** `NotFound` (calendário ilegível, de outra organização ou, no portal, não gerenciado pela app); `Forbidden` (somente leitura ou, no portal, `source_app` diferente da app chamadora); `InvalidInput` (participantes no portal, presença de `description`, campo desconhecido, `source_app` desconhecida, inativa na organização ou igual a `skeleton_calendar`, `source_key` fora do padrão de 1 a 200 caracteres entre letras, dígitos e `: . _ -`, `source_path` fora de `apps/<source_app>/` ou contendo `..`, esquema, barra inicial ou invertida, horários inválidos ou participante que não seja membro ativo ou seja conta de serviço).
- **Efeitos:** repetir a chamada com os mesmos dados mantém revisão e `sequence` intactos; dados diferentes atualizam o evento e incrementam ambos. Alterar `calendar_id` ou `source_path` exclui o evento anterior e cria um novo, pois ambos são imutáveis. A lista de participantes é sincronizada exatamente para `attendee_user_ids`, todos como `accepted`, sem gerar **nenhuma** notificação. Em chamadas concorrentes com a mesma chave, apenas um evento permanece vivo: a chamada perdedora relê e atualiza o registro criado.

#### `remove_managed_event(*, actor, source_app, source_key) -> None`
Exclui logicamente um evento gerenciado.
- **Autorização:** `update` no calendário do evento; aceito em `system_context` da organização; no portal (desde 0.6.0), apenas a partir de um método `allow_external` de `source_app` e em calendário gerenciado por ela.
- **Erros:** `Forbidden` (evento somente leitura; no portal, `source_app` diferente da app chamadora ou `external_not_allowed` fora de método `allow_external`); `InvalidInput` (app de origem ou chave inválidas). Se o evento não existir, for ilegível para o ator ou, no portal, estiver fora de um calendário da app, nada é alterado.
- **Efeitos:** exclusão lógica do evento e dos participantes, sem notificação e de forma idempotente.

#### `managed_event(*, actor, source_app, source_key) -> EventInfo | None`
Consulta um evento gerenciado pela chave de origem.
- **Autorização:** leitura do calendário ou convite; aceito em `system_context` da organização; no portal (desde 0.6.0), apenas a partir de um método `allow_external` de `source_app` e somente se estiver em calendário gerenciado por ela (caso contrário, retorna `None`).
- **Erros:** `InvalidInput` (app de origem ou chave inválidas); no portal, `Forbidden` para `source_app` diferente da app chamadora e `external_not_allowed` fora de método `allow_external`.
- **Efeitos:** nenhum.

Os métodos gerais (`update_event`, `cancel_event`, `delete_event`, `edit_occurrence`, `cancel_occurrence`, `invite`, `remove_attendee`, `respond`, `add_reminder` e `remove_reminder`) respondem `Conflict("managed_event")` ao receber um evento gerenciado: ele só pode ser alterado pelo serviço da app de origem.

**Contrato de privacidade dos eventos gerenciados.** O título deve descrever apenas o tipo de compromisso (como "Atendimento" ou "Reunião"), sem identificar terceiros; os detalhes permanecem na app de origem, acessíveis por `source_path`. Como o evento é privado por padrão, quem não é dono do calendário nem organizador vê apenas "Ocupado", e o ICS sai com `CLASS:PRIVATE`. O feed ICS nunca exporta `source_path` nem o label da app.

## DTOs
- `CalendarInfo`: `sysid`, `name`, `kind`, `is_default`, `color`, `timezone`, `owner_id`, `revision`, `writable` (indica se `create_event` aceitaria o calendário para o ator) e `source_app`/`source_path` (vazios em calendários não gerenciados).
- `CalendarLinks`: `calendar_path` e `subscription_path`, relativos à organização.
- `EventInfo`: `sysid`, `calendar_id`, `title`, `description`, `location`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `start_date`, `end_date`, `tzid`, `status`, `organizer_id`, `revision` e `source_app`/`source_path` (vazios em eventos comuns e para quem vê apenas "Ocupado").
- `EventOccurrence`: `event_id`, `calendar_id`, `recurrence_id`, `title`, `busy_only`, `all_day`, `starts_at`, `ends_at`, `status` e, apenas para eventos de dia inteiro, `start_date` (primeiro dia) e `end_date` (dia seguinte ao último, exclusivo; `None` em eventos com hora), além de `source_app` e `source_path` como em `EventInfo`.
- `BusyBlock`: `user_id`, `starts_at`, `ends_at`. Blocos de dia inteiro usam a meia-noite UTC das datas.
- `CalendarBusyBlock`: `calendar_id`, `starts_at`, `ends_at`.
- `AttendeeInfo`: `sysid`, `event_id`, `user_id`, `response`, `optional`, `revision`.
- `ReminderInfo`: `sysid`, `event_id`, `minutes_before`, `channel_hint`, `revision`.

## Histórico de versões
- **0.7.0:** a agenda passa a ser uma biblioteca first-party — os métodos exigem uma app ativa na organização que dependa dela, em vez de ativação própria; contratos e DTOs não mudam.
- **0.6.0:** suporte a eventos gerenciados pelo portal — `put_managed_event`, `remove_managed_event` e `managed_event` passam a aceitar contexto externo exclusivamente a partir de um método `allow_external` da app gerenciadora do calendário; nada muda fora do portal.
- **0.4.0:** calendários gerenciados — acréscimo de `writer_user_ids`, `reader_user_ids`, `source_app` e `source_path` em `create_shared_calendar` (com padrões compatíveis), introdução de `claim_calendar` e `calendar_links` (`CalendarLinks`) e adição de `source_app`/`source_path` em `CalendarInfo` (com valores padrão).
- **0.1.0 (primeira versão do núcleo):** `my_calendars`, `upcoming`, `create_event`, `update_event`, `cancel_event`, `free_busy` (inclusive `include_attending`), `calendar_busy` (`CalendarBusyBlock`), `calendars`, `create_shared_calendar`, `event`, `delete_event`, `edit_occurrence`, `cancel_occurrence`, `attendees`, `invite`, `remove_attendee`, `respond`, `reminders`, `add_reminder`, `remove_reminder` (`AttendeeInfo` e `ReminderInfo`), `EventOccurrence.start_date`/`end_date`, `CalendarInfo.writable`, idempotência sob corrida em `idempotency_key`, e eventos gerenciados (`put_managed_event`, `remove_managed_event`, `managed_event`, `source_app`/`source_path` em `EventInfo` e `EventOccurrence`, e `Conflict("managed_event")` nos métodos gerais).
