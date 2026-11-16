# Livro contábil

O livro contábil (`skeleton_ledger`, `org.skeleton.ledger`) é uma app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)): o operador a instala com `SKELETON_BUNDLED_APPS=ledger`; a plataforma a disponibiliza e cada organização a ativa, como qualquer app. Ela não se mistura com a cobrança da plataforma (`skeleton/billing`, que cobra as organizações): nunca importa `skeleton.billing`, e o dinheiro vem de `skeleton.common.money` (inteiros da menor unidade da moeda, nunca `float`).

## Contract

### Livros (#542)
`Book` (`SecuredRecord`) pertence a uma organização: `name` (até 150), `currency` (ISO 4217 da tabela de `skeleton.common.money`, imutável), `basis` (`cash`, caixa, ou `accrual`, competência; imutável), `is_default` e `locked_until` (trava de período). Uma organização pode ter vários livros (por exemplo, pessoa física e pessoa jurídica separadas) e tem **um** livro padrão vivo, garantido pela restrição única parcial; duas criações simultâneas deixam um só.

- Ler um livro, e tudo o que é dele, exige `read` no `Book`; gerir (contas, vínculos, lançamentos manuais, trava) exige `update` nele: o dono, o administrador da organização ou uma concessão de registro.
- Sem `TablePermission` de leitura em `skeleton_ledger.book`, a app não existe para o membro (404). A ação do administrador `member-access` concede leitura ao papel `member`.
- O gateway genérico da tabela (`/api/v1/o/<org>/tables/skeleton_ledger.book/records/`) só escreve `name`; moeda, regime e trava mudam só pelos serviços da app (`RecordOperations`). O livro padrão não é excluído.
- Métrica de estado de plano `skeleton_ledger.books` (livros vivos da organização).

### Plano de contas (#543)
`Account` (do livro): `code` (único entre as contas vivas do livro), `name`, `kind` (`asset`, `liability`, `equity`, `revenue`, `expense`), `parent`, `is_group` e `is_active`. `code`, `kind`, `parent` e `is_group` são imutáveis (guard); `name` e `is_active` mudam com CAS. O pai é uma conta-grupo do **mesmo livro e do mesmo tipo**: o modelo recusa, e o banco garante livro e tipo por uma FK composta `(book_id, kind, parent_id)`. Uma conta-grupo soma os filhos e não recebe partida. Conta com filhos, vínculos ou partidas não é excluída (referência viva).

**Chaves de conta.** Uma chave (`AccountKey`, `<app_label>.<nome>`, com o tipo de conta que ela exige) é registrada em código por `register_account_key` no `ready()`; `AccountBinding` diz qual conta analítica, ativa, do livro e do tipo da chave cada chave usa (FK composta `(book_id, account_id)` no banco). A app base registra `ledger.cash`, `ledger.bank`, `ledger.receivables`, `ledger.payables`, `ledger.revenue`, `ledger.expenses`, `ledger.discounts_given`, `ledger.discounts_received`, `ledger.interest_income`, `ledger.interest_expense`, `ledger.fees` e `ledger.opening_balance`. A lista de chaves mostra só as de apps ativas na organização.

**Modelo inicial.** `apply_chart` aplica o modelo "Prestação de serviços" (`chart.py`): ativo, passivo, patrimônio, receitas e despesas, com as contas que as chaves base usam e os vínculos delas; aplicar de novo não duplica nada. `CostCenter` (`code` único vivo por livro, `name`, `is_active`) completa o plano.

### Lançamentos (#544)
`Entry` é um registro **selado** (`SealableMixin`, [modelo de dados](../../../../docs/data-model.md#registros-selados-526)) e `EntryLine` é filha dele (`seal_parent = "entry"`): o lançamento nasce, recebe as partidas e é selado na mesma transação, e depois nada dele muda, por modelo ou por SQL. Correção é só por **estorno**: um lançamento novo com débitos e créditos trocados, ligado por `reverses` (um estorno por lançamento, restrição única; um estorno não é estornável).

- Cada partida tem exatamente um débito ou um crédito, inteiros da menor unidade da moeda. O serviço confere e o banco prova de novo na transição do selo (`seal_condition_sql` congelada na migration): pelo menos duas partidas vivas, débitos = créditos = `total`, toda conta analítica, ativa e do livro, toda partida na data do lançamento e a data depois de `locked_until`.
- Numeração sem lacunas por livro e ano (`year`, `number`, únicos vivos): a linha do livro é travada enquanto o próximo número é tomado, então um rollback devolve o número. (A API de sequências do núcleo numera por organização, não por livro; por isso o livro usa a própria trava.)
- `set_lock` move a trava de período com CAS; lançar na data da trava ou antes é `Conflict("period_locked")`.
- `idempotency_key` única viva por `(organização, source_app, chave)`: repetir devolve o mesmo lançamento.
- Saldos, razões e relatórios só consideram lançamentos selados.

## Acceptance
- `SKELETON_BUNDLED_APPS=ledger` instala a app; sem a variável, nada do ledger é importado.
- Um livro padrão por organização, inclusive com criação concorrente; moeda e regime imutáveis no banco; moeda fora da tabela recusada.
- A/B, app desativada e default deny em todos os métodos de serviço.
