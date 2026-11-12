# API de serviços: Anotações

## Visão geral
`NoteService` é o contrato em processo do exemplo Notes. Outra app o usa para listar, consultar, criar e alterar notas da organização atual, sempre com a autorização do ator que está no contexto. É também o caminho das superfícies da própria app: a página `/o/<org>/apps/example_notes/` e o sumário REST chamam `note_count`. O contrato geral está em [docs/service-api.md](../../../docs/service-api.md).

## Dependência
`app_id`: `org.skeleton.example-notes`. Faixa recomendada no manifesto do consumidor: `{"org.skeleton.example-notes": ">=0.4.0,<1.0.0"}` (para o fluxo de publicação; `>=0.3.0` basta para os demais métodos). Importe só `applications.notes.service_api` (ou `examples.notes.service_api` no checkout); qualquer outro módulo da app reprova o check E019.

## Serviços

### `NoteService`
Todos os métodos exigem a app ativa na organização (`Unavailable` se não estiver), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden` se divergir). Nenhum abre `system_context`, faz commit ou executa efeito externo.

#### `note_count(*, actor) -> int`
Conta as notas que o ator pode ler.
- **Autorização:** `TablePermission` read em `example_notes.note`; conta só registros legíveis.
- **Erros:** `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().note_count(actor=request.user)`

#### `list_notes(*, actor, code=None, cursor=None, page_size=25) -> Page[NoteSummary]`
Lista notas legíveis por ordem de criação, com filtro opcional por código exato.
- **Autorização:** read em `example_notes.note`; filtra antes de paginar.
- **Erros:** `InvalidInput` para cursor, `page_size` fora de 1..100 ou `code` que não seja texto; `Forbidden`; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().list_notes(actor=actor, page_size=50, cursor=page.next_cursor)`

#### `get_note(*, actor, note_id) -> NoteSummary`
Devolve uma nota legível.
- **Autorização:** read na tabela e acesso de leitura ao registro.
- **Erros:** `NotFound` se a nota não existir, for de outra organização ou não for legível (respostas idênticas); `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().get_note(actor=actor, note_id=uuid)`

#### `create_note(*, actor, code, title, body="") -> NoteSummary`
Cria uma nota privada do ator.
- **Autorização:** create em `example_notes.note`.
- **Erros:** `InvalidInput` para campos vazios, longos ou que não sejam texto; `Conflict("integrity_conflict")` se outra nota viva usar o código; `Forbidden`; `Unavailable`.
- **Efeitos:** grava `Note`, auditada por trigger na transação do chamador. O código único torna a repetição segura, por isso não há `idempotency_key`.
- **Exemplo:** `NoteService().create_note(actor=actor, code="n-1", title="Primeira")`

#### `update_note(*, actor, note_id, expected_revision, code=None, title=None, body=None) -> NoteSummary`
Altera código, título ou texto na revisão esperada.
- **Autorização:** update na tabela e acesso de escrita ao registro.
- **Erros:** `NotFound` se não for legível; `Forbidden` sem escrita; `InvalidInput` sem campos ou com valores inválidos; `Conflict("stale_revision")` se a revisão mudou; `Conflict("integrity_conflict")` para código repetido; `Unavailable`.
- **Efeitos:** atualiza `Note` (auditada, com `body` redigido). Nunca tenta de novo em silêncio: reler e repetir é decisão do chamador.
- **Exemplo:** `NoteService().update_note(actor=actor, note_id=info.sysid, expected_revision=info.revision, title="Novo")`

#### `request_publication(*, actor, note_id, idempotency_key=None) -> PublicationStatus`
Inicia o fluxo de publicação da nota (`example_notes.publication`, [fluxos](../../../docs/flows.md)).
- **Autorização:** leitura da nota e escrita nela (a ação de início do fluxo é `update`).
- **Erros:** `NotFound` se a nota não existir, for de outra organização ou não for legível; `Forbidden` sem a permissão de tabela ou sem escrita na nota; `Conflict("already_running")` se a nota já tem um pedido em andamento; `Unavailable`.
- **Efeitos:** inicia a instância do fluxo e abre a tarefa de preparação para o ator, na transação do chamador. A chave de idempotência repetida devolve o mesmo pedido (`created=False`).
- **Exemplo:** `NoteService().request_publication(actor=request.user, note_id=note.sysid, idempotency_key=f"pub:{note.sysid}")`

#### `publication_status(*, actor, note_id) -> PublicationStatus | None`
Devolve o pedido de publicação mais recente da nota, ou `None` quando não há pedido.
- **Autorização:** leitura da nota; o pedido é o que o ator pode ler pelo `FlowService`.
- **Erros:** `NotFound`; `Forbidden` sem a permissão de tabela; `Unavailable`.
- **Efeitos:** nenhum.
- **Exemplo:** `NoteService().publication_status(actor=request.user, note_id=note.sysid)`

## DTOs

### `PublicationStatus`
| Campo | Tipo | Significado |
|---|---|---|
| `instance_id` | UUID | Instância do fluxo, para o `FlowService` |
| `number` | str | Número do processo (vazio: o exemplo não numera) |
| `status` | str | `running`, `suspended`, `completed`, `cancelled` ou `failed` |
| `outcome` | str | `published` ou `withdrawn` quando concluído |
| `revision` | int | Revisão da instância |
| `created` | bool | Falso quando a chave de idempotência já existia, e nas consultas |

### `NoteSummary`
| Campo | Tipo | Significado |
|---|---|---|
| `sysid` | UUID | Identificador estável da nota |
| `code` | str | Código único entre notas vivas da organização |
| `title` | str | Título |
| `visibility` | str | `private`, `organization` ou `group` |
| `revision` | int | Revisão atual, a usar em `expected_revision` |
| `created_at` | datetime (UTC) | Criação |
| `updated_at` | datetime (UTC) | Última alteração |

`body` é campo sensível do `TableSpec` e não faz parte de nenhum DTO.

## Eventos publicados
Não se aplica: o exemplo não publica eventos de domínio.

## Histórico de versões
| Versão | Mudança |
|---|---|
| 0.3.0 | Primeira versão: `note_count`, `list_notes`, `get_note`, `create_note`, `update_note` e `NoteSummary` |
| 0.4.0 | `request_publication`, `publication_status` e `PublicationStatus`, sobre o fluxo de processo `example_notes.publication` (core com `skeleton.flows`) |
