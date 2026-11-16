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

| `post(*, actor, rule, amounts, date, description, source_id, idempotency_key, accounts=None, book_id=None, cost_center_id=None, party_ref="", party_kind="") -> EntryInfo` | a ação da regra no registro de origem (sem leitura do livro); `system_context` aceito | `NotFound` (origem ilegível ou de outra organização, sem livro); `Forbidden` (sem a ação na origem); `Unavailable` (app da origem desativada); `Conflict("account_not_bound")` com a chave; `Conflict("period_locked")`; `InvalidInput` (regra, valores, `Param` ausente ou de outro livro, resultado desbalanceado) |
| `entries_for_source(*, actor, table_key, record_id) -> tuple[EntryInfo, ...]` | leitura do registro de origem | `NotFound`; `InvalidInput` |

#### Títulos e baixas (#546)
| Método | Autorização | Erros |
|---|---|---|
| `create_title(*, actor, direction, amount, due_date, description, category_key, idempotency_key, issue_date=None, counterparty_name="", party_ref="", party_kind="", source_table=None, source_id=None, cost_center_id=None, book_id=None) -> TitleInfo` | com origem, `update` no registro de origem; sem origem, gestão do livro; `system_context` aceito | `NotFound`; `Forbidden`; `Unavailable`; `Conflict("account_not_bound")`; `InvalidInput` |
| `create_installments(*, actor, direction, amount, count, first_due_date, description, category_key, idempotency_key, ...) -> tuple[TitleInfo, ...]` | a de `create_title` | os de `create_title`; `InvalidInput` (`count` fora de 2 a 120) |
| `settle_title(*, actor, title_id, amount, date, payment_account_id, method, expected_revision, idempotency_key, discount=0, interest=0, fee=0) -> SettlementInfo` | a do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("title_settled")`; `Conflict("period_locked")`; `Conflict("account_not_bound")`; `InvalidInput` |
| `reverse_settlement(*, actor, settlement_id, date, expected_revision, reason="") -> TitleInfo` | a do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("already_reversed")`; `Conflict("period_locked")` |
| `cancel_title(*, actor, title_id, expected_revision, reason="", date=None) -> TitleInfo` | a do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("title_has_settlements")` |
| `titles(*, actor, book_id=None, direction=None, status=None, due_until=None, cursor=None, page_size=25) -> Page[TitleInfo]` | leitura do livro | `NotFound`; `InvalidInput` |
| `titles_for_source(*, actor, table_key, record_id) -> tuple[TitleInfo, ...]` | leitura do registro de origem | `NotFound` |
| `settlements(*, actor, title_id) -> tuple[SettlementInfo, ...]` | leitura do livro ou da origem | `NotFound` |

#### Títulos recorrentes (#547)
| Método | Autorização | Erros |
|---|---|---|
| `create_recurrence(*, actor, direction, amount, description, category_key, frequency, start_date, idempotency_key, interval=1, day_of_month=None, end_date=None, max_occurrences=None, days_ahead=30, counterparty_name="", party_ref="", party_kind="", cost_center_id=None, book_id=None) -> RecurrenceInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("account_not_bound")`; `InvalidInput` |
| `update_recurrence(*, actor, recurrence_id, data, expected_revision) -> RecurrenceInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` (campo fora de `amount`, `description`, `counterparty_name`, `party_ref`, `party_kind`, `cost_center_id`, `end_date`, `max_occurrences`, `days_ahead`) |
| `pause(*, actor, recurrence_id, expected_revision) -> RecurrenceInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `resume(*, actor, recurrence_id, expected_revision) -> RecurrenceInfo` | gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `recurrences(*, actor, book_id=None) -> tuple[RecurrenceInfo, ...]` | leitura do livro | `NotFound` |

Os títulos são gerados pelo job diário `ledger.recurrences`, não pelos métodos.

#### Saldos e relatórios (#548)
Todos com leitura do livro, só leitura, só lançamentos selados; `NotFound` para livro ou conta ilegível; `InvalidInput` para janela (até cinco anos), data, cursor ou página inválidos.

| Método | Notas |
|---|---|
| `balance(*, actor, as_of, account_id=None, account_key=None, book_id=None) -> BalanceInfo` | exatamente um de `account_id` e `account_key`; `Conflict("account_not_bound")` com chave sem vínculo |
| `trial_balance(*, actor, start, end, book_id=None) -> TrialBalanceInfo` | todas as contas, grupos somando descendentes |
| `income_statement(*, actor, start, end, book_id=None) -> IncomeStatementInfo` | receitas menos despesas |
| `account_statement(*, actor, account_id, start, end, cursor=None, page_size=25, book_id=None) -> StatementInfo` | paginado, com saldo corrente |
| `cash_flow(*, actor, start, end, book_id=None) -> CashFlowInfo` | realizado e previsto, por mês |
| `aging(*, actor, as_of, direction="receivable", book_id=None) -> AgingInfo` | faixas `current`, `1-30`, `31-60`, `61-90`, `90+` |

`reverse` também aceita `expected_revision` (CAS) e `system_context`; um lançamento com origem é estornado com a ação da regra na origem. `amounts` mapeia o nome de cada componente a um inteiro da menor unidade da moeda; `accounts` mapeia o nome de cada `Param` ao `sysid` de uma conta do livro.

`lines` é uma tupla de `PostingLine(account_id, debit=0, credit=0, memo="", cost_center_id=None, party_ref="", party_kind="")`, reexportado pelo módulo. A contraparte é identificada só por `party_ref` (referência opaca), nunca por nome.

**Registro em código.** `skeleton.contrib.ledger.service_api` reexporta `AccountKey`, `register_account_key`, `PostingRule`, `RuleLine`, `Key`, `Param` e `register_posting_rule`: a app chama `register_account_key(AccountKey(code="acme_services.service_revenue", label=..., kind="revenue"))` no `ready()`. Código fora do formato ou duplicado é `ValueError`.

## DTOs
- `BookInfo`: `sysid`, `name`, `currency`, `basis`, `is_default`, `locked_until`, `revision` e `writable` (se o ator gere o livro).
- `AccountInfo`: `sysid`, `book_id`, `code`, `name`, `kind`, `parent_id`, `is_group`, `is_active`, `revision`.
- `AccountKeyInfo`: `code`, `label`, `kind`, `account_id` (`None` sem vínculo).
- `CostCenterInfo`: `sysid`, `book_id`, `code`, `name`, `is_active`, `revision`.
- `TitleInfo`: `sysid`, `book_id`, `direction`, `description`, `counterparty_name`, `party_ref`, `party_kind`, `category_account_id`, `cost_center_id`, `amount`, `amount_settled`, `balance`, `issue_date`, `due_date`, `status`, `source_table`, `source_id`, `installment_number`, `installment_count`, `revision`. `counterparty_name` sai para quem lê o livro e, em `titles_for_source`, para quem lê a origem.
- `SettlementInfo`: `sysid`, `title_id`, `date`, `amount`, `discount`, `interest`, `fee`, `payment_account_id`, `method`, `entry_id`, `reversal_entry_id`, `revision`.
- `RecurrenceInfo`: `sysid`, `book_id`, `direction`, `description`, `counterparty_name`, `category_account_id`, `cost_center_id`, `amount`, `frequency`, `interval`, `day_of_month`, `start_date`, `end_date`, `max_occurrences`, `next_due` (`None` quando encerrada), `generated_count`, `days_ahead`, `is_active`, `ended`, `revision`.
- `BalanceInfo`: `account_id`, `code`, `name`, `kind`, `is_group`, `as_of`, `debit`, `credit`, `balance` (sinal natural).
- `ReportRow`: `account_id`, `code`, `name`, `kind`, `parent_id`, `is_group`, `opening`, `debit`, `credit`, `closing`; `TrialBalanceInfo`: `start`, `end`, `rows`, `total_debit`, `total_credit`; `IncomeStatementInfo`: `start`, `end`, `revenue`, `expense`, `result`, `rows` (o movimento em `closing`).
- `StatementInfo`: `account_id`, `start`, `end`, `opening`, `page` (`Page[StatementLine]`: `line_id`, `entry_id`, `date`, `year`, `number`, `description`, `memo`, `debit`, `credit`, `balance`).
- `CashFlowInfo`: `start`, `end`, `opening`, `realized_in`, `realized_out`, `closing`, `forecast_in`, `forecast_out`, `months` (`CashFlowMonth`: `month`, `realized_in`, `realized_out`, `forecast_in`, `forecast_out`).
- `AgingInfo`: `as_of`, `direction`, `total`, `buckets` (`AgingBucket`: `name`, `amount`, `count`).
- `EntryInfo`: `sysid`, `book_id`, `year`, `number`, `date`, `description`, `kind`, `rule`, `source_app`, `source_table`, `source_id`, `reverses_id`, `reversed_by_id`, `total`, `sealed`, `digest`, `revision` (a esperada por `reverse`) e `lines` (`EntryLineInfo`: `sysid`, `account_id`, `debit`, `credit`, `memo`, `cost_center_id`, `party_ref`, `party_kind`).

## Histórico de versões
- 0.6.0 (próxima *minor*, com o núcleo): primeira versão (#542): `books`, `create_default_book`; plano de contas, chaves e centros de custo (#543); lançamentos selados, estorno e trava (#544); regras de lançamento autorizadas pela origem, `post`, `entries_for_source` e o estorno pela origem (#545); títulos, parcelas e baixas (#546); títulos recorrentes e o job `ledger.recurrences` (#547); saldos e relatórios (#548).
