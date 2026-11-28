# API de serviços: Livro contábil

## Visão geral
`LedgerService` é o contrato em processo do livro contábil. Outras apps o utilizam para gerar lançamentos a partir de seus registros, criar e baixar títulos e consultar saldos, sempre respeitando a autorização do ator no contexto. As telas do próprio livro consomem o mesmo serviço.

## Dependência
`app_id`: `org.skeleton.ledger`. Faixa recomendada: `{"org.skeleton.ledger": ">=0.10.0,<0.11.0"}` (a versão acompanha a do núcleo e a faixa é atualizada na release; a primeira versão publicada com o livro é a 0.1.0, veja [histórico](#histórico-de-versões)). Importe exclusivamente `skeleton.contrib.ledger.service_api`.

O livro contábil é uma biblioteca first-party: declarar a dependência basta para instalá-lo, ele não possui disponibilidade nem ativação próprias e fica efetivo em uma organização enquanto houver uma app ativa ali que dependa dele. Consulte o guia de consumo em `docs/extending.md` ("Depender do livro contábil").

## Serviços

### `LedgerService`
Todos os métodos exigem que o livro esteja efetivo na organização — isto é, que exista uma app ativa ali dependendo dele (caso contrário, `Unavailable`) —, que o contexto tenant tenha sido aberto pelo chamador e que `actor` coincida com o ator do contexto (`Forbidden`). Nenhum método faz commit nem dispara efeito externo.

Nos parâmetros abaixo, `book_id=None` significa usar o livro padrão da organização.

#### `books(*, actor) -> tuple[BookInfo, ...]`
Lista os livros que o ator pode ler, trazendo o livro padrão primeiro.
- **Autorização:** `TablePermission` de `read` em `skeleton_ledger.book` e visibilidade dos registros.
- **Erros:** `Forbidden` sem a permissão; `Unavailable`.
- **Efeitos:** nenhum.

#### `create_default_book(*, actor, basis="cash", currency="BRL") -> BookInfo`
Devolve o livro padrão da organização, criando-o automaticamente na primeira chamada.
- **Autorização:** `TablePermission` de `create` em `skeleton_ledger.book` quando precisar criar.
- **Erros:** `Forbidden` sem permissão de criação quando ainda não houver livro padrão; `NotFound` se o livro existir mas o ator não puder lê-lo; `InvalidInput` para regime ou moeda inválidos.
- **Efeitos:** cria o livro (com moeda e regime imutáveis) na transação do chamador.

#### Plano de contas (#543)
| Método | Autorização | Erros |
|---|---|---|
| `accounts(*, actor, book_id=None, cursor=None, page_size=25) -> Page[AccountInfo]` | Leitura do livro | `NotFound`; `InvalidInput` (cursor ou tamanho de página) |
| `apply_chart(*, actor, book_id=None, template="services") -> int` | Gestão do livro (`update`) | `NotFound`; `Forbidden`; `InvalidInput` (modelo desconhecido) |
| `create_account(*, actor, code, name, kind, book_id=None, parent_id=None, is_group=False) -> AccountInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("integrity_conflict")` (código repetido); `InvalidInput` (tipo inválido, ou conta pai de outro livro, de outro tipo ou que não é grupo) |
| `update_account(*, actor, account_id, expected_revision, name=None, is_active=None) -> AccountInfo` | Gestão do livro da conta | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `delete_account(*, actor, account_id, expected_revision) -> None` | Gestão do livro da conta | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("live_references")` |
| `account_keys(*, actor, book_id=None) -> tuple[AccountKeyInfo, ...]` | Leitura do livro (apenas chaves de apps ativas) | `NotFound` |
| `bind_key(*, actor, key, account_id, book_id=None) -> AccountKeyInfo` | Gestão do livro | `NotFound`; `Forbidden`; `InvalidInput` (chave desconhecida ou de app inativa; conta de outro livro, grupo, inativa ou de outro tipo) |
| `cost_centers(*, actor, book_id=None) -> tuple[CostCenterInfo, ...]` | Leitura do livro | `NotFound` |
| `create_cost_center(*, actor, code, name, book_id=None) -> CostCenterInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("integrity_conflict")` |

#### Lançamentos (#544)
| Método | Autorização | Erros |
|---|---|---|
| `post_manual(*, actor, lines, date, description, idempotency_key, book_id=None) -> EntryInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("period_locked")`; `Conflict("idempotency_conflict")`; `InvalidInput` (partidas que não fecham, apenas uma partida, conta-grupo, inativa ou de outro livro, ou data/chave inválida) |
| `reverse(*, actor, entry_id, date, reason="", idempotency_key="") -> EntryInfo` | Gestão do livro do lançamento (ou ação da regra na origem, se houver origem); aceita `system_context` | `NotFound`; `Forbidden`; `Conflict("already_reversed")`; `Conflict("period_locked")`; `Conflict("idempotency_conflict")`; `InvalidInput` (estorno de estorno) |
| `entry(*, actor, entry_id) -> EntryInfo` | Leitura do livro | `NotFound` |
| `entries(*, actor, start, end, book_id=None, cursor=None, page_size=25) -> Page[EntryInfo]` | Leitura do livro (janela de até 5 anos) | `NotFound`; `InvalidInput` |
| `set_lock(*, actor, locked_until, expected_revision, book_id=None) -> BookInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` |
| `post(*, actor, rule, amounts, date, description, source_id, idempotency_key, accounts=None, book_id=None, cost_center_id=None, party_ref="", party_kind="") -> EntryInfo` | Ação da regra no registro de origem (dispensa leitura do livro); aceita `system_context` | `NotFound` (origem ilegível, de outra organização ou sem livro); `Forbidden` (sem a ação na origem); `Unavailable` (app de origem desativada); `Conflict("account_not_bound")` com a chave; `Conflict("period_locked")`; `Conflict("idempotency_conflict")`; `InvalidInput` (regra, valores, `Param` ausente ou de outro livro, ou resultado desbalanceado) |
| `entries_for_source(*, actor, table_key, record_id) -> tuple[EntryInfo, ...]` | Leitura do registro de origem | `NotFound`; `InvalidInput` |

Detalhes adicionais sobre lançamentos e registro de regras:

- `reverse` também aceita `expected_revision` (CAS) e `system_context`; um lançamento vinculado a uma origem é estornado com a mesma ação da regra na origem.
- Em `post`, `amounts` mapeia o nome de cada componente a um inteiro na menor unidade da moeda, e `accounts` mapeia o nome de cada `Param` ao `sysid` de uma conta do livro.
- Em `post_manual`, `lines` é uma tupla de `PostingLine(account_id, debit=0, credit=0, memo="", cost_center_id=None, party_ref="", party_kind="")`, reexportado pelo módulo. A contraparte é identificada apenas por `party_ref` (referência opaca), nunca por nome.
- **Origem exposta ou derivada:** a origem de uma regra de lançamento ou de um título pode ser uma tabela exposta ou uma [tabela derivada](../../../../docs/access-control.md#tabelas-derivadas) do núcleo — a ação pedida no registro filho é decidida no registro âncora (`update` para lançar e criar título, `read` para `entries_for_source` e `titles_for_source`).
- **Registro em código:** `skeleton.contrib.ledger.service_api` reexporta `AccountKey`, `register_account_key`, `PostingRule`, `RuleLine`, `Key`, `Param` e `register_posting_rule`. A app chama `register_account_key(AccountKey(code="acme_services.service_revenue", label=..., kind="revenue"))` no `ready()`; código fora do formato ou duplicado lança `ValueError`.

#### Títulos e baixas (#546)
| Método | Autorização | Erros |
|---|---|---|
| `create_title(*, actor, direction, amount, due_date, description, category_key, idempotency_key, issue_date=None, counterparty_name="", party_ref="", party_kind="", source_table=None, source_id=None, cost_center_id=None, book_id=None) -> TitleInfo` | Com origem: `update` no registro de origem; sem origem: gestão do livro; aceita `system_context` | `NotFound`; `Forbidden`; `Unavailable`; `Conflict("account_not_bound")`; `Conflict("idempotency_conflict")`; `InvalidInput` |
| `create_installments(*, actor, direction, amount, count, first_due_date, description, category_key, idempotency_key, ...) -> tuple[TitleInfo, ...]` | A mesma de `create_title` | Os de `create_title`; `InvalidInput` (`count` fora de 2 a 120) |
| `settle_title(*, actor, title_id, amount, date, payment_account_id, method, expected_revision, idempotency_key, discount=0, interest=0, fee=0) -> SettlementInfo` | A mesma do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("title_settled")`; `Conflict("period_locked")`; `Conflict("account_not_bound")`; `InvalidInput` |
| `reverse_settlement(*, actor, settlement_id, date, expected_revision, reason="") -> TitleInfo` | A mesma do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("already_reversed")`; `Conflict("period_locked")` |
| `cancel_title(*, actor, title_id, expected_revision, reason="", date=None) -> TitleInfo` | A mesma do título | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `Conflict("title_has_settlements")` |
| `titles(*, actor, book_id=None, direction=None, status=None, due_until=None, cursor=None, page_size=25) -> Page[TitleInfo]` | Leitura do livro | `NotFound`; `InvalidInput` |
| `titles_for_source(*, actor, table_key, record_id) -> tuple[TitleInfo, ...]` | Leitura do registro de origem; aceita `system_context` (enquanto o registro estiver vivo na organização do contexto) | `NotFound` |
| `settlements(*, actor, title_id) -> tuple[SettlementInfo, ...]` | Leitura do livro ou da origem; aceita `system_context` | `NotFound` |

`title(*, actor, title_id) -> TitleInfo` devolve um único título com a mesma autorização de `settlements` e também aceita `system_context`.

**Uso em `system_context` (consumidor de evento ou job por organização).** As escritas de título (`create_title`, `create_installments`, `settle_title`, `reverse_settlement` e `cancel_title`) e as leituras `title`, `titles_for_source` e `settlements` aceitam `actor=None` dentro do `system_context` da organização. A leitura segue a mesma regra da escrita: títulos com origem ficam visíveis apenas enquanto o registro de origem estiver vivo na organização do contexto, e títulos sem origem ficam acessíveis dentro da organização (isolados pela RLS). Isso permite que um consumidor consulte a `revision` exigida por `cancel_title` e `settle_title` e verifique se a origem já possui título antes de criá-lo. `titles`, os relatórios e a gestão do livro continuam exigindo ator. Veja o padrão completo em [eventos](../../../../docs/events.md#reagir-numerando-e-abrindo-um-título).

#### Títulos recorrentes (#547)
| Método | Autorização | Erros |
|---|---|---|
| `create_recurrence(*, actor, direction, amount, description, category_key, frequency, start_date, idempotency_key, interval=1, day_of_month=None, end_date=None, max_occurrences=None, days_ahead=30, counterparty_name="", party_ref="", party_kind="", cost_center_id=None, book_id=None) -> RecurrenceInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("account_not_bound")`; `Conflict("idempotency_conflict")`; `InvalidInput` |
| `update_recurrence(*, actor, recurrence_id, data, expected_revision) -> RecurrenceInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")`; `InvalidInput` (campo fora de `amount`, `description`, `counterparty_name`, `party_ref`, `party_kind`, `cost_center_id`, `end_date`, `max_occurrences` e `days_ahead`) |
| `pause(*, actor, recurrence_id, expected_revision) -> RecurrenceInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `resume(*, actor, recurrence_id, expected_revision) -> RecurrenceInfo` | Gestão do livro | `NotFound`; `Forbidden`; `Conflict("stale_revision")` |
| `recurrences(*, actor, book_id=None) -> tuple[RecurrenceInfo, ...]` | Leitura do livro | `NotFound` |

Os títulos são gerados pelo job diário `ledger.recurrences`, não diretamente por esses métodos.

**Idempotência.** Um replay por `idempotency_key` devolve o objeto existente apenas quando toda a tupla `(organização, source_app, book_id, source_table, source_id, kind/reverses_id, idempotency_key)` coincide e o ator continua autorizado a ler o objeto. Reaproveitar uma chave em outro livro, outra origem, outro tipo ou sem permissão de leitura responde `Conflict("idempotency_conflict")` de forma opaca, sem expor dados do registro existente e sem criar nova linha.

#### Saldos e relatórios (#548)
Todos os métodos abaixo exigem leitura do livro, são somente leitura e consideram exclusivamente lançamentos selados. Retornam `NotFound` para livro ou conta ilegível e `InvalidInput` para janela acima de cinco anos, data, cursor ou página inválidos.

| Método | Notas |
|---|---|
| `balance(*, actor, as_of, account_id=None, account_key=None, book_id=None) -> BalanceInfo` | Exige exatamente um entre `account_id` e `account_key`; retorna `Conflict("account_not_bound")` se a chave não tiver vínculo. |
| `trial_balance(*, actor, start, end, book_id=None) -> TrialBalanceInfo` | Lista todas as contas, com contas-grupo somando suas descendentes. |
| `income_statement(*, actor, start, end, book_id=None) -> IncomeStatementInfo` | Receitas menos despesas do período. |
| `account_statement(*, actor, account_id, start, end, cursor=None, page_size=25, book_id=None) -> StatementInfo` | Razão paginado, com saldo corrente em cada linha. |
| `cash_flow(*, actor, start, end, book_id=None) -> CashFlowInfo` | Realizado e previsto, agrupado por mês. |
| `aging(*, actor, as_of, direction="receivable", book_id=None) -> AgingInfo` | Faixas `current`, `1-30`, `31-60`, `61-90` e `90+`. |

## DTOs
- `BookInfo`: `sysid`, `name`, `currency`, `basis`, `is_default`, `locked_until`, `revision` e `writable` (indica se o ator pode gerir o livro).
- `AccountInfo`: `sysid`, `book_id`, `code`, `name`, `kind`, `parent_id`, `is_group`, `is_active`, `revision`.
- `AccountKeyInfo`: `code`, `label`, `kind`, `account_id` (`None` quando sem vínculo).
- `CostCenterInfo`: `sysid`, `book_id`, `code`, `name`, `is_active`, `revision`.
- `TitleInfo`: `sysid`, `book_id`, `direction`, `description`, `counterparty_name`, `party_ref`, `party_kind`, `category_account_id`, `cost_center_id`, `amount`, `amount_settled`, `balance`, `issue_date`, `due_date`, `status`, `source_table`, `source_id`, `installment_number`, `installment_count`, `revision`. O campo `counterparty_name` é exposto a quem lê o livro e, em `titles_for_source`, a quem lê a origem.
- `SettlementInfo`: `sysid`, `title_id`, `date`, `amount`, `discount`, `interest`, `fee`, `payment_account_id`, `method`, `entry_id`, `reversal_entry_id`, `revision`.
- `RecurrenceInfo`: `sysid`, `book_id`, `direction`, `description`, `counterparty_name`, `category_account_id`, `cost_center_id`, `amount`, `frequency`, `interval`, `day_of_month`, `start_date`, `end_date`, `max_occurrences`, `next_due` (`None` quando encerrada), `generated_count`, `days_ahead`, `is_active`, `ended`, `revision`.
- `BalanceInfo`: `account_id`, `code`, `name`, `kind`, `is_group`, `as_of`, `debit`, `credit`, `balance` (com sinal natural da conta).
- `ReportRow`: `account_id`, `code`, `name`, `kind`, `parent_id`, `is_group`, `opening`, `debit`, `credit`, `closing`; `TrialBalanceInfo`: `start`, `end`, `rows`, `total_debit`, `total_credit`; `IncomeStatementInfo`: `start`, `end`, `revenue`, `expense`, `result`, `rows` (com o movimento do período em `closing`).
- `StatementInfo`: `account_id`, `start`, `end`, `opening`, `page` (`Page[StatementLine]`: `line_id`, `entry_id`, `date`, `year`, `number`, `description`, `memo`, `debit`, `credit`, `balance`).
- `CashFlowInfo`: `start`, `end`, `opening`, `realized_in`, `realized_out`, `closing`, `forecast_in`, `forecast_out`, `months` (`CashFlowMonth`: `month`, `realized_in`, `realized_out`, `forecast_in`, `forecast_out`).
- `AgingInfo`: `as_of`, `direction`, `total`, `buckets` (`AgingBucket`: `name`, `amount`, `count`).
- `EntryInfo`: `sysid`, `book_id`, `year`, `number`, `date`, `description`, `kind`, `rule`, `source_app`, `source_table`, `source_id`, `reverses_id`, `reversed_by_id`, `total`, `sealed`, `digest`, `revision` (valor esperado em `reverse`) e `lines` (`EntryLineInfo`: `sysid`, `account_id`, `debit`, `credit`, `memo`, `cost_center_id`, `party_ref`, `party_kind`).

## Testes da app consumidora
O kit `skeleton.contrib.ledger.testing` fornece `enable_ledger(organization)`, `prepare_book(actor, basis=..., bindings=...)` e `account_id(actor, code)`.

O conjunto mínimo de verificações para uma app que lança no livro deve comprovar que:

- a regra registrada passa no check `skeleton.E041`;
- o ator com a ação exigida na origem consegue lançar, e o lançamento aparece em `entries_for_source`;
- o ator sem essa ação recebe `Forbidden` (se a origem for legível) ou `NotFound`;
- repetir a mesma `idempotency_key` devolve o mesmo lançamento;
- a organização B não enxerga lançamentos da organização A;
- com a app consumidora desativada (o livro é uma biblioteca e a acompanha), as chamadas respondem `Unavailable`.

O cenário completo de referência está em `tests/contrib/ledger/test_scenario.py`.

## Histórico de versões
- **0.7.0:** o livro contábil passa a ser uma biblioteca first-party — os métodos exigem uma app ativa na organização que dependa dele, em vez de ativação própria, e `testing.enable_ledger` passa a conceder apenas as ações nas tabelas; contratos e DTOs permanecem iguais.
- **0.2.0:** `title`, `titles_for_source` e `settlements` passam a aceitar `system_context` (`actor=None`), acompanhando as escritas de título.
- **0.1.0 (primeira versão do núcleo):** `books` e `create_default_book`; plano de contas, chaves e centros de custo; lançamentos selados, estorno e trava de período; regras de lançamento autorizadas pela origem (`post`, `entries_for_source` e estorno pela origem); títulos, parcelas e baixas; títulos recorrentes e job `ledger.recurrences`; saldos e relatórios; `title` e `EntryInfo.revision` para suporte às telas.
