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

## `notify()`

Apps usam `NotificationService` de `skeleton.service_api.notifications` ([API de serviços](service-api.md#notificationservice)), que exige que a app chamadora seja a dona do tipo e que o ator leia o alvo. A função interna abaixo é do núcleo.

```python
from skeleton.notifications.services import Recipients, notify

notify("tasks.assigned", recipients=Recipients(users=(assignee.pk,)), target=task, payload={"message": "Nova tarefa"})
```

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

- Destinatários: pessoas citadas, membros dos grupos, quem tem os papéis (diretamente ou por grupo) ou toda a organização. Só vínculos ativos de identidades ativas contam, e o ator nunca recebe a própria notificação. Contas de serviço ficarão fora do diretório quando existirem (#44).
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
