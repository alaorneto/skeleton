# Livro contábil

O livro contábil (`skeleton_ledger`, `org.skeleton.ledger`) é uma app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)): o operador a instala com `SKELETON_BUNDLED_APPS=ledger`; a plataforma a disponibiliza e cada organização a ativa, como qualquer app. Ela não se mistura com a cobrança da plataforma (`skeleton/billing`, que cobra as organizações): nunca importa `skeleton.billing`, e o dinheiro vem de `skeleton.common.money` (inteiros da menor unidade da moeda, nunca `float`).

## Contract

### Livros (#542)
`Book` (`SecuredRecord`) pertence a uma organização: `name` (até 150), `currency` (ISO 4217 da tabela de `skeleton.common.money`, imutável), `basis` (`cash`, caixa, ou `accrual`, competência; imutável), `is_default` e `locked_until` (trava de período). Uma organização pode ter vários livros (por exemplo, pessoa física e pessoa jurídica separadas) e tem **um** livro padrão vivo, garantido pela restrição única parcial; duas criações simultâneas deixam um só.

- Ler um livro, e tudo o que é dele, exige `read` no `Book`; gerir (contas, vínculos, lançamentos manuais, trava) exige `update` nele: o dono, o administrador da organização ou uma concessão de registro.
- Sem `TablePermission` de leitura em `skeleton_ledger.book`, a app não existe para o membro (404). A ação do administrador `member-access` concede leitura ao papel `member`.
- O gateway genérico da tabela (`/api/v1/o/<org>/tables/skeleton_ledger.book/records/`) só escreve `name`; moeda, regime e trava mudam só pelos serviços da app (`RecordOperations`). O livro padrão não é excluído.
- Métrica de estado de plano `skeleton_ledger.books` (livros vivos da organização).

## Acceptance
- `SKELETON_BUNDLED_APPS=ledger` instala a app; sem a variável, nada do ledger é importado.
- Um livro padrão por organização, inclusive com criação concorrente; moeda e regime imutáveis no banco; moeda fora da tabela recusada.
- A/B, app desativada e default deny em todos os métodos de serviço.
