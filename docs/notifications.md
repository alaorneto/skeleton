# Notificações

O núcleo fornece notificações in-app, por email e por web push como infraestrutura (`skeleton/notifications`, label `sk_notifications`). Qualquer app registra seus tipos de notificação em código e chama `notify()` na mesma transação da escrita de negócio: se a transação sofrer rollback, nenhuma notificação nem job é gravado.

## Tabelas

| Modelo | O que guarda | Proteção |
|---|---|---|
| `Notification` | Tipo, ator, alvo (`target_table_key`, `target_sysid`), payload, snapshot, especificação dos destinatários, `group_key` e `dedupe_key` | [Tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS de tenant e campos imutáveis |
| `NotificationRecipient` | Cópia de cada destinatário: `read_at`, `archived_at`, `count` de agrupamento e cópias de tipo, alvo, snapshot e payload (feitas no fan-out, pois a caixa global entre organizações nunca lê `Notification`, visível apenas no próprio tenant); inclui o texto já renderizado no idioma da pessoa (`title`, `body`) e as pessoas citadas nele (`subject_people`, [#783](https://github.com/alaorneto/skeleton/issues/783)) | RLS de tenant com [autodescoberta](access-control.md#rls-postgresql) por `user_id`: a pessoa lê suas próprias linhas de todas as organizações e só escreve no tenant atual |

Nenhuma das duas tabelas entra no gateway genérico, e a exclusão física cabe apenas ao serviço de retenção.

## Contextos

Todo tipo de notificação declara sua origem ([#782](https://github.com/alaorneto/skeleton/issues/782)), e esse contexto define quem pode configurá-lo:

| Contexto | O que representa | Exemplos | Quem configura |
|---|---|---|---|
| `platform` | Comunicações da própria plataforma com seus administradores, com as organizações (comunicados) ou com membros sobre assuntos da plataforma (cobrança, limites do plano) | `billing.*`, `organization.member_limit_reached` | Apenas a plataforma: a organização não desliga nem altera o texto |
| `organization` | Eventos internos da organização que não dependem de uma app | `comments.mention`, `flows.*`, `webhooks.endpoint_disabled`, `transfer.ready` | Plataforma e organização |
| `app` | Avisos de uma app instalada; só existem nas organizações em que a app está ativa | `calendar.*` | Plataforma e organização, salvo quando `org_editable=False` |

Um tipo do núcleo (`owner_app` com prefixo `sk_`) precisa declarar `context`. Um tipo de app que omite `context` assume `app` por padrão e não pode declarar `platform` nem `organization`. Independentemente do contexto, cada pessoa continua definindo nas [preferências](#preferências) como deseja receber cada aviso.

## Registrar um tipo

Os tipos de notificação são registrados em código no `ready()` da app, declarando os campos de payload e snapshot, os marcadores de texto e o texto padrão:

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

| Campo | Para que serve |
|---|---|
| `label`, `description` | Nome do aviso e breve descrição exibidos nas telas de configuração e preferências. Use `gettext_lazy` para que o texto acompanhe o idioma de quem lê |
| `context` | `platform`, `organization` ou `app` (veja [Contextos](#contextos)) |
| `audience` | `members` (padrão), `external` (pessoas do [portal](#fan-out)) ou `platform` (administradores da plataforma) |
| `platform_capability` | Usado com `audience="platform"`: quem possui essa capacidade da plataforma (por exemplo, `updates.operate`) também recebe o aviso |
| `fields` | Marcadores disponíveis para o texto: nome (um campo do payload ou do snapshot), rótulo e formato (`text`, `number`, `date`, `datetime`, `money` com `currency_field`, ou `person`) |
| `title`, `body` | Texto padrão com marcadores; sem `title`, vale o formato legado (rótulo e valores do snapshot) |
| `org_editable` | Quando `False`, impede a organização de reescrever o texto de um aviso de app |
| `fixed_text` | Indica que o texto é o próprio conteúdo de cada envio (como nos comunicados da plataforma) e não pode ser editado |

O check `skeleton.E023` valida o registro dos tipos e reprova:

- tipo duplicado, tabela-alvo desconhecida, campo de snapshot ausente de `read_fields` e campo de snapshot ou payload listado em `sensitive_fields`;
- tipo do núcleo sem `context` e tipo de app com contexto `platform` ou `organization`;
- `audience="platform"` fora do contexto `platform`, com alvo, com snapshot ou com canal diferente de `in_app` e `email`;
- `platform_capability` desconhecida ou usada sem `audience="platform"`;
- campo de texto ausente do payload e do snapshot, formato desconhecido, `money` sem `currency_field` declarado ou `person` fora do payload;
- `fixed_text` fora do contexto `platform` e texto padrão inválido (conforme as regras abaixo).

## Texto do aviso

O texto das notificações usa texto simples com marcadores `{campo}` (`skeleton/notifications/texts.py`). Não é um template do Django nem `str.format`: apenas marcadores escritos em letras minúsculas, números e `_` são aceitos; `{{` e `}}` produzem chaves literais, e qualquer outra chave resulta em erro. Não existe acesso a atributos (`{x.__class__}`), indexação, especificadores de formato, HTML nem Markdown.

Os marcadores permitidos são os `fields` do tipo mais os marcadores fixos do núcleo, disponíveis em qualquer tipo ([#806](https://github.com/alaorneto/skeleton/issues/806)):

| Marcador | Valor |
|---|---|
| `{destinatario}` | Nome de exibição de quem recebe (do perfil ou nome e sobrenome) |
| `{destinatario_primeiro_nome}` | Primeira palavra desse nome |
| `{organizacao}` | Nome público da organização (vazio nos avisos destinados à plataforma) |
| `{plataforma}` | Nome do produto da instância ([aparência](appearance.md)) |
| `{aviso}` | Rótulo do tipo de notificação |
| `{data_atual}` | Data de criação da notificação, no fuso horário de quem recebe |
| `{link}` | Endereço que abre o item da notificação (detalhado abaixo) |

Um campo do tipo não pode reutilizar o nome de um marcador fixo (`skeleton.E023`).

- **Datas do item.** Quando o item possui datas relevantes para quem recebe (prazo de uma tarefa, vencimento de uma fatura, início de um evento), o tipo deve declará-las como campos `date` ou `datetime` e exibi-las no texto padrão, permitindo que a plataforma e a organização as destaquem no título ou no corpo. Datas presentes apenas no registro, sem marcador declarado, não chegam ao texto.
- **Link do item.** `{link}` aponta para uma rota do núcleo por notificação: `/o/<org>/notifications/n/<notificação>/` (`notifications:item`) nos avisos de organização e `/platform/notifications/n/<notificação>/` (`platform:notification-item`) nos avisos da plataforma. O endereço é o mesmo para todos os destinatários e usa o domínio próprio da organização quando ativo. A cada clique, a rota confirma que a pessoa recebeu aquela notificação (pela linha dela, por uma linha não lida que agrupou um aviso mais novo do mesmo grupo, ou por uma entrega de email ou push), marca a linha como lida e redireciona para o item **daquela** notificação, reautorizando o acesso como em "abrir" (registro-alvo ou `open_url` do tipo). Quem não recebeu o aviso ou perdeu acesso ao item vê a mesma página genérica de conteúdo indisponível. Nos avisos para pessoas externas, `{link}` leva ao portal da organização. Por isso, todo tipo cujo item tem uma página declara `target_table_key` ou `open_url`: menções levam ao comentário (`#comment-<id>`), eventos da agenda ao dia do evento, exportações ou importações à lista de transferências e webhooks desativados à página do endpoint. Comunicados da plataforma (`fixed_text`) não têm item nem texto editável.
- **Renderização e personalização.** O texto é renderizado uma vez por combinação de idioma e fuso horário e compartilhado entre destinatários com o mesmo par. Se o texto usar `{destinatario}` ou `{destinatario_primeiro_nome}`, é renderizado individualmente, e o nome vai apenas para a linha da pessoa (caixa, email e push), nunca para o payload, o snapshot ou os logs.
- **Limites e formatação.** O título tem de 1 a 150 caracteres em uma única linha; o corpo tem até 2 000 caracteres e 30 linhas, sem caracteres de controle. Cada valor é formatado no idioma e no fuso de quem recebe: datas no formato da interface, números com separador de milhar, valores monetários como nas telas de cobrança (`1.234,56 BRL`) e pessoas pelo nome de exibição.
- **Escape no HTML.** Na caixa e nos emails HTML, todo texto sai escapado e quebras de linha viram `<br>`. Links de abrir, preferências e descadastro nunca vêm do texto editável — são inseridos pelo layout do núcleo. O único endereço clicável dentro do texto é o `{link}` gerado pelo núcleo: no HTML do email, apenas esse endereço exato vira link (`plain_html(texto, link=...)`); qualquer outra URL escrita no texto permanece texto simples.

Um tipo **sem tabela-alvo** pode declarar `open_url=função(user, row)`: ao abrir a notificação, a função reautoriza a pessoa e devolve o endereço de destino ou `None` (exibindo a página genérica). O alvo também pode ser uma [tabela derivada](access-control.md#tabelas-derivadas): quem recebe e quem abre o aviso é quem lê a âncora. Como tabelas derivadas não têm página genérica, a notificação abre apenas pelo `open_url` do tipo, chamado após o núcleo reautorizar a leitura do alvo; sem `open_url`, exibe a página genérica sem link, nunca um endereço que retorne 404. Os tipos dos [fluxos de processo](flows.md#integrações) (`flows.task_assigned`, `flows.task_reminder` e `flows.task_overdue`) usam esse mecanismo para levar à tarefa apenas quem ainda pode vê-la, carregando no payload o rótulo do fluxo e da etapa, o número do processo, o identificador da tarefa e o prazo (`{due_at}`), nunca dados de formulário.

Um tipo sem tabela-alvo também pode declarar `authorize=função(user, notification) -> bool`: o fan-out executa essa verificação para cada destinatário e só cria a linha para quem for aprovado, evitando entregas quando o acesso foi revogado entre o disparo e o fan-out. O tipo `comments.mention` ([menções](comments.md#menções), [#188](https://github.com/alaorneto/skeleton/issues/188)) usa ambos os ganchos: `authorize` confirma que o comentário continua vivo e que a pessoa ainda lê o registro, e `open_url` leva à página do registro sob a mesma condição. O payload guarda apenas os identificadores do comentário, da tabela e do registro; o texto do comentário nunca entra na notificação.

## Configuração da plataforma

O administrador da plataforma gerencia todos os avisos da instância em **Plataforma › Notificações › Avisos** (`/platform/notifications/types/`, [#784](https://github.com/alaorneto/skeleton/issues/784)). O catálogo agrupa os tipos por [contexto](#contextos) (e, nos de app, por aplicativo), exibindo audiência, canais padrão e estado. A página de cada tipo tem duas seções:

- **Envio.** Desligar um aviso (com página de confirmação) impede qualquer organização de entregá-lo: o fan-out não cria linhas nem entregas, e emails ou pushes já agendados são pulados. Avisos obrigatórios não podem ser desligados. Os **canais padrão** escolhidos aqui substituem os definidos em código para quem não personalizou suas preferências; canais incompatíveis com a audiência (`push` para pessoas externas e para a plataforma) são recusados.
- **Texto.** Permite definir título e corpo para cada idioma de `LANGUAGES`, mostrando a lista de marcadores disponíveis e uma prévia com valores de exemplo. A ação "Restaurar texto de fábrica" (com confirmação) volta ao texto do código. Avisos com `fixed_text` não têm texto editável.

As configurações ficam em duas tabelas globais auditadas com revisão: `InstanceNotificationSetting` (estado ligado e canais padrão, uma linha viva por tipo) e `InstanceNotificationText` (título e corpo, uma linha viva por tipo e idioma). O serviço responsável é `skeleton.notifications.instance_settings`: toda escrita exige um administrador completo da plataforma (papéis limitados não escrevem) e a revisão atual da linha (respondendo 409 em caso de revisão desatualizada).

## Configuração da organização

Quem possui a capacidade `notifications.manage` (incluída no papel Administrador) configura os avisos da organização em **Configuração › Organização › Notificações** (`/o/<org>/admin/notifications/`, [#785](https://github.com/alaorneto/skeleton/issues/785)). A tela agrupa os avisos recebíveis pela organização em **Da plataforma**, **Da organização** e um grupo para cada aplicativo ativo, sem exibir códigos técnicos. Avisos de uma app só aparecem — e só são entregues — enquanto ela estiver ativa na organização.

| Contexto do aviso | O que a organização pode fazer |
|---|---|
| `platform` | Apenas visualizar estado e texto: nunca desliga nem reescreve |
| `organization` e `app` | Desligar para todos os membros (com confirmação), se o aviso não for obrigatório e não estiver desligado pela plataforma; personalizar o texto por idioma e restaurar o padrão, salvo quando `org_editable=False` |

Quando desligado na organização, o aviso não cria linha nem gera entrega para nenhum membro dela, sem afetar outras organizações. O texto definido pela organização tem precedência sobre o da plataforma e sobre o de fábrica, valendo apenas para os destinatários dela. Os canais padrão permanecem sob controle da plataforma, pois as preferências individuais valem em todas as organizações da pessoa.

A plataforma pode travar essas configurações por organização usando as chaves `notifications.<tipo>.enabled` (gravando o valor travado na organização) e `notifications.<tipo>.text` (congelando o texto atual), inclusive na política de novas organizações ([configurações geridas](managed-settings.md)). Com a trava ativa, a tela exibe "Definido pela plataforma." e tentativas de escrita recebem `setting_locked` (409).

As escolhas são gravadas em `OrganizationNotificationSetting` e `OrganizationNotificationText` (tabelas organizacionais com RLS, auditoria e revisão) e também estão disponíveis via REST:

| Rota | O que faz |
|---|---|
| `GET /api/v1/o/<org>/notification-settings/` | Lista os avisos da organização com `context`, `enabled`, `reason` (`mandatory`, `instance_disabled`, `organization_disabled`, `platform_type`), `can_disable`, `can_edit_text`, `locked` e `revision` |
| `GET` e `PUT /api/v1/o/<org>/notification-settings/<tipo>/` | `PUT {"enabled": bool}` cria a linha quando ausente (sem `If-Match`) ou atualiza exigindo `If-Match` (428 sem o cabeçalho, 412 com revisão antiga) |
| `PUT` e `DELETE /api/v1/o/<org>/notification-settings/<tipo>/texts/<idioma>/` | `PUT {"title", "body"}` grava o texto personalizado; `DELETE` com `If-Match` restaura o padrão |

Uma app pode consultar o estado dos seus próprios tipos na organização por meio de `NotificationService.settings` ([API de serviços](service-api.md#notificationservice)).

## `notify()`

Apps disparam notificações pelo `NotificationService` em `skeleton.service_api.notifications` ([API de serviços](service-api.md#notificationservice)), que exige que a app chamadora seja a dona do tipo e que o ator leia o alvo. Um tipo registrado sem `owner_app` não pertence a nenhuma app e é recusado (`Forbidden`) para qualquer chamador. A função interna do núcleo é `skeleton.notifications.services.notify(type_code, *, recipients, target=None, payload=None, …)`, que apps não importam diretamente.

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

A tabela abaixo resume as regras da função interna; no `NotificationService`, essas recusas chegam como `ContextRequired`, `InvalidInput` ou `NotFound`.

| Regra | Resultado |
|---|---|
| Fora de contexto tenant | `MissingTenantContext` |
| Tipo desconhecido, chave de payload não declarada ou valor não escalar | `ValidationError` |
| Alvo ausente, de outra tabela ou de outra organização | `ValidationError` |
| Grupo, papel ou pessoa que não pertence à organização atual | `ValidationError` |
| `dedupe_key` repetida para o mesmo tipo | Devolve a notificação já existente |

O snapshot copia apenas os `snapshot_fields` do alvo (campos legíveis e não sensíveis). Um snapshot já gravado pode sobreviver à perda de acesso ao registro até a retenção; esse risco residual é aceito. Ao gravar a notificação, `notify()` enfileira na mesma transação o job `notifications.fanout`, responsável por criar as linhas de cada destinatário.

## Fan-out

O job `notifications.fanout` (executado como sistema, por organização) gera uma linha `NotificationRecipient` para cada pessoa autorizada **no momento do envio**:

- **Destinatários.** Podem ser pessoas listadas diretamente, membros de grupos, titulares de papéis (diretos ou via grupo) ou toda a organização. Apenas vínculos ativos de identidades ativas são incluídos, e o ator nunca recebe a própria notificação. Contas de serviço ficam de fora porque não possuem caixa de notificações.
- **Autorização no alvo.** Cada destinatário precisa passar por `require_record_action(alvo, action="read")`. Quem não lê o alvo não recebe a linha, mesmo citado diretamente; um alvo excluído não gera notificações.
- **Snapshot dos destinatários.** Quem saiu do grupo antes do fan-out não recebe o aviso, e alterações posteriores no grupo não alteram notificações já criadas.
- **Processamento em lotes.** O fan-out processa lotes de até 500 pessoas, cada lote em seu próprio job (`notification:<id>:<offset>`), e o último lote grava `fanned_out_at`. A permissão de leitura do alvo é avaliada para o lote inteiro via `users_who_can_read` (mesma regra de `require_record_action`, com número de consultas independente do tamanho do lote), e entregas já existentes são consultadas de uma só vez por canal.
- **Agrupamento.** Quando há `group_key`, uma linha não lida e não arquivada com a mesma chave recebe `count + 1` e passa a apontar para a notificação mais recente, atualizando título e corpo em vez de criar uma nova linha.
- **Texto renderizado ([#783](https://github.com/alaorneto/skeleton/issues/783)).** O fan-out seleciona o [texto do aviso](#texto-do-aviso), formata os campos e grava título e corpo na linha de cada pessoa no idioma e no fuso dela (perfil, depois organização, depois instância). A renderização ocorre uma vez por par de idioma e fuso do lote, mantendo o número de consultas constante. Esse trabalho é feito no fan-out porque ele roda no contexto da organização: a caixa global opera sem tenant e não poderia ler textos da organização, além de garantir que edições futuras não reescrevam avisos já entregues. Um campo `person` exibe o nome de quem é ou foi membro da mesma organização — ou "Pessoa indisponível" para qualquer outra pessoa — enquanto o payload guarda apenas o UUID.

**Pessoas externas.** Um tipo com `audience="external"` envia avisos a pessoas externas do [portal](portal.md). Os destinatários precisam ser indicados individualmente (`Recipients(users=...)`; grupos, papéis e organização inteira são recusados) e cada um deve possuir `ExternalAccess` ativo na organização. O tipo não tem alvo nem snapshot — apenas os campos declarados do payload — e usa somente o canal `email` (`notification_problems` acusa qualquer outro canal, alvo ou audiência desconhecida). O fan-out não cria linha na caixa de entrada: agenda apenas o email, contendo o link do portal da organização e sem link de preferências; se o acesso for revogado antes do envio, a entrega é pulada (`recipient_inactive`). A deduplicação por chave funciona normalmente, e tipos sem `audience="external"` continuam recusando quem não é membro.

Os avisos de [cobrança](billing.md#inadimplência) (`billing.*`) são tipos sem tabela-alvo, obrigatórios, enviados por email e in-app: `authorize` reconfere no fan-out se a pessoa ainda administra a organização, e `dedupe_key` garante um único envio por etapa. A exceção é `billing.usage_threshold` (aviso de 80% e 100% de um medidor de uso), que não é obrigatório — a pessoa pode desligar o canal — e leva apenas o código do medidor e os números ([medidores de uso](billing.md#medidores-de-uso)).

O aviso de limite de membros atingido (`organization.member_limit_reached`, [#698](https://github.com/alaorneto/skeleton/issues/698)) também não tem tabela-alvo nem é obrigatório: vai por email e in-app para quem tem `members.manage` (reconfirmado por `authorize` no fan-out), carrega apenas `limit` e `used`, e usa `dedupe_key` para sair no máximo uma vez por dia e valor de limite ([limites da organização](organization-limits.md#aviso-ao-chegar-no-limite)).

## Avisos padrão da organização

Toda organização conta com avisos nativos sobre alterações de membros, sem depender de nenhuma app ([#786](https://github.com/alaorneto/skeleton/issues/786)). Eles pertencem ao contexto `organization`, vêm ligados por padrão apenas na aplicação e podem ser desligados ou editados na [configuração da organização](#configuração-da-organização):

| Tipo | Quando ocorre | Quem recebe | Texto de fábrica |
|---|---|---|---|
| `organization.member_joined` | Um vínculo passa a ativo (`membership.created`: convite aceito, admissão, criação ou reativação) | Quem tem `members.manage`, exceto a própria pessoa | "{member} entrou em {organizacao}" |
| `organization.member_left` | Um vínculo ativo é encerrado (`membership.deactivated`: remoção, saída ou desativação) | Quem tem `members.manage` | "{member} não faz mais parte de {organizacao}" |

Esses avisos são criados no mesmo ponto em que o núcleo publica os [eventos de domínio](events.md) de vínculo (`membership_changed`), dentro da mesma transação: um rollback não deixa aviso pendente. O payload carrega apenas o UUID da pessoa; o nome aparece no texto para quem gerencia membros ([campo `person`](#texto-do-aviso)) e desaparece caso a pessoa solicite eliminação. Não geram aviso: contas de serviço, o primeiro vínculo de uma organização (quem a criou não tem a quem avisar) e tipos desligados pela plataforma ou pela organização (que nem chegam a criar a notificação). A própria pessoa nunca recebe aviso sobre si mesma.

## Avisos da plataforma

A plataforma mantém uma caixa exclusiva para seus administradores ([#787](https://github.com/alaorneto/skeleton/issues/787)), independente de qualquer organização. Tipos com `audience="platform"` (exclusivos do núcleo, contexto `platform`) são disparados por `skeleton.notifications.platform.notify_platform(type_code, payload=..., dedupe_key=...)`, que recusa chamadas dentro de um tenant, grava `PlatformNotification` e enfileira o job global `notifications.platform_fanout` na mesma transação. Esse caminho não existe na API de serviços: apps não enviam avisos à plataforma.

- **Quem recebe.** Administradores da plataforma ativos e, quando o tipo declara `platform_capability`, detentores de papéis limitados com essa capacidade (por exemplo, `updates.operate`). Contas de serviço e clientes da plataforma nunca recebem.
- **Canais.** Caixa (`in_app`) e email, seguindo as preferências da pessoa e os canais padrão da plataforma; modos de resumo diário ou semanal funcionam como envio imediato, e não há web push. O email usa a marca da instância, aponta para `/platform/notifications/<id>/open/`, revalida papel, preferência e estado do tipo no envio, e segue o mesmo calendário de retentativas dos emails de organização.
- **Caixa.** `PlatformNotificationRecipient` armazena uma linha por pessoa com o texto renderizado no idioma dela, estado de leitura e arquivamento, agrupamento por `group_key` e estado do email. São tabelas operacionais globais, lidas apenas por código e sempre filtradas pela pessoa autenticada.
- **Telas.** **Plataforma › Notificações › Caixa** (`/platform/notifications/`), acessível a qualquer papel da plataforma, com abas Não lidas, Todas e Arquivadas, além de ações para marcar como lida ou não lida, arquivar e marcar todas. Abrir um aviso o marca como lido e redireciona para `open_url` (reautorizado), ou exibe o próprio aviso quando não há destino. A linha de outra pessoa responde 404. Quando há avisos não lidos, o menu da plataforma e o link "Plataforma" no cabeçalho exibem um ponto indicador sem número, sinalizando atenção em vez de contagem.
- **Retenção.** O job global `notifications.platform_retention` (diário, às 03:45 UTC) aplica os mesmos prazos das caixas de organização; a eliminação da pessoa apaga imediatamente suas linhas.

## Alertas da plataforma

A plataforma monitora automaticamente condições operacionais e avisa os responsáveis ([#788](https://github.com/alaorneto/skeleton/issues/788)). O job periódico global `notifications.platform_watch` roda a cada 15 minutos, consulta apenas contagens e estados e chama `notify_platform`. Como a `dedupe_key` inclui a janela, cada ocorrência gera no máximo um aviso por janela. Cada verificação executa em sua própria transação: se uma falhar, o erro vai para o log (`platform_watch.failed`) e as demais prosseguem normalmente.

| Tipo | Quando dispara | Quem recebe | Uma vez por |
|---|---|---|---|
| `platform.jobs_dead` | Existem jobs `dead`; dispara novamente ao cruzar 1, 10, 100 e 1 000 | Administradores | Dia e faixa |
| `platform.queue_lagging` | O job vencido mais antigo espera além de `SKELETON_PLATFORM_ALERT_QUEUE_LAG_S` (900 s); nunca dispara com a fila pausada por manutenção | Administradores | Hora |
| `platform.update_available` | O verificador detecta versão nova do núcleo ou de apps | `updates.operate` | Par de versões |
| `platform.operation_failed` | Uma operação de atualização terminou `failed`, `interrupted` ou `restored` nas últimas 24 h | `updates.operate` | Operação |
| `platform.erasure_requested` | Uma pessoa solicita a eliminação dos dados (disparado na própria requisição, sem esperar o vigia) | Administradores | Pedido |
| `platform.organization_suspended` | O resumo de gestão da organização (`OrganizationSnapshot`) está `suspended` por cobrança | `billing.operate` | Organização e dia |
| `platform.billing_payment_refunded` | Um pagamento de fatura recebe estorno (parcial ou integral) ou chargeback | `billing.operate` | Pagamento e tipo (`partial` ou `full`) |
| `platform.billing_payment_duplicated` | Chega um segundo pagamento confirmado para uma fatura já paga | `billing.operate` | Pagamento |

Os payloads contêm apenas números, códigos e identificadores — nunca nomes, emails, segredos nem mensagens de erro — e o aviso leva à tela da plataforma onde o operador consulta os detalhes. Cada tipo pode ser desligado ou ter seus canais alterados em [Avisos](#configuração-da-plataforma), e `SKELETON_PLATFORM_ALERTS_ENABLED=False` desativa o vigia e o aviso de eliminação. Note que, sem um worker vivo, nenhum job roda; a ausência de workers continua sendo monitorada pela sonda de saúde e por `check_jobs`.

## Comunicados da plataforma

O administrador da plataforma envia comunicados às organizações em **Plataforma › Notificações › Comunicados** (`/platform/notifications/announcements/`, [#789](https://github.com/alaorneto/skeleton/issues/789)):

1. **Rascunho.** Define título (até 150 caracteres), texto simples (até 2 000 caracteres, sem formatação nem links clicáveis; chaves são tratadas como texto literal), público-alvo (administradores de cada organização ou todos os membros) e organizações destinatárias (todas as ativas ou uma lista de identificadores, um por linha; identificadores desconhecidos ou inativos são recusados pelo nome). O rascunho usa revisão (CAS) e suporta exclusão lógica.
2. **Revisar e enviar.** A página de confirmação informa quantas organizações e qual público receberão o comunicado. Após o envio, o comunicado não pode mais ser editado nem cancelado, e a página passa a exibir data, autor e quantidade de organizações alcançadas.

Ao enviar, o comunicado é marcado como `sent` e enfileira o job global `notifications.announce_batch`, que percorre as organizações selecionadas em lotes de 500, criando um job `notifications.announce` por organização (executado em `system_context` e transação própria). Cada organização recebe o aviso `platform.announcement` — do contexto `platform`, obrigatório na caixa (cada pessoa continua decidindo sobre o email) e com `fixed_text`, pois o conteúdo é o próprio comunicado: nem a plataforma nem a organização reescrevem o texto, e a organização não pode desligá-lo. Reexecutar os jobs não duplica envios graças à `dedupe_key` por comunicado. Os registros ficam em `PlatformAnnouncement`, tabela global auditada gravada apenas por administradores completos da plataforma.

## Caixa de notificações

| Rota | O que faz |
|---|---|
| `/notifications/` | Caixa global: exibe as linhas da pessoa em todas as organizações com vínculo ativo, por [autodescoberta](access-control.md#rls-postgresql) |
| `/o/<org>/notifications/` | Caixa da organização atual |
| `POST /o/<org>/notifications/<id>/read/`, `unread/`, `archive/` | Marca como lida, não lida ou arquivada; idempotente, protegida por CSRF |
| `POST /o/<org>/notifications/read-all/` | Marca todas as notificações como lidas na organização |
| `/o/<org>/notifications/<id>/open/` | Marca como lida e reautoriza o alvo (`require_record_action(..., "read")`): com acesso, redireciona para o registro (ou para o `open_url` do tipo, em tabelas derivadas); sem acesso ou com alvo removido, exibe uma página genérica sem título nem snapshot |
| `GET /api/v1/notifications/` | API de sessão: devolve a página da caixa global (`page`, `page_size`, `archived=0/1`) e a contagem `unread`; cada item traz `title` e `text` renderizados no envio |
| `POST /api/v1/o/<org>/notifications/actions/` | Executa `{"action": "read" \| "unread" \| "archive" \| "read_all", "ids": [...]}` (de 1 a 100 IDs) |

- A linha de outra pessoa responde 404, indistinguível de uma linha inexistente.
- Toda escrita acontece no tenant da linha; a caixa global é somente leitura.
- O cabeçalho exibe o contador de não lidas somando todas as organizações com vínculo ativo, limitado a `99+`, com número constante de consultas graças ao índice parcial `(user_id) WHERE read_at IS NULL AND archived_at IS NULL`.
- Todas as telas funcionam sem JavaScript, usando formulários HTML comuns.
- A raiz `notifications/` é reservada ao núcleo e não pode ser capturada por apps.

## Preferências

Cada pessoa define, por tipo e por canal, como deseja receber notificações em todas as suas organizações (`UserNotificationPreference`, `SystemRecord` global, auditado e com revisão). A configuração fica em `/profile/notifications/` (formulário padrão, sem JavaScript) e na API de sessão `/api/v1/me/notification-preferences/`.

A tela e a API exibem apenas os avisos que a pessoa realmente pode receber ([#790](https://github.com/alaorneto/skeleton/issues/790)): avisos para membros que não estejam desligados pela plataforma, pertencentes ao núcleo ou a alguma app ativa em pelo menos uma organização da pessoa (segundo o resumo de gestão da instância; organizações ainda sem resumo consideram todas as apps instaladas), além de avisos da plataforma para administradores ou detentores da capacidade exigida. Avisos para pessoas externas nunca aparecem, e avisos da plataforma não oferecem push ("Não disponível"). Na API, cada item informa `context` e `description`, e qualquer tipo fora desse conjunto responde 404.

Na página ([#778](https://github.com/alaorneto/skeleton/issues/778)), o padrão geral para todos os avisos aparece em um cartão próprio no topo. Abaixo dele, os tipos são agrupados em cartões por [contexto](#contextos) — Da plataforma, Da organização e um por aplicativo, pelo nome (`group_rows` em `skeleton/notifications/preference_views.py`, [#790](https://github.com/alaorneto/skeleton/issues/790)) — com descrição e sem códigos técnicos, em uma tabela de colunas fixas que se adapta em blocos abaixo de 640 px. O seletor de hora do resumo só aparece quando "Resumo diário" ou "Resumo semanal" está marcado (via CSS `:has()`; em navegadores sem suporte, fica sempre visível). Canais travados de tipos obrigatórios exibem o selo "Obrigatório" e o texto "Sempre ligado", sem campo editável enviado no POST. Os nomes dos campos (`mode:<tipo>:<canal>`, `hour:…`, `revision:…`) e o controle de revisão por linha são preservados.

| Canal | Modos |
|---|---|
| `in_app` | `off`, `immediate` |
| `email` | `off`, `immediate`, `daily`, `weekly` (com `digest_hour` de 0 a 23 no fuso da pessoa; o resumo semanal sai às segundas-feiras) |
| `push` | `off`, `immediate` |

A resolução da preferência segue a ordem do mais específico para o mais geral: linha da pessoa para o tipo, linha `*` (padrão pessoal para todos os tipos), canais padrão configurados pela plataforma ([configuração da plataforma](#configuração-da-plataforma)) e, por fim, `default_channels` do tipo em código (canais listados como `immediate`, demais como `off`). Um tipo `mandatory=True` sempre entrega na caixa `in_app`: tentar desligá-lo responde 400, e a linha `*` não o desativa.

| Rota | O que faz |
|---|---|
| `GET /api/v1/me/notification-preferences/` | Lista todos os tipos disponíveis e o `*`, com `mode`, `digest_hour`, `source` (`own`, `default`, `instance`, `type`, `mandatory`), `sysid` e `revision` da linha, quando existir |
| `GET` e `PUT /api/v1/me/notification-preferences/<tipo>/<canal>/` | `PUT {"mode": ..., "digest_hour": ...}` cria a linha quando ausente (sem `If-Match`) ou atualiza exigindo `If-Match` (428 sem o cabeçalho, 412 com revisão antiga) |
| `DELETE /api/v1/me/notification-preferences/<tipo>/<canal>/` | Exclusão lógica com `If-Match`, restaurando o nível seguinte na cadeia de resolução |

A pessoa lê e altera apenas as próprias preferências: nenhuma rota aceita identificador de usuário. Enviar o formulário com revisão antiga responde 409 sem gravar nada.

## Email imediato

Quem escolhe `email` em `immediate` (pela preferência ou pelo `default_channels` do tipo) recebe um email por notificação. O fan-out cria uma linha `NotificationDelivery` (canal `email`, [tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS de tenant) e enfileira o job `notifications.email` na mesma transação: um rollback descarta ambos, e nenhum email é disparado dentro de `notify()`.

Executando em `system_context` da organização, o job realiza três etapas:

1. **Revalidação.** Pula a entrega (`skipped`) se a pessoa foi desativada ou saiu da organização (`recipient_inactive`), desligou o email (`opted_out`) ou perdeu leitura do alvo (`target_unreadable`) desde o fan-out.
2. **Renderização.** Renderiza, no idioma e no fuso do perfil (`translation.override`, `timezone.override`), os templates `notifications/<tipo>/subject.txt`, `body.txt` e `body.html` (opcional), buscando primeiro `<nome>.<idioma>.<ext>` (como `body.en.txt`) e recorrendo a `notifications/default/`. O contexto inclui `title` e `body` (lidos da linha da pessoa ou renderizados no job caso ela tenha desligado a caixa), `body_html` (corpo escapado com `<br>`), `label`, `snapshot`, `payload`, `created_at`, `open_url` e `preferences_url`. Os templates padrão usam o título como assunto; templates por tipo continuam válidos como layout e recebem o mesmo contexto.
3. **Envio e retentativas.** Envia com o cabeçalho `List-Unsubscribe: <…/profile/notifications/>`. Em caso de falha, incrementa as tentativas e reagenda após 1 min, 5 min, 30 min e 2 h; depois da quinta tentativa, a entrega passa para `dead`. Uma entrega já `sent` nunca é reenviada: a entrega é pelo menos uma vez e idempotente por linha.

Os links dos emails usam `SKELETON_PUBLIC_URL`, que deve ser uma origem sem caminho (`https://app.example.com`). Sem ela, os links saem relativos, o cabeçalho `List-Unsubscribe` é omitido e `check --deploy` emite `skeleton.W035`. Quando a organização possui endereço próprio ativo ([#487](https://github.com/alaorneto/skeleton/issues/487)), todos os links (abrir, caixa, preferências e `List-Unsubscribe`, tanto no email imediato quanto no resumo e no push) são montados em forma limpa no domínio dela (`https://acme.skeleton.com/notifications/<id>/open/`, `https://acme.skeleton.com/profile/notifications/`) via `skeleton.tenancy.site.organization_url`. A URL é construída no envio a partir do índice global e de `SKELETON_PUBLIC_URL`, nunca a partir do `Host` de uma requisição anterior; o índice é consultado uma única vez por organização e unidade de trabalho.

A inscrição de web push é vinculada à origem: quem ativa push tanto no domínio principal quanto no endereço próprio da organização recebe duas vezes (limitação conhecida). Emails com tokens ou links secretos (allauth e convites) continuam usando `schedule_mail` e não passam por esta fila.

Os templates padrão existem em português e inglês; o inglês só entra em uso quando o projeto adiciona `en` a `LANGUAGES`, pois o núcleo entrega apenas `pt-BR`. Idioma e fuso de cada destinatário seguem a mesma precedência das telas: perfil, depois organização e, por fim, instância.

### Layout e marca

Os emails HTML do núcleo estendem `skeleton/email/base.html` ([#173](https://github.com/alaorneto/skeleton/issues/173)), cobrindo notificações, resumos e convites. O layout inclui:

- faixa superior e nome na cor da organização;
- logo, quando configurado, embutido como parte inline `Content-ID: <organization-logo>` referenciada por `cid:organization-logo`, nunca por URL pública;
- rodapé.

Esses dados vêm das configurações da organização no contexto em que o email é montado (o job da organização ou a requisição de convite). O logo de uma organização nunca aparece no email de outra.

### Marca nos e-mails

A precedência da identidade visual é organização → instância → núcleo ([#600](https://github.com/alaorneto/skeleton/issues/600), [aparência](appearance.md#e-mails)):

- **Com organização no contexto:** usa nome, cor e logo da organização. Se ela não definir cor própria, herda a cor do tema ativo da instância; o logo da instância nunca entra em emails de uma organização.
- **Sem organização:** usa nome do produto, cor do tema ativo e logo da instância (inline com `cid:organization-logo`, mantendo o mesmo `Content-ID` para os templates).
- **Sem marca da instância:** mantém o padrão sem nome e com a cor `#1d4ed8`.

Emails de conta enviados pelo allauth (confirmação de endereço e redefinição de senha) são texto simples e não usam `skeleton/email/base.html`: no domínio principal usam o nome do produto da instância (quando definido) e, no endereço próprio de uma organização, o nome publicado dela.

A versão em texto simples continua sem marca. Os convites usam `skeleton/tenancy/invitation/{subject,body}.txt` e `body.html`, renderizados no idioma e fuso padrão da organização (e depois da instância), já que a pessoa convidada pode ainda não ter conta.

## Resumo diário ou semanal

Quem escolhe `daily` ou `weekly` no canal `email` recebe um resumo consolidado em vez de emails individuais. O job periódico `notifications.digest` roda a cada hora, por organização, em transação própria:

- Seleciona membros ativos cuja `digest_hour` corresponde à hora atual **no fuso do perfil** (no modo semanal, apenas às segundas-feiras).
- Reúne até 50 linhas da caixa (`NotificationRecipient`) ainda não lidas, não arquivadas e ainda fora de resumo para os tipos configurados, gravando `digest_sent_at` para garantir que nenhuma linha entre em dois resumos.
- Cria uma `NotificationDelivery` (canal `digest`) por pessoa e hora local, com `dedupe_key` `digest:<pessoa>:<AAAAMMDDHH>`, e reutiliza o job `notifications.email` com o mesmo calendário de retentativas. Reexecutar a mesma janela não gera um segundo resumo.
- Resumos vazios não são criados. Itens lidos ou arquivados entre a criação e o envio são descartados; se não restar nenhum item, a entrega termina `skipped` (`empty_digest`).

Os templates são `notifications/digest/subject.txt`, `body.txt` e `body.html` (com variantes por idioma), listando título e corpo de cada linha. Como o resumo lê da caixa in-app, um tipo com `in_app` desligado não gera linha e não entra no resumo. Uma pessoa presente em várias organizações recebe um resumo por organização.

O job recebe a janela agendada (`window`), de modo que mesmo atrasado ainda envia o resumo referente àquela hora; ficam puladas apenas janelas nunca agendadas (agendador parado) ou horas locais inexistentes em mudanças de horário de verão, cujos itens seguem para o próximo resumo. No momento do envio, cada item é reautorizado exatamente como no email imediato: alvos que a pessoa deixou de ler são omitidos, e um resumo sem itens legíveis termina `skipped` (`empty_digest`).

## Web push

Quando o push está configurado na instância, `/profile/notifications/` exibe a seção "Push neste navegador" (oculta sem JavaScript ou sem suporte do navegador, sem prejuízo ao restante da página). Ao ativar, `site.js` registra o service worker `/sw.js` (escopo `/`) e envia a assinatura do navegador com sessão e CSRF:

| Rota | O que faz |
|---|---|
| `POST /api/v1/me/push-subscriptions/` | `{"endpoint", "keys": {"p256dh", "auth"}}` registra o navegador da pessoa autenticada (201). Endpoints fora de `WEBPUSH_ALLOWED_HOSTS`, sem HTTPS, com porta ou com credenciais na URL retornam 400. Se o endpoint pertencia a outra conta no mesmo navegador, a assinatura antiga é excluída |
| `POST /api/v1/me/push-subscriptions/unsubscribe/` | `{"endpoint"}` exclui logicamente a assinatura da própria pessoa (204) |
| `GET /sw.js` | Entrega o service worker apenas quando o push está configurado (404 caso contrário) |

`WebPushSubscription` é um `SystemRecord` global: o endpoint (uma URL de capacidade) e as duas chaves são cifrados com o keyring `INTEGRATION_ENCRYPTION_KEYS` e declarados em `sensitive_fields` (redigidos na auditoria). O campo `endpoint_digest` (SHA-256) permite localizar a assinatura sem decifrá-la.

Para quem tem `push` em `immediate`, cada notificação cria uma `NotificationDelivery` (canal `push`) e enfileira o job `notifications.push`:

- O job repete as verificações do email (pessoa ativa, preferência e leitura do alvo) e envia a todas as assinaturas vivas da pessoa. As assinaturas não ficam bloqueadas durante a chamada de rede — inscrever ou cancelar nunca espera um envio —, e o resultado de cada assinatura é gravado após o último envio, relendo a linha com lock limitado a 5 s.
- O payload mínimo é `{"title": <rótulo do tipo>, "url": <link de abertura>}`. O texto do aviso (título e corpo renderizados, ou snapshot em linhas antigas) só entra em `body` quando o tipo declara `push_snapshot=True`, evitando expor dados do alvo na tela bloqueada sem consentimento explícito do tipo.
- O corpo é cifrado com `aes128gcm` (RFC 8291) e assinado com VAPID ES256 (RFC 8292) usando `cryptography` (já presente no lock), sem depender de `pywebpush` nem adicionar `aiohttp` ou `requests`. Todas as saídas passam por `skeleton.common.outbound_http` (proteção anti-SSRF) e restringem-se aos hosts da allowlist.
- Cada pessoa mantém no máximo 10 assinaturas vivas: uma nova assinatura exclui logicamente a mais antiga, e o envio considera apenas as 10 mais recentes. `last_success_at` é atualizado no máximo uma vez por dia, evitando que cada push gere uma escrita auditada.
- Respostas `2xx` confirmam o envio; `404` ou `410` excluem logicamente a assinatura sem retentativa; `429` e `5xx` seguem o mesmo calendário de retentativas do email até `dead`.

## Retenção

Como as tabelas de notificação são operacionais, a exclusão física ocorre apenas no job periódico `notifications.retention` (diário, às 03:30 UTC, por organização e no `system_context` dela). Os prazos são configuráveis via settings e catálogo da instância:

| Setting | Padrão | O que remove |
|---|---|---|
| `SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS` | 30 | Entregas finalizadas (`sent`, `skipped`, `dead`) mais antigas que o prazo e também entregas abertas (`pending`, `retrying`) que ultrapassam o prazo — como o calendário de retentativas dura horas, uma entrega aberta há tantos dias foi abandonada por um job encerrado. Dentro do prazo, entregas abertas são mantidas |
| `SKELETON_NOTIFICATION_READ_RETENTION_DAYS` | 90 | Linhas da caixa lidas ou arquivadas há mais tempo que o prazo |
| `SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS` | 180 | Linhas não lidas cuja última atualização (incluindo agrupamentos) excede o prazo e, em seguida, as `Notification` mais antigas que o prazo (distribuídas ou não, pois um job de fan-out encerrado pode deixar `fanned_out_at` vazio) que não sejam mais referenciadas por nenhuma linha nem entrega |

A exclusão respeita as chaves estrangeiras `PROTECT`: primeiro entregas, depois linhas da caixa e, por fim, notificações órfãs. O contador de não lidas permanece consistente com as linhas restantes, e uma `dedupe_key` só volta a ser aceita depois que a notificação original sai pela retenção.

A eliminação a pedido do titular ([#191](https://github.com/alaorneto/skeleton/issues/191)) utiliza `forget_person`, declarado ao lado da retenção: em cada organização, apaga as entregas e as linhas da pessoa, deixando as notificações órfãs para a retenção ([privacidade](privacy.md#eliminação-da-identidade)). Linhas de outras pessoas que citavam o titular pelo nome (`subject_people`, como "Ana entrou na organização") passam a mostrar apenas o rótulo do tipo, garantindo que nenhuma notificação continue nomeando a pessoa eliminada.
