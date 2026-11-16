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

## Acceptance
- `SKELETON_BUNDLED_APPS=ledger` instala a app; sem a variável, nada do ledger é importado.
- Um livro padrão por organização, inclusive com criação concorrente; moeda e regime imutáveis no banco; moeda fora da tabela recusada.
- A/B, app desativada e default deny em todos os métodos de serviço.
