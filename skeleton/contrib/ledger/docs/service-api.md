# API de serviços: Livro contábil

## Visão geral
`LedgerService` é o contrato em processo do livro contábil. Outras apps o usam para lançar pelos registros delas, criar e baixar títulos e consultar saldos, sempre com a autorização do ator que está no contexto. As páginas do próprio livro usam o mesmo serviço.

## Dependência
`app_id`: `org.skeleton.ledger`. Faixa recomendada: `{"org.skeleton.ledger": ">=0.5.0,<0.6.0"}` (a versão é a do núcleo). Importe só `skeleton.contrib.ledger.service_api`.

## Serviços

### `LedgerService`
Todos os métodos exigem a app ativa na organização (`Unavailable`), o contexto tenant aberto pelo chamador e `actor` igual ao ator do contexto (`Forbidden`). Nenhum faz commit nem executa efeito externo. "Livro opcional" significa: sem `book_id`, o livro padrão da organização.

#### `books(*, actor) -> tuple[BookInfo, ...]`
Livros que o ator lê, o padrão primeiro.
- **Autorização:** TablePermission read em `skeleton_ledger.book` e visibilidade dos registros.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** nenhum.

#### `create_default_book(*, actor, basis="cash", currency="BRL") -> BookInfo`
O livro padrão da organização, criado na primeira chamada.
- **Autorização:** TablePermission create em `skeleton_ledger.book` para criar.
- **Erros:** `Forbidden` sem a permissão de criar quando ainda não há livro padrão; `NotFound` se ele existe mas o ator não o lê; `InvalidInput` para regime ou moeda inválidos.
- **Efeitos:** cria o livro (moeda e regime imutáveis) na transação do chamador.

#### Plano de contas (#543)
| Método | Autorização | Erros |
|---|---|---|
| `accounts(*, actor, book_id=None, cursor=None, page_size=25) -> Page[AccountInfo]` | leitura do livro | `NotFound`; `InvalidInput` (cursor ou página) |
| `apply_chart(*, actor, book_id=None, template="services") -> int` | gestão do livro (update) | `NotFound`; `Forbidden`; `InvalidInput` (modelo desconhecido) |
| `create_account(*, actor, code, name, kind, book_id=None, parent_id=None, is_group=False) -> AccountInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("integrity_conflict")` (código repetido); `InvalidInput` (tipo, pai de outro livro, de outro tipo ou que não é grupo) |
| `update_account(*, actor, account_id, expected_revision, name=None, is_active=None) -> AccountInfo` | gestão do livro da conta | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `delete_account(*, actor, account_id, expected_revision) -> None` | gestão do livro da conta | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("live_references")` |
| `account_keys(*, actor, book_id=None) -> tuple[AccountKeyInfo, ...]` | leitura do livro; só chaves de apps ativas | `NotFound` |
| `bind_key(*, actor, key, account_id, book_id=None) -> AccountKeyInfo` | gestão do livro | `NotFound`; `Forbidden`; `InvalidInput` (chave desconhecida ou de app inativa; conta de outro livro, grupo, inativa ou de outro tipo) |
| `cost_centers(*, actor, book_id=None) -> tuple[CostCenterInfo, ...]` | leitura do livro | `NotFound` |
| `create_cost_center(*, actor, code, name, book_id=None) -> CostCenterInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("integrity_conflict")` |

#### Lançamentos (#544)
| Método | Autorização | Erros |
|---|---|---|
| `post_manual(*, actor, lines, date, description, idempotency_key, book_id=None) -> EntryInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("period_locked")`; `InvalidInput` (partidas que não fecham, uma só, conta-grupo, inativa ou de outro livro, data ou chave inválidas) |
| `reverse(*, actor, entry_id, date, reason="", idempotency_key="") -> EntryInfo` | gestão do livro do lançamento | `NotFound`; `Forbidden`; `Conflict("already_reversed")`; `Conflict("period_locked")`; `InvalidInput` (estorno de estorno) |
| `entry(*, actor, entry_id) -> EntryInfo` | leitura do livro | `NotFound` |
| `entries(*, actor, start, end, book_id=None, cursor=None, page_size=25) -> Page[EntryInfo]` | leitura do livro; até cinco anos | `NotFound`; `InvalidInput` |
| `set_lock(*, actor, locked_until, expected_revision, book_id=None) -> BookInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` |

`lines` é uma tupla de `PostingLine(account_id, debit=0, credit=0, memo="", cost_center_id=None, party_ref="", party_kind="")`, reexportado pelo módulo. A contraparte é identificada só por `party_ref` (referência opaca), nunca por nome.

**Registro em código.** `skeleton.contrib.ledger.service_api` reexporta `AccountKey` e `register_account_key`: a app chama `register_account_key(AccountKey(code="acme_services.service_revenue", label=..., kind="revenue"))` no `ready()`. Código fora do formato ou duplicado é `ValueError`.

## DTOs
- `BookInfo`: `sysid`, `name`, `currency`, `basis`, `is_default`, `locked_until`, `revision` e `writable` (se o ator gere o livro).
- `AccountInfo`: `sysid`, `book_id`, `code`, `name`, `kind`, `parent_id`, `is_group`, `is_active`, `revision`.
- `AccountKeyInfo`: `code`, `label`, `kind`, `account_id` (`None` sem vínculo).
- `CostCenterInfo`: `sysid`, `book_id`, `code`, `name`, `is_active`, `revision`.
- `EntryInfo`: `sysid`, `book_id`, `year`, `number`, `date`, `description`, `kind`, `rule`, `source_app`, `source_table`, `source_id`, `reverses_id`, `reversed_by_id`, `total`, `sealed`, `digest` e `lines` (`EntryLineInfo`: `sysid`, `account_id`, `debit`, `credit`, `memo`, `cost_center_id`, `party_ref`, `party_kind`).

## Histórico de versões
- 0.6.0 (próxima *minor*, com o núcleo): primeira versão (#542): `books`, `create_default_book`; plano de contas, chaves e centros de custo (#543); lançamentos selados, estorno e trava (#544).
