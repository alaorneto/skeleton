# Notificações

O núcleo oferece notificações in-app como infraestrutura (`skeleton/notifications`, label `sk_notifications`). Qualquer app registra tipos de notificação e chama `notify()` dentro da transação do negócio: um rollback não deixa notificação nem job.

## Tabelas

| Modelo | O que guarda | Proteção |
|---|---|---|
| `Notification` | Tipo, ator, alvo (`target_table_key`, `target_sysid`), payload, snapshot, especificação dos destinatários, `group_key`, `dedupe_key` | [Tabela operacional](data-model.md#tabelas-operacionais-adr) com RLS tenant; campos imutáveis |
| `NotificationRecipient` | A cópia de cada pessoa: `read_at`, `archived_at`, `count` agrupada | RLS tenant com [autodescoberta](access-control.md#rls-postgresql) por `user_id`: a pessoa lê as próprias linhas de todas as organizações, e só escreve no tenant atual |

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
