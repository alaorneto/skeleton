# API de serviços: Anotações

## Visão geral
`NoteService` é o contrato em processo da app de exemplo Notes. Outras apps o utilizam para listar, consultar, criar e alterar notas na organização atual, sempre respeitando a autorização do ator no contexto. As superfícies da própria app seguem o mesmo caminho: tanto a página `/o/<org>/apps/example_notes/` quanto o sumário REST chamam `note_count`. O contrato geral está em [docs/service-api.md](../../../docs/service-api.md).

## Dependência
`app_id`: `org.skeleton.example-notes`. Faixa recomendada no manifesto da app consumidora: `{"org.skeleton.example-notes": ">=0.4.0,<1.0.0"}` (necessária para o fluxo de publicação; `>=0.3.0` basta para os demais métodos). Importe exclusivamente `applications.notes.service_api` (ou `examples.notes.service_api` no checkout); importar qualquer outro módulo da app dispara o check `skeleton.E019`.

## Serviços

### `NoteService`
Todos os métodos exigem que a app esteja ativa na organização (`Unavailable` caso contrário), que o contexto tenant tenha sido aberto pelo chamador e que `actor` coincida com o ator do contexto (`Forbidden` se divergir). Nenhum método abre `system_context`, faz commit nem executa efeitos externos.

#### `note_count(*, actor) -> int`
Conta quantas notas o ator tem permissão para ler.
- **Autorização:** `TablePermission` de `read` em `example_notes.note`; contabiliza apenas registros legíveis.
- **Erros:** `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().note_count(actor=request.user)`

#### `list_notes(*, actor, code=None, cursor=None, page_size=25) -> Page[NoteSummary]`
Lista as notas legíveis em ordem de criação, com filtro opcional por código exato.
- **Autorização:** `read` em `example_notes.note`; a filtragem de acesso ocorre antes da paginação.
- **Erros:** `InvalidInput` para cursor inválido, `page_size` fora de `1..100` ou `code` que não seja texto; `Forbidden`; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().list_notes(actor=actor, page_size=50, cursor=page.next_cursor)`

#### `recent_notes(*, actor, limit=5) -> tuple[NoteSummary, ...]`
Devolve as notas legíveis modificadas mais recentemente, da mais nova para a mais antiga (desde a versão 0.6.0; utilizado pelo quadro `example_notes.recent` do painel).
- **Autorização:** `read` em `example_notes.note`; retorna apenas registros legíveis.
- **Erros:** `InvalidInput` para `limit` fora de `1..20`; `Forbidden`; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().recent_notes(actor=request.user, limit=5)`

#### `get_note(*, actor, note_id) -> NoteSummary`
Devolve uma nota legível pelo seu identificador.
- **Autorização:** `read` na tabela e acesso de leitura ao registro.
- **Erros:** `NotFound` se a nota não existir, pertencer a outra organização ou não for legível (respostas idênticas); `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().get_note(actor=actor, note_id=uuid)`

#### `create_note(*, actor, code, title, body="") -> NoteSummary`
Cria uma nota privada pertencente ao ator.
- **Autorização:** `create` em `example_notes.note`.
- **Erros:** `InvalidInput` para campos vazios, longos demais ou que não sejam texto; `Conflict("integrity_conflict")` se outra nota viva já usar o mesmo código; `Forbidden`; `Unavailable`.
- **Efeitos:** grava `Note`, auditada por trigger na transação do chamador. Como o código é único entre notas vivas, repetições já são seguras e não exigem `idempotency_key`.
- **Exemplo:** `NoteService().create_note(actor=actor, code="n-1", title="Primeira")`

#### `update_note(*, actor, note_id, expected_revision, code=None, title=None, body=None) -> NoteSummary`
Altera código, título ou texto de uma nota na revisão esperada.
- **Autorização:** `update` na tabela e acesso de escrita ao registro.
- **Erros:** `NotFound` se o registro não for legível; `Forbidden` sem permissão de escrita; `InvalidInput` quando nenhum campo é informado ou há valores inválidos; `Conflict("stale_revision")` se a revisão mudou; `Conflict("integrity_conflict")` em caso de código repetido; `Unavailable`.
- **Efeitos:** atualiza `Note` (auditada, com `body` redigido). Nunca faz novas tentativas em silêncio: reler e repetir é decisão do chamador.
- **Exemplo:** `NoteService().update_note(actor=actor, note_id=info.sysid, expected_revision=info.revision, title="Novo")`

#### `request_publication(*, actor, note_id, idempotency_key=None) -> PublicationStatus`
Inicia o fluxo de publicação da nota (`example_notes.publication`, [fluxos](../../../docs/flows.md)).
- **Autorização:** leitura e escrita na nota (a ação de início do fluxo é `update`).
- **Erros:** `NotFound` se a nota não existir, for de outra organização ou não for legível; `Forbidden` sem a permissão de tabela ou sem escrita na nota; `Conflict("already_running")` se já existir um pedido de publicação em andamento para a nota; `Unavailable`.
- **Efeitos:** inicia a instância do fluxo e abre a tarefa de preparação para o ator na transação do chamador. Repetir a mesma `idempotency_key` devolve o pedido já aberto (`created=False`).
- **Exemplo:** `NoteService().request_publication(actor=request.user, note_id=note.sysid, idempotency_key=f"pub:{note.sysid}")`

#### `publication_status(*, actor, note_id) -> PublicationStatus | None`
Devolve o pedido de publicação mais recente da nota, ou `None` quando não houver nenhum.
- **Autorização:** leitura da nota; o pedido retornado respeita o que o ator pode ler via `FlowService`.
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().publication_status(actor=request.user, note_id=note.sysid)`

## DTOs

### `PublicationStatus`
| Campo | Tipo | Significado |
|---|---|---|
| `instance_id` | UUID | Identificador da instância do fluxo no `FlowService` |
| `number` | str | Número do processo (vazio, pois o exemplo não usa numeração) |
| `status` | str | `running`, `suspended`, `completed`, `cancelled` ou `failed` |
| `outcome` | str | `published` ou `withdrawn` quando concluído |
| `revision` | int | Revisão atual da instância |
| `created` | bool | `False` quando a chave de idempotência já existia e em consultas |

### `NoteSummary`
| Campo | Tipo | Significado |
|---|---|---|
| `sysid` | UUID | Identificador estável da nota |
| `code` | str | Código único entre notas vivas da organização |
| `title` | str | Título da nota |
| `visibility` | str | `private`, `organization` ou `group` |
| `revision` | int | Revisão atual, usada em `expected_revision` |
| `created_at` | datetime (UTC) | Data e hora de criação |
| `updated_at` | datetime (UTC) | Data e hora da última alteração |

O campo `body` é classificado como sensível no `TableSpec` e não faz parte de nenhum DTO.

## Eventos publicados
Não se aplica: a app de exemplo não publica eventos de domínio.

## Histórico de versões
| Versão | Mudança |
|---|---|
| 0.3.0 | Primeira versão: `note_count`, `list_notes`, `get_note`, `create_note`, `update_note` e `NoteSummary` |
| 0.4.0 | `request_publication`, `publication_status` e `PublicationStatus`, sobre o fluxo de processo `example_notes.publication` (núcleo com `skeleton.flows`) |
| 0.6.0 | `recent_notes`, para alimentar o quadro `example_notes.recent` do painel |
