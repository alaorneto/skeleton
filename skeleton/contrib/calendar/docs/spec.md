# Agenda

A agenda (`skeleton_calendar`) é a primeira app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)). O operador a instala com `SKELETON_BUNDLED_APPS=calendar`; a plataforma a disponibiliza e cada organização a ativa, como qualquer app.

## Contract
`Calendar` (`SecuredRecord`) pertence a uma organização: `name` (até 150), `kind` (`personal` ou `shared`, imutável), `is_default`, `color` (`#RRGGBB`), `timezone` (IANA) e `description` (até 2000).

- Cada membro tem **um** calendário pessoal padrão por organização, criado na primeira visita (`ensure_personal_calendar`); duas visitas simultâneas criam um só, pela restrição única parcial.
- O calendário pessoal é sempre `private`: mudar a visibilidade responde 400, e o dono não muda. A delegação a outra pessoa usa concessões de registro (`RecordGrant`), não visibilidade.
- O calendário pessoal padrão não pode ser excluído.
- Calendários criados pelo gateway da tabela (`/api/v1/o/<org>/tables/skeleton_calendar.calendar/records/`) são compartilhados; as escritas passam pelos serviços da app (`RecordOperations`).
- Sem `TablePermission` de leitura em `skeleton_calendar.calendar`, a agenda não existe para o membro (404). A ação do administrador `member-access` concede ao papel `member` as permissões básicas (`read`, `create`, `update`, `delete`) pelos serviços de acesso.

### Eventos (#69)
`Event` (`OrganizationRecord`, fora do gateway genérico) pertence a um calendário: `uid` (único entre eventos vivos da organização), `title`, `description`, `location`, `all_day`, `starts_at`/`ends_at` (UTC) ou `start_date`/`end_date` (dia inteiro, fim exclusivo), `tzid`, `is_private`, `status` (`confirmed`, `tentative`, `cancelled`), `organizer` e `sequence` (cresce a cada alteração).

- Ler um evento exige ler o calendário; criar, alterar, cancelar ou excluir exige `update` no calendário. No calendário pessoal de outra pessoa, só uma delegação de `update` (concessão de registro) escreve: o bypass do administrador lê, mas não escreve.
- Um evento privado aparece como "Ocupado" (sem título, descrição nem local) para quem não é o organizador nem o dono do calendário. No calendário pessoal de outra pessoa, o administrador sem delegação vê todos os eventos só como "Ocupado".
- Título, descrição e local são redigidos na auditoria, para que o histórico não revele o que a tela esconde.
- Fim igual ou anterior ao início responde 400, no modelo e por restrição no banco. Alterações usam CAS pela revisão.

### Recorrência (#70)
O evento mestre guarda a regra (`rrule`), `rdates` e `exdates`; uma linha de substituição aponta para o mestre (`series`) e nomeia a ocorrência que substitui (`recurrence_id`, o início original, como o RECURRENCE-ID do iCalendar, com o mesmo `uid`).

- Subconjunto aceito: `FREQ` DAILY, WEEKLY, MONTHLY ou YEARLY, com `INTERVAL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS` e `WKST`, sempre limitado por `COUNT` (até 1000) ou `UNTIL`. Qualquer outra parte (`FREQ=SECONDLY`, `BYHOUR`) responde 400.
- A regra roda no relógio local do `tzid` (`python-dateutil` + `zoneinfo`): um evento semanal às 09:00 continua às 09:00 locais na troca de horário, e o instante UTC muda.
- `expand()` lê uma janela de até 400 dias e devolve até 1000 ocorrências, com `truncated` quando para antes.
- Custo limitado (#236): `UNTIL` e as datas do evento ficam entre 1900 e 2200 (data inexistente ou fora da faixa responde 400); nenhuma série é expandida além de 100 anos depois do início; uma regra cujos filtros nunca coincidem (`BYMONTH=2;BYMONTHDAY=30`) é recusada na gravação com erro em `rrule`. `rdates` e `exdates` são validados e gravados em ISO 8601 UTC (`+00:00`); um item inválido já gravado é ignorado na leitura e registrado no log sem o valor.
- Edição de uma ocorrência: "só esta" cria ou altera a substituição; "esta e as seguintes" divide a série (a primeira parte termina antes da ocorrência, um novo mestre com `uid` novo começa nela e as substituições posteriores passam para ele mantendo os IDs); "todas" altera o mestre. Cancelar uma ocorrência acrescenta um EXDATE.
- `recurrence_end` guarda o fim da série para consultas por janela.

### Participantes (#72)
`EventAttendee` liga um evento a um vínculo da mesma organização (`response`: `needs_action`, `accepted`, `declined`, `tentative`; `optional`).

- Só pessoas ativas da organização podem ser convidadas: um membro de outra organização ou uma conta de serviço responde 400.
- O convite dá leitura do evento (inclusive em calendário pessoal) e das substituições da série; não dá escrita.
- Cada pessoa responde só ao próprio convite: o de outra pessoa, ou nenhum, é 404.
- Remover o participante revoga a leitura na próxima requisição; um vínculo inativo deixa de ler e de ser notificado.
- Convidar, alterar e cancelar notificam os participantes (`calendar.invited`, `calendar.updated`, `calendar.cancelled`) pela API de notificações, na transação da mudança; o payload leva o título (ou "Ocupado", se o evento for privado) e o UUID do evento. Cada lote de convites notifica as pessoas daquele lote: a chave de deduplicação de `calendar.invited` inclui um resumo dos UUIDs convidados, e repetir o mesmo lote não notifica de novo (#235).

### Lembretes (#73)
`EventReminder` pede um aviso `minutes_before` (0 a 40320, 28 dias) cada ocorrência, com `channel_hint` indicativo: o canal final segue as preferências de notificação da pessoa.

- O job periódico `calendar.reminders` roda a cada 5 minutos em cada organização (contexto de sistema dela), expande as ocorrências próximas e notifica (`calendar.reminder`) o organizador e os participantes ativos das que chegaram à hora do aviso, com até 30 minutos de tolerância para um agendador atrasado.
- A chave `reminder:<evento>:<ocorrência>:<minutos>` faz cada lembrete disparar uma única vez por ocorrência, mesmo com o job reexecutado.
- Evento cancelado, ocorrência excluída (EXDATE) ou substituição cancelada não geram lembrete; um participante removido não recebe.
- O organizador só recebe enquanto tem vínculo ativo; uma ocorrência sem nenhum destinatário ativo é pulada. Com a agenda desativada na organização, o job não envia nada (e o worker cancela o job, #337). Um lembrete recusado como entrada inválida fica registrado no log (só identificadores) e não desfaz os demais do mesmo ciclo; séries já encerradas não entram na consulta, e substituições e participantes são carregados em lote (#223).

### Telas (#71)
`/o/<org>/apps/skeleton_calendar/` mostra mês, semana, dia ou lista (30 dias) em grade CSS, sem JavaScript, com `?view=` e `?date=` (AAAA-MM-DD) validados: valor desconhecido ou data inválida respondem 400. Os horários aparecem no fuso do perfil. Tudo passa pelo `CalendarService`, então um evento ilegível nunca é renderizado e um privado aparece como "Ocupado". O formulário de evento (`events/new/`) aceita dia inteiro ou início e fim, privado, e os presets de repetição diária, semanal, mensal e anual com quantidade de vezes. A navegação tem landmarks (`nav` com rótulo, títulos por dia, `time datetime`). Uma janela com mais de 5000 ocorrências responde 400 ("Há eventos demais neste período"); em "Minha agenda", a organização acima do teto é nomeada à parte e as demais continuam listadas. A leitura de uma janela faz um número fixo de consultas: os eventos sem recorrência que começam depois da janela nem são carregados, e o mascaramento (delegações e convites) é lido uma vez por chamada (#291). Todas as páginas usam `tenant_page` (login, MFA administrativo, erros de serviço mapeados para 404/403/400), e cada erro de validação aparece no campo do formulário a que se refere, como "Fim" para um fim anterior ao início (#234).

### Minha agenda (#75)
`/me/apps/skeleton_calendar/` (rota global da pessoa, `runtime.user_urls`) lista os próximos 14 dias de todas as organizações da pessoa onde a agenda está ativa, cada uma lida no próprio contexto somente leitura (`for_each_organization`). Uma TablePermission revogada, um vínculo inativo ou a app desativada tiram os itens daquela organização na requisição seguinte. A página só lê; "Novo evento" pede a organização e leva ao formulário dela.

### ICS e feed (#76)
- `calendars/<id>.ics` baixa um calendário legível (sessão) em iCalendar (`icalendar`): um VEVENT por mestre, com RRULE, RDATE e EXDATE, e um por substituição, com RECURRENCE-ID e o mesmo UID. `?busy=1` exporta só blocos ocupados.
- O feed `/me/apps/skeleton_calendar/feed/<token>.ics` usa um token de API `kind=feed` com o escopo `calendar.feed`, criado em `/me/apps/skeleton_calendar/feed-token/` e revogável em `/profile/tokens/`. Token desconhecido, expirado, revogado ou acima do limite de taxa responde 404. Cada pedido relê todas as organizações com a autorização atual (`for_each_organization`). Um token de feed não autentica `/api/v1/`.
- O que é bloco ocupado para a pessoa (privado, calendário pessoal de outra pessoa sem delegação) sai só com horário, `SUMMARY:Ocupado` e `CLASS:PRIVATE`.
- O feed leva só eventos que terminaram há no máximo 30 dias e, sem recorrência, que começam em até 400 dias; feed e download levam no máximo 2000 componentes (o corte fica no log, sem dados dos eventos). A exportação faz um número fixo de consultas (#291).

## Acceptance
- Um calendário pessoal padrão por membro, inclusive com criação concorrente.
- Mudar a visibilidade de calendário pessoal responde 400.
- Membro sem TablePermission recebe 404.
- A organização B não lê calendários de A.
- Quem lê o calendário sem `update` lê os eventos e não os cria.
- Evento privado aparece só como "Ocupado" para os demais leitores; o administrador vê só blocos ocupados no calendário pessoal de outra pessoa.
- Fim igual ou anterior ao início responde 400; dia inteiro é guardado como datas.
- Um evento semanal às 09:00 continua às 09:00 locais na troca de horário (Europe/Berlin e o histórico de America/Sao_Paulo).
- EXDATE remove uma ocorrência; uma substituição move uma.
- `FREQ=SECONDLY` responde 400; uma expansão acima do limite é truncada e sinalizada.
- "Esta e as seguintes" divide a série sem perder IDs.
- Membro de outra organização ou conta de serviço como participante responde 400; o participante não edita; RSVP de outra pessoa é 404; remover o participante revoga a leitura.
- Cada lembrete dispara uma vez por ocorrência, mesmo com o job reexecutado; participante removido não recebe; ocorrência cancelada não gera lembrete.
- Parâmetro de data ou visão inválido responde 400; evento não legível nunca é renderizado; as telas funcionam sem JavaScript.
- Em Minha agenda, TablePermission revogada em A some com os itens de A na próxima requisição; não há itens de organizações com vínculo inativo.
- Token revogado ou expirado responde 404; o feed respeita a autorização atual; eventos privados aparecem só como ocupado; a saída passa por um parser RFC 5545 com RRULE, EXDATE e RECURRENCE-ID.
