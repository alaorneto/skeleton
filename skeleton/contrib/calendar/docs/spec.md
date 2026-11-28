# Agenda

A agenda (`skeleton_calendar`) é a primeira app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)). Trata-se de uma **biblioteca** (`kind: "bundled_library"`): ela é instalada junto com a app que depende dela ou via `SKELETON_BUNDLED_APPS=calendar`, não possui disponibilidade nem ativação próprias e fica efetiva em uma organização enquanto houver uma app ativa ali que a utilize. Suas telas permanecem disponíveis enquanto ela estiver efetiva.

## Contract
`Calendar` (`SecuredRecord`) pertence a uma organização e contém `name` (até 150 caracteres), `kind` (`personal` ou `shared`, imutável), `is_default`, `color` (`#RRGGBB`), `timezone` (IANA), `description` (até 2.000 caracteres) e, nos calendários gerenciados, a origem (`source_app` e `source_path`).

- Cada membro possui **um** calendário pessoal padrão por organização, criado na primeira visita (`ensure_personal_calendar`); acessos simultâneos criam apenas um, garantido por restrição única parcial.
- O calendário pessoal é sempre `private`: tentar alterar a visibilidade responde 400, e o dono não pode ser alterado. Delegações para outras pessoas usam concessões de registro (`RecordGrant`), nunca visibilidade.
- O calendário pessoal padrão não pode ser excluído.
- Calendários criados pelo gateway da tabela (`/api/v1/o/<org>/tables/skeleton_calendar.calendar/records/`) nascem compartilhados; todas as escritas passam pelos serviços da app (`RecordOperations`).
- Um calendário compartilhado pode ser **gerenciado** por outra app: `source_app` guarda o label dela e `source_path`, o caminho da página correspondente na origem (`apps/<source_app>/...`, relativo à organização). Somente calendários compartilhados podem ser gerenciados (restrição no banco), e a app de origem é imutável após a gravação. As telas da agenda não renomeiam, não alteram compartilhamento e não excluem um calendário gerenciado (409), e o gateway da tabela recusa alteração e exclusão (400): toda mudança ocorre exclusivamente pelo serviço da app de origem, que mantém o espelho de acesso.
- Sem `TablePermission` de leitura em `skeleton_calendar.calendar`, a agenda não existe para o membro (404). A ação administrativa `member-access` concede ao papel `member` as permissões básicas (`read`, `create`, `update`, `delete`) pelos serviços de acesso.

### Eventos (#69)
`Event` (`OrganizationRecord`, fora do gateway genérico) pertence a um calendário e guarda `uid` (único entre eventos vivos da organização), `title`, `description`, `location`, `all_day`, `starts_at`/`ends_at` (em UTC) ou `start_date`/`end_date` (para dia inteiro, com fim exclusivo), `tzid`, `is_private`, `status` (`confirmed`, `tentative`, `cancelled`), `organizer` e `sequence` (incrementada a cada alteração).

- Ler um evento exige leitura do calendário; criar, alterar, cancelar ou excluir exige `update` no calendário. No calendário pessoal de outra pessoa, somente uma delegação de `update` (concessão de registro) autoriza escrita: o bypass de administrador permite leitura, nunca escrita.
- Excluir um evento (com `expected_revision`) notifica o cancelamento aos participantes e exclui logicamente, linha a linha e com auditoria, os lembretes, participantes e substituições da série, preservando seus IDs. Lembretes de eventos excluídos não disparam.
- Um evento privado aparece apenas como "Ocupado" (sem título, descrição nem local) para quem não é o organizador nem o dono do calendário. No calendário pessoal de outra pessoa, o administrador sem delegação também enxerga todos os eventos apenas como "Ocupado".
- Título, descrição e local são redigidos na auditoria para que o histórico não exponha o que a tela oculta.
- Fim igual ou anterior ao início responde 400, tanto no modelo quanto por restrição no banco. Toda alteração utiliza CAS pela revisão.

### Eventos gerenciados (#537)
Outra app pode projetar compromissos próprios na agenda como eventos gerenciados, identificados por `source_app` (label da app), `source_key` (chave opaca, normalmente o `sysid` do registro de origem) e `source_path` (caminho relativo dentro da organização em `apps/<source_app>/...`).

As três colunas são imutáveis; `source_app` e `source_key` devem ser ambos vazios ou ambos preenchidos, e existe no máximo um evento vivo por `(organização, source_app, source_key)` (garantido por restrição no banco). Eventos gerenciados não possuem recorrência, descrição nem lembretes, nascem privados por padrão e só mudam por `put_managed_event` e `remove_managed_event` no serviço; os métodos gerais respondem `Conflict("managed_event")`.

### Recorrência (#70)
O evento mestre armazena a regra (`rrule`), `rdates` e `exdates`; cada substituição aponta para o mestre (`series`) e identifica a ocorrência substituída por `recurrence_id` (o início original, equivalente ao `RECURRENCE-ID` do iCalendar, mantendo o mesmo `uid`).

- **Subconjunto aceito:** `FREQ` em `DAILY`, `WEEKLY`, `MONTHLY` ou `YEARLY`, com `INTERVAL`, `BYDAY`, `BYMONTHDAY`, `BYMONTH`, `BYSETPOS` e `WKST`, sempre limitado por `COUNT` (até 1.000) ou `UNTIL`. Qualquer outra cláusula (`FREQ=SECONDLY`, `BYHOUR` etc.) responde 400.
- **Horário local:** a regra expande no relógio local do `tzid` (`python-dateutil` + `zoneinfo`), mantendo um evento semanal às 09:00 sempre às 09:00 locais na mudança de horário de verão enquanto o instante UTC se ajusta.
- **Expansão:** `expand()` cobre janelas de até 400 dias e devolve até 1.000 ocorrências, sinalizando `truncated` se atingir o limite.
- **Limites de custo:** `UNTIL` e datas de eventos devem ficar entre 1900 e 2200 (datas inexistentes ou fora da faixa respondem 400); nenhuma série expande além de 100 anos após o início; regras cujos filtros nunca coincidem (`BYMONTH=2;BYMONTHDAY=30`) são recusadas na gravação com erro em `rrule`. `rdates` e `exdates` são validados e gravados em ISO 8601 UTC (`+00:00`); se houver algum item inválido já persistido, ele é ignorado na leitura e registrado no log sem o valor.
- **Edição e cancelamento de ocorrências:**
  - "Só esta" cria ou atualiza a substituição correspondente.
  - "Esta e as seguintes" divide a série: a primeira metade termina antes da ocorrência, um novo mestre com novo `uid` começa a partir dela, e as substituições posteriores migram para o novo mestre mantendo seus IDs; `exdates` são repartidos pelo instante real, mesmo quando gravados com outro deslocamento de fuso.
  - "Todas" altera diretamente o mestre.
  - Cancelar uma ocorrência acrescenta um `EXDATE` ao mestre.
  - Editar qualquer ocorrência também grava o mestre: `recurrence_end` passa a cobrir substituições movidas para além da última ocorrência original, e a revisão esperada da série é consumida (uma segunda edição com a mesma revisão recebe `Conflict("stale_revision")`). Na divisão, cada mestre é gravado depois de reposicionar as substituições para que ambos fiquem com `recurrence_end` exato. Alterações sem nenhum campo modificado são recusadas sem alterar revisão ou `sequence` e sem notificar.
- `recurrence_end` registra o término da série para otimizar consultas por janela.
- `is_cancelled` em uma substituição marca a ocorrência como cancelada (some da expansão e exporta com `STATUS:CANCELLED`); atualmente o serviço cancela apenas via `EXDATE` e não grava esse campo.

### Participantes (#72)
Operações sobre participantes, lembretes, exclusão e ocorrências passam pelo `CalendarService`, com CAS, resposta `NotFound` idêntica para inexistente e ilegível e as regras descritas a seguir; telas e outras apps consomem esse mesmo serviço.

`EventAttendee` vincula um evento a um vínculo da mesma organização, com `response` (`needs_action`, `accepted`, `declined`, `tentative`) e `optional`.

- Somente pessoas ativas da organização podem ser convidadas; membros de outra organização e contas de serviço respondem 400.
- O convite concede leitura do evento (inclusive em calendário pessoal) e das substituições da série, mas nunca concede escrita.
- Cada pessoa responde apenas ao próprio convite; o de terceiros ou a ausência de convite retorna 404.
- Remover o participante revoga a leitura na requisição seguinte; um vínculo inativo perde leitura e deixa de ser notificado.
- Convidar, alterar e cancelar notificam os participantes (`calendar.invited`, `calendar.updated`, `calendar.cancelled`) pela API de notificações, na mesma transação da mudança. O payload leva o título (ou "Ocupado", se privado), o UUID do evento e o início (`starts_at` para evento com hora ou `start_date` para dia inteiro, ficando o outro vazio), ambos disponíveis como marcadores no texto padrão. Abrir a notificação ou clicar em `{link}` leva à agenda no dia do evento enquanto a pessoa mantiver leitura. Cada lote de convites notifica apenas as pessoas daquele lote: a chave de deduplicação de `calendar.invited` incorpora um resumo dos UUIDs convidados, impedindo notificações duplicadas em reenvios.

### Lembretes (#73)
`EventReminder` agenda um aviso com `minutes_before` (0 a 40.320 minutos, ou 28 dias) antes de cada ocorrência, com `channel_hint` apenas indicativo — o canal efetivo segue as preferências de notificação da pessoa.

- O job periódico `calendar.reminders` roda a cada 5 minutos por organização (em contexto de sistema dela), expande as ocorrências próximas e envia `calendar.reminder` ao organizador e aos participantes ativos cujos avisos venceram, com tolerância de até 30 minutos para atrasos do agendador.
- A chave de deduplicação `reminder:<evento>:<ocorrência>:<minutos>` garante um único disparo por ocorrência, mesmo em reexecuções do job.
- Eventos cancelados, ocorrências excluídas (`EXDATE`) e substituições canceladas não geram lembretes; participantes removidos não recebem.
- O job retorna a contagem de lembretes devidos processados (em reexecução, inclui os deduplicados).
- Para eventos de dia inteiro, a contagem parte da meia-noite no fuso do evento (`tzid`), não da meia-noite UTC.
- O organizador só recebe enquanto mantiver vínculo ativo, e ocorrências sem destinatário ativo são puladas. Com a agenda desativada na organização, o job não envia nada (e o worker o cancela). Se um lembrete falhar por entrada inválida, apenas os identificadores vão para o log sem desfazer os demais do ciclo. Séries já encerradas ficam fora da consulta, e substituições e participantes são carregados em lote.

### Telas (#71)
A rota `/o/<org>/apps/skeleton_calendar/` exibe as visões de mês, semana, dia ou lista (30 dias) em grade CSS, sem JavaScript. Os parâmetros `?view=` e `?date=` (`AAAA-MM-DD`) são validados, respondendo 400 para valores inválidos. Horários são exibidos no fuso do perfil.

- Todas as páginas leem via `CalendarService`, impedindo a renderização de eventos ilegíveis e exibindo eventos privados como "Ocupado"; o download ICS e o feed leem via `policies.readable_events`, aplicando o mesmo mascaramento.
- O formulário de evento (`events/new/`) aceita dia inteiro ou horário de início e fim, marcação de evento privado e presets de repetição (diária, semanal, mensal e anual) com número de repetições.
- A navegação usa landmarks acessíveis (`nav` rotulado, títulos por dia e `time datetime`).
- Uma janela com mais de 5.000 ocorrências responde 400 ("Há eventos demais neste período"); em "Minha agenda", a organização que excede o teto é listada à parte sem bloquear as demais.
- A leitura de uma janela executa número fixo de consultas: eventos não recorrentes que começam após a janela nem são carregados, e as informações de mascaramento (delegações e convites) são lidas uma única vez por chamada.
- Todas as páginas usam `tenant_page` (login, MFA administrativo e mapeamento de erros de serviço para 404, 403 e 400), exibindo cada erro de validação diretamente no campo correspondente do formulário (como "Fim" quando o término antecede o início).

### Eventos gerenciados nas telas e no feed (#540)
Na grade e na lista, eventos gerenciados exibidos com detalhes mostram o selo "gerenciado por <nome da app>" e omitem controles de edição; um `POST` forjado para cancelamento responde 409, pois toda mudança pertence à app de origem.

- Quando a app de origem está ativa na organização, aparece o link "abrir na origem" (`/o/<org>/<source_path>`); quando inativa ou desconhecida, o link é substituído por "origem indisponível".
- Quem vê apenas "Ocupado" não visualiza nem o selo nem o link.
- "Minha agenda" inclui eventos gerenciados em que a pessoa é participante, exibindo o selo e refletindo revogações na requisição seguinte.
- O download ICS e o feed exportam eventos gerenciados como qualquer outro evento, preservando o mascaramento e sem jamais expor `source_path` ou o label da app.
- O feed limita a saída a 2.000 componentes (`MAX_COMPONENTS`): caso uma pessoa tenha mais compromissos (gerenciados ou não), os 2.000 primeiros são entregues e o corte é registrado em log (`calendar.feed_truncated`), sem dados dos eventos.

### Calendários e compartilhamento (#670)
- `calendars/` lista os calendários legíveis pela pessoa, com o selo "gerenciado por <app>" nos gerenciados. Quem possui `access.manage` e `create` na tabela visualiza o formulário de criação com nome, responsável (membro ativo), fuso IANA, grupos e pessoas com escrita e grupos e pessoas apenas com leitura. A criação usa `create_shared_calendar` (calendário privado e concessões de registro via `AccessService`); `POST` sem ambas as permissões responde 403. O formulário inclui chave de idempotência para evitar duplicação em reenvios.
- `calendars/<id>/` exibe o calendário legível (404 para os demais, inclusive de outra organização) e aponta para a página do link de assinatura. Em calendários compartilhados não gerenciados, quem tem `access.manage` pode ajustar o compartilhamento: escritores recebem `read` e `update`, leitores recebem `read` e concessões removidas da lista são revogadas (preservando concessões de `delete` e de papéis criadas pela gestão de acesso). A alteração vale na requisição seguinte. Quem tem escrita pode renomear (com revisão), e quem tem `delete` pode excluir o calendário desde que vazio (o pessoal padrão nunca pode ser excluído).
- Em calendários gerenciados, a página exibe "gerenciado por <app>" e "abrir na origem" (`/o/<org>/<source_path>` se a app estiver ativa, ou "origem indisponível"), sem formulários; qualquer `POST` forjado para renomear, compartilhar ou excluir responde 409.

### Minha agenda (#75)
A rota global do usuário `/me/apps/skeleton_calendar/` (`runtime.user_urls`) lista os próximos 14 dias de todas as organizações da pessoa onde a agenda está efetiva, lendo cada uma em seu próprio contexto somente leitura (`for_each_organization`).

Uma `TablePermission` revogada, um vínculo inativo ou a desativação da app retiram os itens daquela organização na requisição seguinte. A página é somente leitura; o botão "Novo evento" solicita a escolha da organização e redireciona para o formulário dela. A janela de 14 dias considera os dias locais do fuso do perfil: eventos de dia inteiro aparecem somente nas datas correspondentes (sem vazar o dia anterior a leste de UTC nem o 15º dia a oeste), exibindo sua data própria, enquanto eventos com horário aparecem convertidos para o fuso do perfil.

### ICS e feed (#76)
- **Download de calendário (`calendars/<id>.ics`):** exporta um calendário legível (via sessão) no formato iCalendar (`icalendar`), com um `VEVENT` por mestre (contendo `RRULE`, `RDATE` e `EXDATE`) e um por substituição (com `RECURRENCE-ID` e mesmo `UID`). O parâmetro `?busy=1` exporta apenas blocos ocupados. Eventos com hora saem no fuso próprio (`DTSTART;TZID=...` acompanhado do `VTIMEZONE`), garantindo que clientes expandam a série no mesmo horário local antes e depois de trocas de horário; `UNTIL` sai em UTC (`Z`) para eventos com hora e como data para dia inteiro, `EXDATE` e `RECURRENCE-ID` preservam os instantes gravados, e eventos em `UTC` mantêm o sufixo `Z`.
- **Feed pessoal (`/me/apps/skeleton_calendar/feed/<token>.ics`):** utiliza token de API `kind=feed` com escopo `calendar.feed`, emitido em `/me/apps/skeleton_calendar/feed-token/` e revogável em `/profile/tokens/`. Token desconhecido, expirado, revogado ou acima do limite de taxa responde 404. Cada requisição relê todas as organizações com a autorização vigente (`for_each_organization`). Tokens de feed nunca autenticam `/api/v1/`.
- **Link de calendário individual (`calendars/<id>/link/`):** quem lê o calendário pode emitir, por `POST` com CSRF, um link exibido uma única vez para aquele calendário: `/me/apps/skeleton_calendar/calendar-feed/<token>.ics`. O token é de feed, com escopo `calendar.link`, vinculado à pessoa, ao calendário e à organização (`FeedTokenService.issue_for_record`); cada emissão cria um token independente, revogável em `/profile/tokens/` com o nome do calendário. A cada acesso, o feed abre apenas a organização do calendário, revalida a leitura e exporta somente os eventos dele com o mesmo mascaramento e janela do feed pessoal (`?busy=1` exporta apenas blocos ocupados). Perder a leitura bloqueia o link na requisição seguinte (404) sem revogá-lo, e recuperar a leitura o reativa. Token desconhecido, expirado, revogado, acima do limite de taxa, de outro calendário ou do feed pessoal respondem o mesmo 404; o token do link não abre o feed pessoal nem `/api/v1/`. A resposta sempre inclui `Cache-Control: no-store`.
- **Mascaramento:** tudo o que for bloco ocupado para a pessoa (evento privado ou calendário pessoal alheio sem delegação) sai apenas com horário, `SUMMARY:Ocupado` e `CLASS:PRIVATE`.
- **Janela e limite:** o feed inclui apenas eventos encerrados há no máximo 30 dias e eventos não recorrentes que iniciam em até 400 dias; tanto o feed quanto o download limitam a saída a 2.000 componentes (registrando cortes em log, sem dados dos eventos). A exportação executa número fixo de consultas.

## Acceptance
- Um único calendário pessoal padrão por membro, inclusive sob criação concorrente.
- Alterar a visibilidade de um calendário pessoal responde 400.
- Membro sem `TablePermission` recebe 404.
- A organização B não lê calendários da organização A.
- Quem possui apenas leitura no calendário lê os eventos, mas não consegue criá-los.
- Evento privado aparece apenas como "Ocupado" para demais leitores; administrador vê apenas blocos ocupados no calendário pessoal de outra pessoa.
- Fim igual ou anterior ao início responde 400; eventos de dia inteiro são persistidos como datas.
- Evento semanal às 09:00 mantém as 09:00 locais na mudança de horário (`Europe/Berlin` e histórico de `America/Sao_Paulo`).
- `EXDATE` remove uma ocorrência; substituição altera ou move uma ocorrência específica.
- `FREQ=SECONDLY` responde 400; expansão acima do limite é truncada e sinalizada.
- "Esta e as seguintes" divide a série preservando os IDs das substituições.
- Membro de outra organização ou conta de serviço como participante responde 400; participante não edita o evento; resposta ao convite de outra pessoa retorna 404; remover o participante revoga a leitura.
- Cada lembrete dispara uma única vez por ocorrência, mesmo com reexecução do job; participante removido não recebe; ocorrência cancelada não gera lembrete.
- Parâmetro de data ou visão inválido responde 400; evento não legível nunca é renderizado; todas as telas funcionam sem JavaScript.
- Em "Minha agenda", revogar `TablePermission` em A remove os itens de A na próxima requisição; não aparecem itens de organizações com vínculo inativo.
- Token revogado ou expirado responde 404; o feed respeita a autorização atual; eventos privados saem apenas como ocupados; o arquivo gerado passa em parser RFC 5545 com `RRULE`, `EXDATE` e `RECURRENCE-ID`.
- O link de um calendário exporta apenas aquele calendário; revogar um link não afeta outro; perder a leitura bloqueia o link e recuperá-la o reativa; o token do link não acessa o feed pessoal, outro calendário nem `/api/v1/`.
- Quem tem `access.manage` cria e compartilha calendários pela tela; quem não tem recebe 403 no `POST`; calendários gerenciados não podem ser renomeados, recompartilhados nem excluídos pela tela (409).
