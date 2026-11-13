# Notificações

O núcleo oferece notificações in-app como infraestrutura (`skeleton/notifications`, label `sk_notifications`). Qualquer app registra tipos de notificação e chama `notify()` dentro da transação do negócio: um rollback não deixa notificação nem job.

## Tabelas

| Modelo | O que guarda | Proteção |
|---|---|---|
| `Notification` | Tipo, ator, alvo (`target_table_key`, `target_sysid`), payload, snapshot, especificação dos destinatários, `group_key`, `dedupe_key` | [Tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS tenant; campos imutáveis |
| `NotificationRecipient` | A cópia de cada pessoa: `read_at`, `archived_at`, `count` agrupada e, copiados no fan-out, tipo, alvo, snapshot e payload, porque a caixa entre organizações nunca lê `Notification` (visível só no próprio tenant) | RLS tenant com [autodescoberta](access-control.md#rls-postgresql) por `user_id`: a pessoa lê as próprias linhas de todas as organizações, e só escreve no tenant atual |

Nenhuma das duas entra no gateway, e a exclusão física fica para o serviço de retenção.

## Registrar um tipo

```python
from skeleton.notifications.registry import NotificationType, register_notification_type

register_notification_type(
    NotificationType(
        "tasks.assigned",
        "Tarefa atribuída",
        payload_fields=("message",),
        snapshot_fields=("code", "title"),
        target_table_key="tasks.task",
        group_key="task:{target}",
        owner_app="tasks",
    )
)
```

O check `skeleton.E023` reprova tipo duplicado, tabela-alvo desconhecida e campo de snapshot fora de `read_fields`, e também reprova campo de snapshot ou de payload que esteja em `sensitive_fields`.

Um tipo **sem tabela-alvo** pode declarar `open_url=função(user, row)`: ao abrir a notificação, a função reautoriza a pessoa e devolve o endereço, ou `None` (página genérica). Os tipos dos [fluxos de processo](flows.md#integrações) (`flows.task_assigned`, `flows.task_reminder` e `flows.task_overdue`) usam isso para levar à tarefa só quem ainda pode vê-la; o payload deles tem o rótulo do fluxo e da etapa, o número do processo e o identificador da tarefa, nunca dados de formulário.

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
- Lotes de até 10 mil pessoas: cada lote é um job (`notification:<id>:<offset>`), e o último marca `fanned_out_at`.
- Agrupamento: com `group_key`, uma linha não lida e não arquivada da mesma chave recebe `count + 1` e passa a apontar para a notificação mais recente, em vez de criar outra linha.

## Caixa de notificações

| Rota | O que faz |
|---|---|
| `/notifications/` | Caixa global: as linhas da pessoa em todas as organizações com vínculo ativo, por [autodescoberta](access-control.md#rls-postgresql) |
| `/o/<org>/notifications/` | Caixa da organização atual |
| `POST /o/<org>/notifications/<id>/read/`, `unread/`, `archive/` | Marca lida, não lida ou arquivada; idempotente, com CSRF |
| `POST /o/<org>/notifications/read-all/` | Marca todas como lidas na organização |
| `/o/<org>/notifications/<id>/open/` | Marca lida e reautoriza o alvo (`require_record_action(..., "read")`): com acesso, redireciona ao registro; sem acesso ou alvo removido, mostra uma página genérica, sem título nem snapshot |
| `GET /api/v1/notifications/` | API de sessão: página da caixa global (`page`, `page_size`, `archived=0/1`) mais `unread` |
| `POST /api/v1/o/<org>/notifications/actions/` | `{"action": "read" \| "unread" \| "archive" \| "read_all", "ids": [...]}` (1 a 100 ids) |

- A linha de outra pessoa responde 404, igual a uma linha inexistente.
- Toda escrita acontece no tenant da linha; a caixa global só lê.
- O cabeçalho mostra o contador de não lidas de todas as organizações com vínculo ativo, limitado a `99+`. O número de consultas é constante (índice parcial `(user_id) WHERE read_at IS NULL AND archived_at IS NULL`).
- Tudo funciona sem JavaScript: as ações são formulários comuns.
- A raiz `notifications/` é reservada ao núcleo: uma app não pode capturá-la.

## Preferências

Cada pessoa escolhe, por tipo e canal, como recebe as notificações em todas as organizações (`UserNotificationPreference`, `SystemRecord` global, auditado e com revisão). A página fica em `/profile/notifications/` (formulário comum, sem JavaScript) e a API de sessão em `/api/v1/me/notification-preferences/`.

| Canal | Modos |
|---|---|
| `in_app` | `off`, `immediate` |
| `email` | `off`, `immediate`, `daily`, `weekly` (com `digest_hour`, de 0 a 23, no fuso da pessoa; o semanal sai às segundas-feiras) |
| `push` | `off`, `immediate` |

A resolução vai do mais específico ao mais geral: a linha da pessoa para o tipo, a linha `*` (padrão dela para todos os tipos) e, sem linha, o `default_channels` do tipo (canal listado é `immediate`, os demais `off`). Um tipo `mandatory=True` sempre chega à caixa in-app: desligá-lo responde 400, e a linha `*` não o desliga.

| Rota | O que faz |
|---|---|
| `GET /api/v1/me/notification-preferences/` | Todos os tipos registrados e o `*`, com `mode`, `digest_hour`, `source` (`own`, `default`, `type`, `mandatory`), `sysid` e `revision` da linha, quando existe |
| `GET`/`PUT /api/v1/me/notification-preferences/<tipo>/<canal>/` | `PUT {"mode": ..., "digest_hour": ...}`. Sem linha, cria (sem `If-Match`); com linha, exige `If-Match` com a revisão (428 sem ela, 412 desatualizada) |
| `DELETE /api/v1/me/notification-preferences/<tipo>/<canal>/` | Exclusão lógica com `If-Match`: volta a valer o nível seguinte da resolução |

A pessoa só lê e altera as próprias linhas: nenhuma rota recebe identificador de usuário. Um formulário com revisão antiga responde 409 sem gravar nada.

## Email imediato

Quem tem `email` em `immediate` (pela preferência ou pelo `default_channels` do tipo) recebe um email por notificação. O fan-out cria uma `NotificationDelivery` (canal `email`, [tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS tenant) e enfileira o job `notifications.email` na mesma transação: um rollback não deixa nem a entrega nem o job, e nada sai durante o `notify()`.

O job, em `system_context` da organização:

1. Pula a entrega (`skipped`) se a pessoa foi desativada, saiu da organização (`recipient_inactive`), desligou o email (`opted_out`) ou perdeu a leitura do alvo (`target_unreadable`) desde o fan-out.
2. Renderiza no idioma e no fuso do perfil (`translation.override`, `timezone.override`) os templates `notifications/<tipo>/subject.txt`, `body.txt` e `body.html` (opcional), procurando antes `<nome>.<idioma>.<ext>` (`body.en.txt`) e caindo em `notifications/default/`. O contexto traz `label`, `snapshot`, `payload`, `created_at`, `open_url` e `preferences_url`.
3. Envia com `List-Unsubscribe: <…/profile/notifications/>`. Uma falha conta uma tentativa e agenda outra em 1 min, 5 min, 30 min e 2 h; depois da quinta, a entrega fica `dead`. Uma entrega `sent` nunca sai de novo: a entrega é pelo menos uma vez, idempotente por linha.

Os links usam `SKELETON_PUBLIC_URL`, que precisa ser uma origem (`https://app.example.com`, sem caminho): sem ela os links saem relativos e o cabeçalho `List-Unsubscribe` é omitido, e o `check --deploy` avisa com `skeleton.W035` (#279). Emails com token ou link secreto (allauth, convites) continuam em `schedule_mail` e nunca passam por esta fila. Os textos dos templates padrão existem em português e inglês. O inglês só é usado quando o projeto acrescenta `en` a `LANGUAGES`, porque o núcleo entrega apenas pt-BR. O idioma e o fuso de cada destinatário seguem a mesma ordem das telas: perfil, depois organização, depois instância (#170).

### Layout e marca

Os emails HTML do núcleo estendem `skeleton/email/base.html` ([#173](https://github.com/alaorneto/skeleton/issues/173)). Isso inclui a notificação, o resumo e o convite. O layout tem:

- uma faixa e o nome na cor da organização;
- o logo, quando houver, como parte inline `Content-ID: <organization-logo>` referenciada por `cid:organization-logo`, nunca por URL pública;
- um rodapé.

Os dados vêm das configurações da organização do contexto em que o email é montado: o job da organização ou a requisição do convite. O logo de uma organização nunca vai no email de outra.

O texto simples continua sem marca. O convite usa os templates `skeleton/tenancy/invitation/{subject,body}.txt` e `body.html`. Ele sai no idioma e no fuso padrão da organização, depois da instância, porque a pessoa convidada pode ainda não ter conta.

## Resumo diário ou semanal

Quem escolhe `daily` ou `weekly` no canal `email` recebe um resumo em vez de um email por notificação. O job periódico `notifications.digest` roda a cada hora, por organização e numa transação própria:

- Seleciona os membros ativos cuja `digest_hour` é a hora atual **no fuso do perfil**; o semanal só às segundas-feiras.
- Reúne as linhas da caixa (`NotificationRecipient`) não lidas, não arquivadas e ainda fora de resumo, dos tipos em resumo, até 50 por email, e marca `digest_sent_at`: uma linha nunca entra em dois resumos.
- Cria uma `NotificationDelivery` (canal `digest`) por pessoa e hora local, com `dedupe_key` `digest:<pessoa>:<AAAAMMDDHH>`, e o mesmo job `notifications.email` envia com as mesmas retentativas. Rodar a mesma janela de novo não cria outro resumo.
- Resumo vazio não é criado. Itens lidos ou arquivados entre a criação e o envio saem do email; se nenhum sobrar, a entrega fica `skipped` (`empty_digest`).

Templates: `notifications/digest/subject.txt` e `body.txt|html`, com as variantes por idioma. O resumo junta a caixa in-app: um tipo com `in_app` desligado não tem linha e não entra no resumo. Uma pessoa em N organizações recebe N resumos; um resumo único entre organizações fica para depois. O job recebe a janela para a qual foi agendado (`window`): um job atrasado ainda envia o resumo daquela hora (#276). Continuam puladas só a janela que nunca foi agendada (agendador parado) e a hora local que não existe na mudança de horário; os itens seguem para o próximo resumo. No envio, cada item é reautorizado como no email imediato: um alvo que a pessoa deixou de ler fica fora, e um resumo sem nenhum item legível termina `skipped` (`empty_digest`).

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
- Payload mínimo: `{"title": <rótulo do tipo>, "url": <link de abertura>}`. O snapshot do alvo (o título, por exemplo) só entra quando o tipo declara `push_snapshot=True`.
- O corpo é cifrado com `aes128gcm` (RFC 8291) e assinado com VAPID ES256 (RFC 8292). Os dois são implementados com `cryptography`, que já está no lock, e não com `pywebpush`, para não trazer `aiohttp`, `requests` e outras dependências. A saída é só pelo `skeleton.common.outbound_http` (anti-SSRF), e de novo só para hosts da allowlist.
- 2xx conta como enviado. 404 ou 410 exclui logicamente a assinatura, sem retentativa. 429 e 5xx seguem o mesmo calendário de retentativas do email até `dead`.

## Retenção

As tabelas de notificação são operacionais, e a exclusão física só acontece no job periódico `notifications.retention` (diário, às 03:30 UTC, por organização e no `system_context` dela). Prazos (settings e chaves da configuração da instância):

| Setting | Padrão | O que remove |
|---|---|---|
| `SKELETON_NOTIFICATION_DELIVERY_RETENTION_DAYS` | 30 | Entregas finalizadas (`sent`, `skipped`, `dead`) criadas antes do prazo, e as abertas (`pending`, `retrying`) também: o calendário de retentativas dura horas, então uma entrega aberta há mais tempo que o prazo foi abandonada por um job que morreu (#277). Dentro do prazo, as abertas ficam |
| `SKELETON_NOTIFICATION_READ_RETENTION_DAYS` | 90 | Linhas da caixa lidas ou arquivadas há mais tempo que o prazo |
| `SKELETON_NOTIFICATION_UNREAD_RETENTION_DAYS` | 180 | Linhas não lidas cuja última atualização (incluindo o agrupamento) é mais antiga que o prazo; e, depois delas, as `Notification` mais antigas que o prazo, distribuídas ou não (um job de fan-out que morreu deixa `fanned_out_at` vazio, #277), que nenhuma linha nem entrega referencia |

A ordem respeita as referências `PROTECT`: primeiro as entregas, depois as linhas e por último as notificações órfãs. O contador de não lidas continua igual ao número de linhas não lidas que restam. Uma `dedupe_key` volta a aceitar uma notificação nova só depois que a antiga sai pela retenção. A retenção por LGPD (expurgo a pedido) fica para o epic do ciclo 2.
