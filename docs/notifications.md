# Notificações

O núcleo oferece notificações in-app como infraestrutura (`skeleton/notifications`, label `sk_notifications`). Qualquer app registra tipos de notificação e chama `notify()` dentro da transação do negócio: um rollback não deixa notificação nem job.

## Tabelas

| Modelo | O que guarda | Proteção |
|---|---|---|
| `Notification` | Tipo, ator, alvo (`target_table_key`, `target_sysid`), payload, snapshot, especificação dos destinatários, `group_key`, `dedupe_key` | [Tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS tenant; campos imutáveis |
| `NotificationRecipient` | A cópia de cada pessoa: `read_at`, `archived_at`, `count` agrupada e, copiados no fan-out, tipo, alvo, snapshot e payload, porque a caixa entre organizações nunca lê `Notification` (visível só no próprio tenant); e o texto já renderizado no idioma dela (`title`, `body`) com as pessoas que ele cita (`subject_people`, [#783](https://github.com/alaorneto/skeleton/issues/783)) | RLS tenant com [autodescoberta](access-control.md#rls-postgresql) por `user_id`: a pessoa lê as próprias linhas de todas as organizações, e só escreve no tenant atual |

Nenhuma das duas entra no gateway, e a exclusão física fica para o serviço de retenção.

## Contextos

Todo tipo diz de onde vem ([#782](https://github.com/alaorneto/skeleton/issues/782)), e isso decide quem pode configurá-lo:

| Contexto | O que é | Exemplos | Quem configura |
|---|---|---|---|
| `platform` | A plataforma falando: com os próprios administradores, com as organizações (comunicados) e com os membros sobre o que é dela (cobrança, limites do plano) | `billing.*`, `organization.member_limit_reached` | Só a plataforma: a organização não desliga nem reescreve |
| `organization` | O que acontece dentro da organização, sem depender de app | `comments.mention`, `flows.*`, `webhooks.endpoint_disabled`, `transfer.ready` | A plataforma e a organização |
| `app` | Avisos de uma app instalada; só existem onde a app está ativa | `calendar.*` | A plataforma e a organização, salvo `org_editable=False` |

Um tipo do núcleo (`owner_app` com prefixo `sk_`) declara `context`; o de uma app que não declara é `app`, e uma app não pode declarar `platform` nem `organization`. Cada pessoa continua escolhendo, nas [preferências](#preferências), como recebe cada aviso.

## Registrar um tipo

```python
from django.utils.translation import gettext_lazy

from skeleton.notifications.registry import NotificationField, NotificationType, register_notification_type

register_notification_type(
    NotificationType(
        "tasks.assigned",
        gettext_lazy("Tarefa atribuída"),
        payload_fields=("message",),
        snapshot_fields=("code", "title"),
        target_table_key="tasks.task",
        group_key="task:{target}",
        owner_app="tasks",
        description=gettext_lazy("Quando uma tarefa é atribuída a você"),
        fields=(
            NotificationField("code", gettext_lazy("Código da tarefa")),
            NotificationField("title", gettext_lazy("Título da tarefa")),
        ),
        title=gettext_lazy("Nova tarefa: {title}"),
        body=gettext_lazy("Código {code}."),
    )
)
```

| Campo | Para quê |
|---|---|
| `label`, `description` | O nome do aviso e uma frase do que ele significa, nas telas de configuração e de preferências. Use `gettext_lazy`: o texto sai no idioma de quem lê |
| `context` | `platform`, `organization` ou `app` (acima) |
| `audience` | `members` (padrão), `external` (pessoas do [portal](#fan-out)) ou `platform` (os administradores da plataforma) |
| `platform_capability` | Com `audience="platform"`: quem tem essa capacidade da plataforma (por exemplo `updates.operate`) também recebe |
| `fields` | Os marcadores do texto: nome (um campo do payload ou do snapshot), rótulo e formato (`text`, `number`, `date`, `datetime`, `money` com `currency_field`, `person`) |
| `title`, `body` | O texto de fábrica, com marcadores; sem título vale o comportamento antigo (rótulo e valores do snapshot) |
| `org_editable` | `False` impede a organização de reescrever o texto de um aviso de app |
| `fixed_text` | O texto é o conteúdo de cada aviso (os comunicados da plataforma): ninguém o edita |

O check `skeleton.E023` reprova tipo duplicado, tabela-alvo desconhecida e campo de snapshot fora de `read_fields`, e também reprova campo de snapshot ou de payload que esteja em `sensitive_fields`. Ele também reprova: tipo do núcleo sem `context`; tipo de app com contexto `platform` ou `organization`; `audience="platform"` fora do contexto `platform`, com alvo, snapshot ou canal que não seja `in_app`/`email`; `platform_capability` desconhecida ou sem a audiência `platform`; campo de texto fora do payload e do snapshot, de formato desconhecido, `money` sem `currency_field` declarado ou `person` fora do payload; `fixed_text` fora da plataforma; e texto de fábrica inválido (abaixo).

## Texto do aviso

O texto é texto simples com marcadores `{campo}` (`skeleton/notifications/texts.py`). Não é template do Django nem `str.format`: só existem os marcadores, escritos em letras minúsculas, números e `_`; `{{` e `}}` escrevem chaves literais, e qualquer outra chave é erro. Não há acesso a atributo (`{x.__class__}`), índice, formato nem HTML ou Markdown.

- Marcadores permitidos: os `fields` do tipo e os marcadores fixos do núcleo, que todo tipo tem ([#806](https://github.com/alaorneto/skeleton/issues/806)):

  | Marcador | Valor |
  |---|---|
  | `{destinatario}` | o nome de exibição de quem recebe (o do perfil, senão nome e sobrenome) |
  | `{destinatario_primeiro_nome}` | a primeira palavra desse nome |
  | `{organizacao}` | o nome público da organização; vazio nos avisos à plataforma |
  | `{plataforma}` | o nome do produto da instância ([aparência](appearance.md)) |
  | `{aviso}` | o rótulo do tipo |
  | `{data_atual}` | a data em que a notificação foi criada, no fuso de quem recebe |
  | `{link}` | o endereço que abre o item do aviso (abaixo) |

  Um campo de tipo não pode ter o nome de um marcador fixo (`skeleton.E023`).
- **Datas do item.** Quando o item do aviso tem datas que importam para quem recebe (o prazo de uma tarefa, o vencimento de uma fatura, o início de um evento), o tipo as declara como campos `date` ou `datetime` e o texto de fábrica as mostra, para que a plataforma e a organização possam destacá-las no título ou no corpo. Uma data que só existe no registro, sem marcador, não chega ao texto.
- **Link do item.** `{link}` é o endereço de uma rota do núcleo por notificação: `/o/<org>/notifications/n/<notificação>/` (`notifications:item`) nos avisos da organização e `/platform/notifications/n/<notificação>/` (`platform:notification-item`) nos da plataforma; o mesmo endereço para todos os destinatários, no domínio da organização quando ela tem um. A cada clique a rota confere que a pessoa recebeu aquela notificação (a linha dela, a linha não lida em que um aviso mais novo do mesmo grupo a agrupou, ou uma entrega por email ou push), marca a linha como lida e leva ao item **daquela** notificação, reautorizado como em "abrir": o registro-alvo ou o `open_url` do tipo. Quem não recebeu, ou não lê mais o item, vê a mesma página de conteúdo indisponível. Nos avisos a pessoas externas, `{link}` é o portal da organização. Por isso todo tipo cujo item tem uma página declara `target_table_key` ou `open_url`: a menção leva ao próprio comentário (`#comment-<id>`), os eventos da agenda ao dia do evento, a exportação ou importação concluída à lista de transferências e o webhook desativado à página dele. Os comunicados da plataforma (`fixed_text`) não têm item nem texto editável.
- O texto é renderizado uma vez por idioma e fuso e compartilhado por quem tem o mesmo par; um texto que usa `{destinatario}` ou `{destinatario_primeiro_nome}` é renderizado para cada pessoa, e o nome só vai para a linha dela (caixa, email, push), nunca para o payload, o snapshot ou o log.
- Título: de 1 a 150 caracteres, uma linha. Corpo: até 2000 caracteres e 30 linhas. Sem caracteres de controle.
- Cada valor é formatado no idioma e no fuso de quem recebe: datas como no resto da interface, números com separador de milhar, dinheiro como nas telas da cobrança (`1.234,56 BRL`), pessoas pelo nome de exibição.
- No HTML (caixa, email) todo pedaço sai escapado, e a quebra de linha vira `<br>`. Os links de abrir, de preferências e de descadastro nunca vêm do texto: o layout do núcleo os acrescenta. O único endereço que o texto traz é o de `{link}`, gerado pelo núcleo: no HTML do email só esse endereço exato vira link (`plain_html(texto, link=...)`); qualquer outro endereço escrito no texto continua texto.

Um tipo **sem tabela-alvo** pode declarar `open_url=função(user, row)`: ao abrir a notificação, a função reautoriza a pessoa e devolve o endereço, ou `None` (página genérica). O alvo pode ser uma [tabela derivada](access-control.md#tabelas-derivadas) (#562): quem recebe e quem abre é quem lê a âncora. Como tabela derivada não tem página genérica, a notificação abre só no `open_url` do tipo, chamado depois que o núcleo reautorizou a leitura do alvo; sem ele, abre na página genérica sem link, nunca num endereço que responde 404. Os tipos dos [fluxos de processo](flows.md#integrações) (`flows.task_assigned`, `flows.task_reminder` e `flows.task_overdue`) usam isso para levar à tarefa só quem ainda pode vê-la; o payload deles tem o rótulo do fluxo e da etapa, o número do processo, o identificador da tarefa e o prazo (`{due_at}`), nunca dados de formulário.

Um tipo sem tabela-alvo também pode declarar `authorize=função(user, notification) -> bool`: o fan-out a chama para cada pessoa e só cria a linha de quem ela aprova, de modo que um acesso revogado entre a notificação e o fan-out impede a entrega. O tipo `comments.mention` ([menções](comments.md#menções), [#188](https://github.com/alaorneto/skeleton/issues/188)) usa os dois ganchos: `authorize` confere que o comentário segue vivo e que a pessoa ainda lê o registro, e `open_url` leva à página do registro só nessa condição. O payload tem só os identificadores do comentário, da tabela e do registro; o texto do comentário nunca vai na notificação.

## Configuração da plataforma

O administrador da plataforma configura cada aviso da instância em **Plataforma › Notificações › Avisos** (`/platform/notifications/types/`, [#784](https://github.com/alaorneto/skeleton/issues/784)). O catálogo mostra todos os tipos registrados, agrupados por [contexto](#contextos) (e, nos de app, por app), com quem recebe, os canais padrão e o estado. A página de um aviso tem duas partes:

- **Envio.** Desligar o aviso (página de confirmação) faz com que nenhuma organização o entregue enquanto estiver desligado: o fan-out não cria linha nem entrega, e o email e o push já agendados são pulados. Um aviso obrigatório não pode ser desligado. Os **canais padrão** escolhidos aqui substituem os do código para quem não escolheu outra coisa nas preferências; o canal que a audiência não permite (`push` para pessoas externas e para a plataforma) é recusado.
- **Texto.** Um título e um corpo por idioma de `LANGUAGES`, com a lista dos marcadores do aviso e uma prévia com valores de exemplo. "Restaurar texto de fábrica" (com confirmação) volta ao texto do código. O texto de um aviso `fixed_text` não é editável.

As escolhas ficam em duas tabelas globais auditadas, com revisão: `InstanceNotificationSetting` (ligado e canais padrão, uma linha viva por tipo) e `InstanceNotificationText` (título e corpo, uma linha viva por tipo e idioma). O serviço é `skeleton.notifications.instance_settings`: toda escrita exige o administrador da plataforma completo (papéis limitados não escrevem) e a revisão da linha (409 com revisão antiga).

## Configuração da organização

Quem tem a capacidade `notifications.manage` (o papel Administrador a tem) configura os avisos da organização em **Configuração › Organização › Notificações** (`/o/<org>/admin/notifications/`, [#785](https://github.com/alaorneto/skeleton/issues/785)). A lista mostra os avisos que a organização pode receber, agrupados em **Da plataforma**, **Da organização** e um grupo por aplicativo ativo, sem os códigos técnicos. Os avisos de uma app só aparecem, e só são entregues, enquanto a app está ativa na organização.

| Contexto do aviso | A organização pode |
|---|---|
| `platform` | só ver o estado e o texto: nunca desliga nem reescreve |
| `organization` e `app` | desligar para todos os membros (com confirmação), se o aviso não for obrigatório e a plataforma não o tiver desligado; escrever o próprio texto por idioma e restaurar o padrão, salvo `org_editable=False` |

Desligado na organização, o aviso não cria linha nem entrega para ninguém dela; as outras organizações não mudam. O texto da organização vem antes do da plataforma e do de fábrica, só para quem recebe nela. Os canais padrão continuam sendo da plataforma: as preferências da pessoa valem em todas as organizações dela.

A plataforma pode travar essas escolhas por organização com as chaves `notifications.<tipo>.enabled` (o valor travado é gravado na organização) e `notifications.<tipo>.text` (o texto atual fica como está), também na política das organizações novas ([configurações geridas](managed-settings.md)). Com a trava, a tela mostra "Definido pela plataforma." e uma escrita forjada recebe `setting_locked` (409).

As escolhas ficam em `OrganizationNotificationSetting` e `OrganizationNotificationText` (por organização, com RLS, auditadas, com revisão). A mesma configuração tem REST:

| Rota | O que faz |
|---|---|
| `GET /api/v1/o/<org>/notification-settings/` | Os avisos da organização, com `context`, `enabled`, `reason` (`mandatory`, `instance_disabled`, `organization_disabled`, `platform_type`), `can_disable`, `can_edit_text`, `locked` e `revision` |
| `GET`/`PUT /api/v1/o/<org>/notification-settings/<tipo>/` | `PUT {"enabled": bool}`: sem linha, cria sem `If-Match`; com linha, exige `If-Match` (428 sem ela, 412 desatualizada) |
| `PUT`/`DELETE /api/v1/o/<org>/notification-settings/<tipo>/texts/<idioma>/` | `PUT {"title", "body"}` grava o texto; `DELETE` com `If-Match` volta ao padrão |

Uma app lê o estado dos próprios tipos na organização por `NotificationService.settings` ([API de serviços](service-api.md#notificationservice)).

## `notify()`

Apps usam `NotificationService` de `skeleton.service_api.notifications` ([API de serviços](service-api.md#notificationservice)), que exige que a app chamadora seja a dona do tipo e que o ator leia o alvo. Um tipo registrado sem `owner_app` não pertence a ninguém: o serviço o recusa (`Forbidden`) para qualquer chamador (#272). A função interna do núcleo é `skeleton.notifications.services.notify(type_code, *, recipients, target=None, payload=None, …)`; apps não a importam.

```python
from skeleton.service_api.notifications import NotificationService, Recipients

NotificationService().notify(
    actor=request.user,
    type_code="tasks.assigned",
    recipients=Recipients(users=(assignee.pk,)),
    payload=(("message", "Nova tarefa"),),
    target_table_key="tasks.task",
    target_id=task.pk,
)
```

A tabela abaixo descreve a função interna; o serviço devolve as recusas como `ContextRequired`, `InvalidInput` ou `NotFound`.

| Regra | Resultado |
|---|---|
| Fora de contexto tenant | `MissingTenantContext` |
| Tipo desconhecido, chave de payload não declarada, valor não escalar | `ValidationError` |
| Alvo ausente, de outra tabela ou de outra organização | `ValidationError` |
| Grupo, papel ou pessoa que não pertence à organização atual | `ValidationError` |
| `dedupe_key` repetida para o mesmo tipo | devolve a notificação existente |

O snapshot copia só os `snapshot_fields` do alvo, que são lidos e não sensíveis. Um snapshot pode sobreviver à perda de acesso ao registro até a retenção; esse risco residual é aceito. `notify()` enfileira o job `notifications.fanout`, que transforma a notificação em linhas por pessoa.

## Fan-out

O job `notifications.fanout` (sistema, por organização) cria uma linha `NotificationRecipient` por pessoa autorizada **no momento do envio**:

- Destinatários: pessoas citadas, membros dos grupos, quem tem os papéis (diretamente ou por grupo) ou toda a organização. Só vínculos ativos de identidades ativas contam, e o ator nunca recebe a própria notificação. Contas de serviço (#44) ficam de fora: não têm caixa de notificações.
- Autorização no alvo: cada pessoa precisa passar em `require_record_action(alvo, action="read")`. Quem não lê o alvo não recebe linha, mesmo citado diretamente; um alvo excluído não notifica ninguém.
- Snapshot dos destinatários: quem saiu do grupo antes do fan-out não recebe, e mudanças posteriores no grupo não reescrevem a história.
- Lotes de até 500 pessoas: cada lote é um job (`notification:<id>:<offset>`), e o último marca `fanned_out_at`. A leitura do alvo é decidida para o lote inteiro por `users_who_can_read`, com a mesma regra de `require_record_action` e um número de consultas que não cresce com o lote; as entregas já existentes do lote são lidas numa consulta por canal (#296).
- Agrupamento: com `group_key`, uma linha não lida e não arquivada da mesma chave recebe `count + 1` e passa a apontar para a notificação mais recente, em vez de criar outra linha; o título e o corpo passam a ser os da mais recente.
- Texto ([#783](https://github.com/alaorneto/skeleton/issues/783)): o fan-out escolhe o [texto do aviso](#texto-do-aviso), formata os campos e grava título e corpo na linha de cada pessoa, no idioma e no fuso dela (perfil, depois organização, depois instância). A renderização é feita uma vez por par idioma e fuso do lote, não por pessoa, e o número de consultas continua sem crescer com o lote. Isso acontece no fan-out porque ele roda no contexto da organização: a caixa global roda sem tenant e não poderia ler textos da organização, e um texto editado depois não reescreve o que já foi entregue. Um campo `person` mostra o nome de exibição de quem é ou foi membro da mesma organização; de qualquer outra pessoa, "Pessoa indisponível". O payload guarda só o UUID.

**Pessoas externas** (#535). Um tipo com `audience="external"` notifica pessoas externas do [portal](portal.md): os destinatários são indicados um a um (`Recipients(users=...)`; grupos, papéis e a organização inteira são recusados) e cada um precisa de `ExternalAccess` ativo na organização. O tipo não tem alvo nem snapshot, só os campos declarados do payload, e usa só o canal `email` (`notification_problems` acusa outro canal, um alvo ou uma audiência desconhecida). O fan-out, por job como sempre, não cria linha de caixa de entrada: só agenda o email, que leva o link do portal da organização e nenhum link de preferências, e é pulado (`recipient_inactive`) se o acesso foi revogado antes do envio. A deduplicação pela chave vale igual. Um tipo sem a declaração continua recusando quem não é membro.

Os avisos da [cobrança](billing.md#inadimplência) (`billing.*`) são tipos sem tabela-alvo, obrigatórios, por email e na aplicação: `authorize` reconfere no fan-out que a pessoa ainda administra a organização, e `dedupe_key` faz cada etapa sair uma vez. A exceção é `billing.usage_threshold` (#525), o aviso de 80% e 100% do limite de um medidor de uso: não é obrigatório (a pessoa pode desligar o canal) e leva só o código do medidor e os números ([medidores de uso](billing.md#medidores-de-uso)).

O limite de membros atingido (`organization.member_limit_reached`, [#698](https://github.com/alaorneto/skeleton/issues/698)) é um tipo sem tabela-alvo e não obrigatório, por email e na aplicação, para quem tem `members.manage` (`authorize` reconfere no fan-out); leva só `limit` e `used`, e `dedupe_key` faz sair um por dia e valor de limite ([limites da organização](organization-limits.md#aviso-ao-chegar-no-limite)).

## Avisos padrão da organização

Toda organização tem avisos sobre as próprias pessoas, sem depender de app ([#786](https://github.com/alaorneto/skeleton/issues/786)). Eles são do contexto `organization`, ligados por padrão só na aplicação, e a organização pode desligá-los ou reescrever o texto ([configuração da organização](#configuração-da-organização)):

| Tipo | Quando | Quem recebe | Texto de fábrica |
|---|---|---|---|
| `organization.member_joined` | um vínculo passa a ativo (`membership.created`: convite aceito, admissão, criação, reativação) | quem tem `members.manage`, menos a própria pessoa | "{member} entrou em {organizacao}" |
| `organization.member_left` | um vínculo ativo é encerrado (`membership.deactivated`: remoção, saída, desativação) | quem tem `members.manage` | "{member} não faz mais parte de {organizacao}" |

Os avisos são criados no mesmo ponto em que o núcleo publica os [eventos de domínio](events.md) do vínculo (`membership_changed`), na transação da mudança, como o aviso de limite de membros: um rollback não deixa aviso. O payload leva só o UUID da pessoa; o nome aparece no texto de quem pode gerir membros ([campo `person`](#texto-do-aviso)) e sai com a eliminação dela. Não geram aviso: contas de serviço, o primeiro vínculo de uma organização (quem a criou não tem a quem avisar) e um tipo desligado pela plataforma ou pela organização, que nem cria a notificação. A própria pessoa nunca recebe o aviso sobre si.

## Avisos da plataforma

A plataforma tem uma caixa própria para os seus administradores ([#787](https://github.com/alaorneto/skeleton/issues/787)), fora de qualquer organização. Um tipo com `audience="platform"` (só do núcleo, contexto `platform`) é registrado por `skeleton.notifications.platform.notify_platform(type_code, payload=..., dedupe_key=...)`, que recusa ser chamado dentro de um tenant, grava `PlatformNotification` e enfileira o job global `notifications.platform_fanout` na mesma transação. Não existe na API de serviços: uma app não envia avisos à plataforma.

- **Quem recebe:** os administradores da plataforma ativos e, quando o tipo declara `platform_capability`, quem tem um papel limitado com essa capacidade (por exemplo `updates.operate`). Nunca contas de serviço nem clientes da plataforma.
- **Canais:** a caixa (`in_app`) e o email, pelas preferências da pessoa e pelos canais padrão da plataforma; resumo diário ou semanal contam como imediato, e não há push. O email usa a marca da instância e leva ao aviso em `/platform/notifications/<id>/open/`; ele é reconferido no envio (papel, preferência, tipo ligado) e segue o mesmo calendário de retentativas do email da organização.
- **Caixa:** `PlatformNotificationRecipient`, uma linha por pessoa com o texto renderizado no idioma dela, lida, arquivada, agrupada por `group_key` e o estado do email. Tabelas globais operacionais: só o código as lê, sempre filtrado pela pessoa da requisição.
- **Telas:** **Plataforma › Notificações › Caixa** (`/platform/notifications/`), para qualquer papel da plataforma, com Não lidas, Todas e Arquivadas, marcar como lida ou não lida, arquivar e marcar todas. Abrir um aviso o marca como lido e leva ao destino do tipo (`open_url`, reautorizado); sem destino, mostra o aviso. A linha de outra pessoa responde 404. O contador de não lidas aparece no menu da plataforma e no link "Plataforma" do cabeçalho, limitado a `99+`.
- **Retenção:** o job global `notifications.platform_retention` (diário, 03:45 UTC) usa os mesmos prazos das caixas das organizações; a eliminação da pessoa apaga as linhas dela.

## Alertas da plataforma

A plataforma avisa sozinha quem precisa agir ([#788](https://github.com/alaorneto/skeleton/issues/788)). O job periódico global `notifications.platform_watch` roda a cada 15 minutos, lê só contagens e estados e chama `notify_platform`; a `dedupe_key` de cada aviso carrega a janela, então uma situação é avisada uma vez por janela. Cada verificação roda numa transação própria: uma que falha fica no log (`platform_watch.failed`) e as outras seguem.

| Tipo | Quando | Quem recebe | Uma vez por |
|---|---|---|---|
| `platform.jobs_dead` | há jobs `dead`; de novo ao passar de 1, 10, 100 e 1000 | administradores | dia e faixa |
| `platform.queue_lagging` | o job vencido mais antigo espera mais que `SKELETON_PLATFORM_ALERT_QUEUE_LAG_S` (900 s); nunca com a fila pausada por manutenção | administradores | hora |
| `platform.update_available` | o aviso de atualização da plataforma tem versão nova do núcleo ou de apps | `updates.operate` | par de versões |
| `platform.operation_failed` | uma operação de atualização terminou `failed`, `interrupted` ou `restored` nas últimas 24 h | `updates.operate` | operação |
| `platform.erasure_requested` | uma pessoa pede a eliminação dos dados (na própria requisição, sem esperar o vigia) | administradores | pedido |
| `platform.organization_suspended` | o resumo de gestão da organização (`OrganizationSnapshot`) está `suspended` pela cobrança | `billing.operate` | organização e dia |

Os payloads só têm números, códigos e identificadores: nunca nome, email, segredo nem mensagem de erro; o aviso leva à tela da plataforma onde a pessoa vê o resto. Cada tipo pode ser desligado ou ter os canais trocados em [Avisos](#configuração-da-plataforma), e `SKELETON_PLATFORM_ALERTS_ENABLED=False` desliga o vigia e o aviso de eliminação. Sem worker vivo nenhum job roda: a falta de worker continua sendo vista pela sonda de saúde e por `check_jobs`.

## Comunicados da plataforma

O administrador da plataforma avisa as organizações em **Plataforma › Notificações › Comunicados** (`/platform/notifications/announcements/`, [#789](https://github.com/alaorneto/skeleton/issues/789)):

1. **Rascunho:** título (até 150 caracteres), texto simples (até 2000, sem formatação nem links clicáveis; chaves são só caracteres), quem recebe (os administradores de cada organização ou todos os membros) e as organizações (todas as ativas ou uma lista de identificadores, um por linha; um desconhecido ou inativo é recusado pelo nome). O rascunho muda com revisão e pode ser excluído (exclusão lógica).
2. **Revisar e enviar:** a página de confirmação diz quantas organizações recebem e quem; depois de enviado, o comunicado não muda nem é cancelado, e a página mostra quando, por quem e para quantas.

O envio marca o comunicado `sent` e enfileira um job global (`notifications.announce_batch`) que percorre as organizações escolhidas de 500 em 500, cada uma com um job próprio (`notifications.announce`) no contexto de sistema dela, numa transação própria. Ali sai o aviso `platform.announcement`: do contexto `platform`, obrigatório na caixa (cada pessoa ainda escolhe o email) e com `fixed_text`, porque o texto é o próprio comunicado: nem a plataforma nem a organização o reescrevem, e a organização não o desliga. Rodar os jobs de novo não repete o aviso (`dedupe_key` por comunicado). Tudo fica em `PlatformAnnouncement`, tabela global auditada, escrita só pelo administrador da plataforma completo.

## Caixa de notificações

| Rota | O que faz |
|---|---|
| `/notifications/` | Caixa global: as linhas da pessoa em todas as organizações com vínculo ativo, por [autodescoberta](access-control.md#rls-postgresql) |
| `/o/<org>/notifications/` | Caixa da organização atual |
| `POST /o/<org>/notifications/<id>/read/`, `unread/`, `archive/` | Marca lida, não lida ou arquivada; idempotente, com CSRF |
| `POST /o/<org>/notifications/read-all/` | Marca todas como lidas na organização |
| `/o/<org>/notifications/<id>/open/` | Marca lida e reautoriza o alvo (`require_record_action(..., "read")`): com acesso, redireciona ao registro (alvo de tabela derivada: ao `open_url` do tipo, se houver); sem acesso ou alvo removido, mostra uma página genérica, sem título nem snapshot |
| `GET /api/v1/notifications/` | API de sessão: página da caixa global (`page`, `page_size`, `archived=0/1`) mais `unread`; cada linha traz `title` e `text`, o texto renderizado no envio |
| `POST /api/v1/o/<org>/notifications/actions/` | `{"action": "read" \| "unread" \| "archive" \| "read_all", "ids": [...]}` (1 a 100 ids) |

- A linha de outra pessoa responde 404, igual a uma linha inexistente.
- Toda escrita acontece no tenant da linha; a caixa global só lê.
- O cabeçalho mostra o contador de não lidas de todas as organizações com vínculo ativo, limitado a `99+`. O número de consultas é constante (índice parcial `(user_id) WHERE read_at IS NULL AND archived_at IS NULL`).
- Tudo funciona sem JavaScript: as ações são formulários comuns.
- A raiz `notifications/` é reservada ao núcleo: uma app não pode capturá-la.

## Preferências

Cada pessoa escolhe, por tipo e canal, como recebe as notificações em todas as organizações (`UserNotificationPreference`, `SystemRecord` global, auditado e com revisão). A página fica em `/profile/notifications/` (formulário comum, sem JavaScript) e a API de sessão em `/api/v1/me/notification-preferences/`.

A página e a API mostram só os avisos que a pessoa pode receber ([#790](https://github.com/alaorneto/skeleton/issues/790)): os de membros que a plataforma não desligou, do núcleo ou de uma app ativa em ao menos uma organização dela (pelo resumo de gestão da instância; uma organização ainda sem resumo conta todas as apps instaladas); os da plataforma só para os administradores dela e para quem tem a capacidade do tipo; nunca os de pessoas externas. Os avisos da plataforma não têm push ("Não disponível"). Na API, cada item traz `context` e `description`, e um tipo que a pessoa não recebe responde 404, como um inexistente.

Na página ([#778](https://github.com/alaorneto/skeleton/issues/778)), o padrão para todos os avisos fica num cartão próprio no topo; os tipos vêm em cartões pelo [contexto](#contextos) que declaram (Da plataforma, Da organização e um por aplicativo, pelo nome; `group_rows` em `skeleton/notifications/preference_views.py`, [#790](https://github.com/alaorneto/skeleton/issues/790)), cada um com a descrição, numa tabela de colunas fixas que vira um bloco por aviso abaixo de 640 px. O código técnico do tipo não aparece. A hora do resumo só aparece com "Resumo diário" ou "Resumo semanal" escolhido (CSS `:has()`; sem ele, aparece sempre). O canal travado de um tipo obrigatório mostra o marcador "Obrigatório" e o texto "Sempre ligado", sem campo: o POST não o envia. Os nomes de campo (`mode:<tipo>:<canal>`, `hour:…`, `revision:…`) e a revisão de cada linha não mudaram.

| Canal | Modos |
|---|---|
| `in_app` | `off`, `immediate` |
| `email` | `off`, `immediate`, `daily`, `weekly` (com `digest_hour`, de 0 a 23, no fuso da pessoa; o semanal sai às segundas-feiras) |
| `push` | `off`, `immediate` |

A resolução vai do mais específico ao mais geral: a linha da pessoa para o tipo, a linha `*` (padrão dela para todos os tipos), os canais padrão que a plataforma escolheu para o tipo ([configuração da plataforma](#configuração-da-plataforma)) e, sem nada disso, o `default_channels` do tipo (canal listado é `immediate`, os demais `off`). Um tipo `mandatory=True` sempre chega à caixa in-app: desligá-lo responde 400, e a linha `*` não o desliga.

| Rota | O que faz |
|---|---|
| `GET /api/v1/me/notification-preferences/` | Todos os tipos registrados e o `*`, com `mode`, `digest_hour`, `source` (`own`, `default`, `instance`, `type`, `mandatory`), `sysid` e `revision` da linha, quando existe |
| `GET`/`PUT /api/v1/me/notification-preferences/<tipo>/<canal>/` | `PUT {"mode": ..., "digest_hour": ...}`. Sem linha, cria (sem `If-Match`); com linha, exige `If-Match` com a revisão (428 sem ela, 412 desatualizada) |
| `DELETE /api/v1/me/notification-preferences/<tipo>/<canal>/` | Exclusão lógica com `If-Match`: volta a valer o nível seguinte da resolução |

A pessoa só lê e altera as próprias linhas: nenhuma rota recebe identificador de usuário. Um formulário com revisão antiga responde 409 sem gravar nada.

## Email imediato

Quem tem `email` em `immediate` (pela preferência ou pelo `default_channels` do tipo) recebe um email por notificação. O fan-out cria uma `NotificationDelivery` (canal `email`, [tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS tenant) e enfileira o job `notifications.email` na mesma transação: um rollback não deixa nem a entrega nem o job, e nada sai durante o `notify()`.

O job, em `system_context` da organização:

1. Pula a entrega (`skipped`) se a pessoa foi desativada, saiu da organização (`recipient_inactive`), desligou o email (`opted_out`) ou perdeu a leitura do alvo (`target_unreadable`) desde o fan-out.
2. Renderiza no idioma e no fuso do perfil (`translation.override`, `timezone.override`) os templates `notifications/<tipo>/subject.txt`, `body.txt` e `body.html` (opcional), procurando antes `<nome>.<idioma>.<ext>` (`body.en.txt`) e caindo em `notifications/default/`. O contexto traz `title` e `body` (o texto da linha da pessoa, ou renderizado no job para quem desligou a caixa e não tem linha), `body_html` (o corpo escapado, com `<br>`), `label`, `snapshot`, `payload`, `created_at`, `open_url` e `preferences_url`. Os templates padrão usam o título como assunto; um template por tipo continua valendo como layout e recebe o mesmo contexto.
3. Envia com `List-Unsubscribe: <…/profile/notifications/>`. Uma falha conta uma tentativa e agenda outra em 1 min, 5 min, 30 min e 2 h; depois da quinta, a entrega fica `dead`. Uma entrega `sent` nunca sai de novo: a entrega é pelo menos uma vez, idempotente por linha.

Os links usam `SKELETON_PUBLIC_URL`, que precisa ser uma origem (`https://app.example.com`, sem caminho): sem ela os links saem relativos e o cabeçalho `List-Unsubscribe` é omitido, e o `check --deploy` avisa com `skeleton.W035` (#279). Quando a organização da notificação tem o endereço próprio ligado ([#487](https://github.com/alaorneto/skeleton/issues/487)), os links (abrir, caixa de entrada, preferências e `List-Unsubscribe`, no email imediato, no resumo e no push) saem no endereço dela em forma limpa (`https://acme.skeleton.com/notifications/<id>/open/`, `https://acme.skeleton.com/profile/notifications/`) por `skeleton.tenancy.site.organization_url`; sem endereço ligado, nada muda. A URL é montada no envio, a partir do índice global e de `SKELETON_PUBLIC_URL`, nunca do `Host` de uma requisição antiga; o índice é lido uma vez por organização e unidade de trabalho, então um resumo com muitos itens faz uma consulta. A inscrição de push é por origem: quem aceitou o push no domínio principal e no endereço da organização recebe duas vezes (limite conhecido). Emails com token ou link secreto (allauth, convites) continuam em `schedule_mail` e nunca passam por esta fila. Os textos dos templates padrão existem em português e inglês. O inglês só é usado quando o projeto acrescenta `en` a `LANGUAGES`, porque o núcleo entrega apenas pt-BR. O idioma e o fuso de cada destinatário seguem a mesma ordem das telas: perfil, depois organização, depois instância (#170).

### Layout e marca

Os emails HTML do núcleo estendem `skeleton/email/base.html` ([#173](https://github.com/alaorneto/skeleton/issues/173)). Isso inclui a notificação, o resumo e o convite. O layout tem:

- uma faixa e o nome na cor da organização;
- o logo, quando houver, como parte inline `Content-ID: <organization-logo>` referenciada por `cid:organization-logo`, nunca por URL pública;
- um rodapé.

Os dados vêm das configurações da organização do contexto em que o email é montado: o job da organização ou a requisição do convite. O logo de uma organização nunca vai no email de outra.

### Marca nos e-mails

A precedência é organização → instância → núcleo ([#600](https://github.com/alaorneto/skeleton/issues/600), [aparência](appearance.md#e-mails)):

- **Com organização no contexto:** nome, cor e logo dela. Sem cor própria, vale a cor do tema ativo da instância; o logo da instância nunca entra num email de organização.
- **Sem organização:** o nome do produto, a cor do tema ativo e o logo da instância (inline, `cid:organization-logo`, o mesmo `Content-ID`: os templates não mudam).
- **Sem marca da instância:** como antes, sem nome e com a cor `#1d4ed8`.

Os emails de conta do allauth (confirmação de endereço, redefinição de senha) são só texto e não usam `skeleton/email/base.html`: no domínio principal eles saudam com o nome do produto da instância, quando definido; no endereço de uma organização, com o nome publicado dela.

O texto simples continua sem marca. O convite usa os templates `skeleton/tenancy/invitation/{subject,body}.txt` e `body.html`. Ele sai no idioma e no fuso padrão da organização, depois da instância, porque a pessoa convidada pode ainda não ter conta.

## Resumo diário ou semanal

Quem escolhe `daily` ou `weekly` no canal `email` recebe um resumo em vez de um email por notificação. O job periódico `notifications.digest` roda a cada hora, por organização e numa transação própria:

- Seleciona os membros ativos cuja `digest_hour` é a hora atual **no fuso do perfil**; o semanal só às segundas-feiras.
- Reúne as linhas da caixa (`NotificationRecipient`) não lidas, não arquivadas e ainda fora de resumo, dos tipos em resumo, até 50 por email, e marca `digest_sent_at`: uma linha nunca entra em dois resumos.
- Cria uma `NotificationDelivery` (canal `digest`) por pessoa e hora local, com `dedupe_key` `digest:<pessoa>:<AAAAMMDDHH>`, e o mesmo job `notifications.email` envia com as mesmas retentativas. Rodar a mesma janela de novo não cria outro resumo.
- Resumo vazio não é criado. Itens lidos ou arquivados entre a criação e o envio saem do email; se nenhum sobrar, a entrega fica `skipped` (`empty_digest`).

Templates: `notifications/digest/subject.txt` e `body.txt|html`, com as variantes por idioma; cada item mostra o título e o corpo da linha. O resumo junta a caixa in-app: um tipo com `in_app` desligado não tem linha e não entra no resumo. Uma pessoa em N organizações recebe N resumos; um resumo único entre organizações fica para depois. O job recebe a janela para a qual foi agendado (`window`): um job atrasado ainda envia o resumo daquela hora (#276). Continuam puladas só a janela que nunca foi agendada (agendador parado) e a hora local que não existe na mudança de horário; os itens seguem para o próximo resumo. No envio, cada item é reautorizado como no email imediato: um alvo que a pessoa deixou de ler fica fora, e um resumo sem nenhum item legível termina `skipped` (`empty_digest`).

## Web push

Com o push configurado, a página `/profile/notifications/` mostra a seção "Push neste navegador". Ela fica oculta sem JavaScript ou sem suporte do navegador, e o resto da página funciona igual. Ao ativar, o `site.js` registra o service worker `/sw.js` (escopo `/`) e envia a assinatura do navegador (sessão e CSRF):

| Rota | O que faz |
|---|---|
| `POST /api/v1/me/push-subscriptions/` | `{"endpoint", "keys": {"p256dh", "auth"}}` registra o navegador para a pessoa logada (201). Endpoint fora de `WEBPUSH_ALLOWED_HOSTS`, sem HTTPS, com porta ou credencial na URL: 400. Se o mesmo endpoint era de outra pessoa (outro login no mesmo navegador), a assinatura antiga é excluída |
| `POST /api/v1/me/push-subscriptions/unsubscribe/` | `{"endpoint"}` exclui logicamente a assinatura da própria pessoa (204) |
| `GET /sw.js` | O service worker, só com o push configurado (404 sem ele) |

`WebPushSubscription` é um `SystemRecord` global: o endpoint (uma URL de capacidade) e as duas chaves ficam cifrados com o keyring `INTEGRATION_ENCRYPTION_KEYS` e são `sensitive_fields`, redigidos na auditoria; `endpoint_digest` (SHA-256) localiza a assinatura sem decifrar.

Quem tem `push` em `immediate` recebe, a cada notificação, uma `NotificationDelivery` (canal `push`) e o job `notifications.push`:

- O job repete as checagens do email (pessoa ativa, preferência, leitura do alvo) e envia a cada assinatura viva da pessoa. As assinaturas não ficam bloqueadas durante os envios (inscrever e cancelar não esperam o push); o resultado de cada uma é gravado depois do último envio, relendo a linha com lock, e a espera por lock do job é limitada a 5 s (#295).
- Payload mínimo: `{"title": <rótulo do tipo>, "url": <link de abertura>}`. O texto do aviso (título e corpo renderizados, ou o snapshot numa linha antiga) só entra, em `body`, quando o tipo declara `push_snapshot=True`: a tela bloqueada não mostra dado do alvo sem essa declaração.
- O corpo é cifrado com `aes128gcm` (RFC 8291) e assinado com VAPID ES256 (RFC 8292). Os dois são implementados com `cryptography`, que já está no lock, e não com `pywebpush`, para não trazer `aiohttp`, `requests` e outras dependências. A saída é só pelo `skeleton.common.outbound_http` (anti-SSRF), e de novo só para hosts da allowlist.
- Cada pessoa tem no máximo 10 assinaturas vivas: uma nova exclui logicamente a mais antiga, e o envio contata só as 10 mais recentes. `last_success_at` é regravado no máximo uma vez por dia, para cada push não virar uma escrita auditada (#314).
- 2xx conta como enviado. 404 ou 410 exclui logicamente a assinatura, sem retentativa. 429 e 5xx seguem o mesmo calendário de retentativas do email até `dead`.

## Retenção

As tabelas de notificação são operacionais, e a exclusão física só acontece no job periódico `notifications.retention` (diário, às 03:30 UTC, por organização e no `system_context` dela). Prazos (settings e chaves da configuração da instância):

| Setting | Padrão | O que remove |
|---|---|---|
| `SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS` | 30 | Entregas finalizadas (`sent`, `skipped`, `dead`) criadas antes do prazo, e as abertas (`pending`, `retrying`) também: o calendário de retentativas dura horas, então uma entrega aberta há mais tempo que o prazo foi abandonada por um job que morreu (#277). Dentro do prazo, as abertas ficam |
| `SKELETON_NOTIFICATION_READ_RETENTION_DAYS` | 90 | Linhas da caixa lidas ou arquivadas há mais tempo que o prazo |
| `SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS` | 180 | Linhas não lidas cuja última atualização (incluindo o agrupamento) é mais antiga que o prazo; e, depois delas, as `Notification` mais antigas que o prazo, distribuídas ou não (um job de fan-out que morreu deixa `fanned_out_at` vazio, #277), que nenhuma linha nem entrega referencia |

A ordem respeita as referências `PROTECT`: primeiro as entregas, depois as linhas e por último as notificações órfãs. O contador de não lidas continua igual ao número de linhas não lidas que restam. Uma `dedupe_key` volta a aceitar uma notificação nova só depois que a antiga sai pela retenção. A eliminação a pedido do titular ([#191](https://github.com/alaorneto/skeleton/issues/191)) usa `forget_person`, declarado ao lado da retenção: em cada organização, apaga as entregas e as linhas da pessoa, e as notificações órfãs saem depois pela retenção ([privacidade](privacy.md#eliminação-da-identidade)). As linhas de outras pessoas que a citam pelo nome (`subject_people`, como "Ana entrou na organização") ficam só com o rótulo do tipo: depois da eliminação, nenhuma linha de notificação nomeia a pessoa.
