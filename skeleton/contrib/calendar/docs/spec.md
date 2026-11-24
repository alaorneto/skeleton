# Agenda

A agenda (`skeleton_calendar`) é a primeira app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)). É uma **biblioteca** (`kind: "bundled_library"`, #792): é instalada com a app que depende dela (#793) ou com `SKELETON_BUNDLED_APPS=calendar`, não tem disponibilidade nem ativação próprias e fica efetiva numa organização enquanto uma app ativa ali a usa. As telas dela valem enquanto está efetiva.

## Contract
`Calendar` (`SecuredRecord`) pertence a uma organização: `name` (até 150), `kind` (`personal` ou `shared`, imutável), `is_default`, `color` (`#RRGGBB`), `timezone` (IANA), `description` (até 2000) e a origem de um calendário gerenciado (`source_app`, `source_path`, #670).

- Cada membro tem **um** calendário pessoal padrão por organização, criado na primeira visita (`ensure_personal_calendar`); duas visitas simultâneas criam um só, pela restrição única parcial.
- O calendário pessoal é sempre `private`: mudar a visibilidade responde 400, e o dono não muda. A delegação a outra pessoa usa concessões de registro (`RecordGrant`), não visibilidade.
- O calendário pessoal padrão não pode ser excluído.
- Calendários criados pelo gateway da tabela (`/api/v1/o/<org>/tables/skeleton_calendar.calendar/records/`) são compartilhados; as escritas passam pelos serviços da app (`RecordOperations`).
- Um calendário compartilhado pode ser **gerenciado** por outra app (#670): `source_app` é o label dela e `source_path`, o caminho da página dele lá (`apps/<source_app>/...`, relativo à organização). Só calendário compartilhado é gerenciado (restrição no banco), e a app de origem não muda depois de gravada. As telas da agenda não renomeiam, não mudam o acesso e não excluem um calendário gerenciado (409), e o gateway da tabela recusa a alteração e a exclusão dele (400): ele muda só pelo serviço da app de origem, que mantém o acesso que ela espelha.
- Sem `TablePermission` de leitura em `skeleton_calendar.calendar`, a agenda não existe para o membro (404). A ação do administrador `member-access` concede ao papel `member` as permissões básicas (`read`, `create`, `update`, `delete`) pelos serviços de acesso.

### Eventos (#69)
`Event` (`OrganizationRecord`, fora do gateway genérico) pertence a um calendário: `uid` (único entre eventos vivos da organização), `title`, `description`, `location`, `all_day`, `starts_at`/`ends_at` (UTC) ou `start_date`/`end_date` (dia inteiro, fim exclusivo), `tzid`, `is_private`, `status` (`confirmed`, `tentative`, `cancelled`), `organizer` e `sequence` (cresce a cada alteração).

- Ler um evento exige ler o calendário; criar, alterar, cancelar ou excluir exige `update` no calendário. No calendário pessoal de outra pessoa, só uma delegação de `update` (concessão de registro) escreve: o bypass do administrador lê, mas não escreve.
- Excluir um evento (com `expected_revision`) notifica o cancelamento aos participantes e exclui logicamente, linha a linha e com auditoria, os lembretes, os participantes e as substituições da série, mantendo os IDs; um lembrete de evento excluído não dispara (#292).
- Um evento privado aparece como "Ocupado" (sem título, descrição nem local) para quem não é o organizador nem o dono do calendário. No calendário pessoal de outra pessoa, o administrador sem delegação vê todos os eventos só como "Ocupado".
- Título, descrição e local são redigidos na auditoria, para que o histórico não revele o que a tela esconde.
- Fim igual ou anterior ao início responde 400, no modelo e por restrição no banco. Alterações usam CAS pela revisão.

### Eventos gerenciados (#537)

Outra app projeta um compromisso dela na agenda como evento gerenciado: `source_app` (label da app), `source_key` (chave opaca, o `sysid` do registro dela) e `source_path` (caminho relativo dentro da organização para o registro de origem, `apps/<source_app>/...`). As três colunas são imutáveis; `source_app` e `source_key` são vazios juntos ou preenchidos juntos, e há um só evento vivo por `(organização, source_app, source_key)` (restrição no banco). Um evento gerenciado não tem recorrência, descrição nem lembrete, é privado por padrão e muda só pelo `put_managed_event`/`remove_managed_event` do serviço; os métodos gerais respondem `Conflict("managed_event")`.

### Recorrência (#70)
O evento mestre guarda a regra (`rrule`), `rdates` e `exdates`; uma linha de substituição aponta para o mestre (`series`) e nomeia a ocorrência que substitui (`recurrence_id`, o início original, como o RECURRENCE-ID do iCalendar, com o mesmo `uid`).

- Subconjunto aceito: `FREQ` DAILY, WEEKLY, MONTHLY ou YEARLY, com `INTERVAL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS` e `WKST`, sempre limitado por `COUNT` (até 1000) ou `UNTIL`. Qualquer outra parte (`FREQ=SECONDLY`, `BYHOUR`) responde 400.
- A regra roda no relógio local do `tzid` (`python-dateutil` + `zoneinfo`): um evento semanal às 09:00 continua às 09:00 locais na troca de horário, e o instante UTC muda.
- `expand()` lê uma janela de até 400 dias e devolve até 1000 ocorrências, com `truncated` quando para antes.
- Custo limitado (#236): `UNTIL` e as datas do evento ficam entre 1900 e 2200 (data inexistente ou fora da faixa responde 400); nenhuma série é expandida além de 100 anos depois do início; uma regra cujos filtros nunca coincidem (`BYMONTH=2;BYMONTHDAY=30`) é recusada na gravação com erro em `rrule`. `rdates` e `exdates` são validados e gravados em ISO 8601 UTC (`+00:00`); um item inválido já gravado é ignorado na leitura e registrado no log sem o valor.
- Edição de uma ocorrência: "só esta" cria ou altera a substituição; "esta e as seguintes" divide a série (a primeira parte termina antes da ocorrência, um novo mestre com `uid` novo começa nela e as substituições posteriores passam para ele mantendo os IDs); "todas" altera o mestre. Cancelar uma ocorrência acrescenta um EXDATE. Editar uma ocorrência grava também o mestre: o fim da série (`recurrence_end`) passa a cobrir uma substituição movida para além da última ocorrência da regra, e a revisão esperada da série é consumida, então uma segunda edição com a mesma revisão recebe `Conflict("stale_revision")`. Na divisão, cada mestre é gravado depois de as substituições estarem no lugar, e os dois fins ficam certos. Uma alteração sem nenhum campo é recusada, sem mudar revisão nem `sequence` e sem notificar (#294).
- `recurrence_end` guarda o fim da série para consultas por janela.
- `is_cancelled` numa substituição marca a ocorrência como cancelada: ela some da expansão e é exportada com `STATUS:CANCELLED`; hoje o serviço cancela por `EXDATE` e não grava esse campo (#345).
- Na divisão "esta e as seguintes", os `exdates` são repartidos pelo instante, mesmo um item gravado em outro deslocamento (#345).

### Participantes (#72)
As operações de participantes, lembretes, exclusão e ocorrências são oferecidas pelo `CalendarService` (#344), com CAS, `NotFound` igual para inexistente e ilegível, e as mesmas regras abaixo; as telas e outras apps usam o serviço.

`EventAttendee` liga um evento a um vínculo da mesma organização (`response`: `needs_action`, `accepted`, `declined`, `tentative`; `optional`).

- Só pessoas ativas da organização podem ser convidadas: um membro de outra organização ou uma conta de serviço responde 400.
- O convite dá leitura do evento (inclusive em calendário pessoal) e das substituições da série; não dá escrita.
- Cada pessoa responde só ao próprio convite: o de outra pessoa, ou nenhum, é 404.
- Remover o participante revoga a leitura na próxima requisição; um vínculo inativo deixa de ler e de ser notificado.
- Convidar, alterar e cancelar notificam os participantes (`calendar.invited`, `calendar.updated`, `calendar.cancelled`) pela API de notificações, na transação da mudança; o payload leva o título (ou "Ocupado", se o evento for privado), o UUID do evento e o início: `starts_at` num evento com hora, `start_date` num de dia inteiro (o outro fica vazio), ambos marcadores do texto, que o corpo de fábrica mostra (#806). Abrir a notificação (ou o `{link}` do texto) leva à agenda no dia do evento, só enquanto a pessoa lê o evento. Cada lote de convites notifica as pessoas daquele lote: a chave de deduplicação de `calendar.invited` inclui um resumo dos UUIDs convidados, e repetir o mesmo lote não notifica de novo (#235).

### Lembretes (#73)
`EventReminder` pede um aviso `minutes_before` (0 a 40320, 28 dias) cada ocorrência, com `channel_hint` indicativo: o canal final segue as preferências de notificação da pessoa.

- O job periódico `calendar.reminders` roda a cada 5 minutos em cada organização (contexto de sistema dela), expande as ocorrências próximas e notifica (`calendar.reminder`) o organizador e os participantes ativos das que chegaram à hora do aviso, com até 30 minutos de tolerância para um agendador atrasado.
- A chave `reminder:<evento>:<ocorrência>:<minutos>` faz cada lembrete disparar uma única vez por ocorrência, mesmo com o job reexecutado.
- Evento cancelado, ocorrência excluída (EXDATE) ou substituição cancelada não geram lembrete; um participante removido não recebe.
- O job devolve quantos lembretes devidos processou; uma reexecução conta também os deduplicados (#345).
- O lembrete de um evento de dia inteiro conta da meia-noite no fuso do evento (`tzid`), não da meia-noite UTC (#312).
- O organizador só recebe enquanto tem vínculo ativo; uma ocorrência sem nenhum destinatário ativo é pulada. Com a agenda desativada na organização, o job não envia nada (e o worker cancela o job, #337). Um lembrete recusado como entrada inválida fica registrado no log (só identificadores) e não desfaz os demais do mesmo ciclo; séries já encerradas não entram na consulta, e substituições e participantes são carregados em lote (#223).

### Telas (#71)
`/o/<org>/apps/skeleton_calendar/` mostra mês, semana, dia ou lista (30 dias) em grade CSS, sem JavaScript, com `?view=` e `?date=` (AAAA-MM-DD) validados: valor desconhecido ou data inválida respondem 400. Os horários aparecem no fuso do perfil. As páginas leem pelo `CalendarService`, então um evento ilegível nunca é renderizado e um privado aparece como "Ocupado"; o download ICS e o feed leem por `policies.readable_events`, com o mesmo mascaramento (#345). O formulário de evento (`events/new/`) aceita dia inteiro ou início e fim, privado, e os presets de repetição diária, semanal, mensal e anual com quantidade de vezes. A navegação tem landmarks (`nav` com rótulo, títulos por dia, `time datetime`). Uma janela com mais de 5000 ocorrências responde 400 ("Há eventos demais neste período"); em "Minha agenda", a organização acima do teto é nomeada à parte e as demais continuam listadas. A leitura de uma janela faz um número fixo de consultas: os eventos sem recorrência que começam depois da janela nem são carregados, e o mascaramento (delegações e convites) é lido uma vez por chamada (#291). Todas as páginas usam `tenant_page` (login, MFA administrativo, erros de serviço mapeados para 404/403/400), e cada erro de validação aparece no campo do formulário a que se refere, como "Fim" para um fim anterior ao início (#234).

### Eventos gerenciados nas telas e no feed (#540)
Na grade e na lista, um evento gerenciado que a pessoa vê com detalhes leva a marca "gerenciado por <nome da app>" e não tem controle de edição; um `POST` forjado para cancelar responde 409 (a mudança é sempre na app de origem). Com a app de origem ativa na organização, aparece o link "abrir na origem" (`/o/<org>/<source_path>`); com ela inativa ou desconhecida, o link some e aparece "origem indisponível". Quem só vê "Ocupado" não vê marca nem link. "Minha agenda" lista os eventos gerenciados em que a pessoa é participante, com a marca, e uma revogação vale na requisição seguinte. O download ICS e o feed exportam o evento gerenciado como qualquer evento, com o mascaramento de sempre, e nunca levam `source_path` nem o label da app. O feed tem no máximo 2000 componentes (`MAX_COMPONENTS`): uma pessoa com mais compromissos (gerenciados ou não) recebe os 2000 primeiros, e o corte fica no log (`calendar.feed_truncated`), sem dado dos eventos.

### Calendários e compartilhamento (#670)
- `calendars/` lista os calendários que a pessoa lê, com a marca "gerenciado por <app>" nos gerenciados. Quem tem `access.manage` e `create` na tabela vê o formulário de criação: nome, responsável (membro ativo), fuso IANA, grupos e pessoas que escrevem e grupos e pessoas que só leem. A criação passa por `create_shared_calendar` (calendário privado, concessões de registro pelo `AccessService`); um `POST` de quem não tem as duas responde 403. Um formulário reenviado cria um calendário só (chave de idempotência no formulário).
- `calendars/<id>/` mostra o calendário legível (404 para os demais, inclusive de outra organização) e leva à página do link de assinatura. Num calendário compartilhado não gerenciado, quem tem `access.manage` altera o compartilhamento: quem escreve recebe `read` e `update`, quem só lê recebe `read`, e as concessões que saíram da lista são revogadas; concessões de `delete` e de papéis ficam como a gestão de acesso deixou. A mudança vale no pedido seguinte. Quem escreve no calendário o renomeia (com revisão), e quem tem `delete` o exclui, só sem eventos (o calendário pessoal padrão nunca).
- Num calendário gerenciado, a página mostra "gerenciado por <app>" e "abrir na origem" (`/o/<org>/<source_path>`, só com a app ativa; senão, "origem indisponível"), sem formulários; um `POST` forjado para renomear, compartilhar ou excluir responde 409.

### Minha agenda (#75)
`/me/apps/skeleton_calendar/` (rota global da pessoa, `runtime.user_urls`) lista os próximos 14 dias de todas as organizações da pessoa onde a agenda está ativa, cada uma lida no próprio contexto somente leitura (`for_each_organization`). Uma TablePermission revogada, um vínculo inativo ou a app desativada tiram os itens daquela organização na requisição seguinte. A página só lê; "Novo evento" pede a organização e leva ao formulário dela. Os 14 dias são os dias locais do fuso do perfil: um evento de dia inteiro aparece só nas datas desses dias (sem o de ontem a leste de UTC nem o do 15º dia a oeste), mostra a sua data, e as horas aparecem no fuso do perfil (#312).

### ICS e feed (#76)
- `calendars/<id>.ics` baixa um calendário legível (sessão) em iCalendar (`icalendar`): um VEVENT por mestre, com RRULE, RDATE e EXDATE, e um por substituição, com RECURRENCE-ID e o mesmo UID. `?busy=1` exporta só blocos ocupados. Eventos com hora saem no fuso do evento (`DTSTART;TZID=...`, com o `VTIMEZONE` correspondente no arquivo), então um cliente expande a série na mesma hora local antes e depois da troca de horário; o `UNTIL` sai em UTC (terminado em `Z`) para evento com hora e como data para dia inteiro, e `EXDATE`/`RECURRENCE-ID` são os instantes gravados (#293). Eventos em `UTC` continuam com o sufixo `Z`.
- O feed `/me/apps/skeleton_calendar/feed/<token>.ics` usa um token de API `kind=feed` com o escopo `calendar.feed`, criado em `/me/apps/skeleton_calendar/feed-token/` e revogável em `/profile/tokens/`. Token desconhecido, expirado, revogado ou acima do limite de taxa responde 404. Cada pedido relê todas as organizações com a autorização atual (`for_each_organization`). Um token de feed não autentica `/api/v1/`.
- O link de um calendário (#669): na página `calendars/<id>/link/`, quem lê o calendário cria, por `POST` com CSRF, um link só daquele calendário, mostrado uma única vez: `/me/apps/skeleton_calendar/calendar-feed/<token>.ics`. O token é de feed, com o escopo `calendar.link`, vinculado à pessoa, ao calendário e à organização dele (`FeedTokenService.issue_for_record`); há um por pedido de criação, e cada um é revogado sozinho em `/profile/tokens/`, onde aparece com o nome do calendário. A cada pedido o feed abre só a organização do calendário, confere que a pessoa ainda o lê e exporta só os eventos dele, com o mesmo mascaramento e a mesma janela do feed pessoal; `?busy=1` exporta só blocos ocupados. Perder a leitura derruba o link no pedido seguinte (404) sem revogá-lo, e voltar a ler o reativa. Token desconhecido, expirado, revogado, acima do limite de taxa, de outro calendário ou do feed pessoal respondem o mesmo 404, e o token do link não abre o feed pessoal nem `/api/v1/`. A resposta tem `Cache-Control: no-store`.
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
- O link de um calendário exporta só aquele calendário; revogar um link não afeta o de outro; perder a leitura derruba o link e voltar a ler o reativa; o token do link não abre o feed pessoal, outro calendário nem `/api/v1/`.
- Quem tem `access.manage` cria e compartilha um calendário pela tela; quem não tem recebe 403 no `POST`; um calendário gerenciado não é renomeado, recompartilhado nem excluído pela tela (409).
