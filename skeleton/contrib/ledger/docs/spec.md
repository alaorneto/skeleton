# Livro contábil

O livro contábil (`skeleton_ledger`, `org.skeleton.ledger`) é uma app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)): é uma **biblioteca** (`kind: "bundled_library"`, #792): é instalada com a app que depende dela (#793) ou com `SKELETON_BUNDLED_APPS=ledger`, não tem disponibilidade nem ativação próprias e fica efetiva numa organização enquanto uma app ativa ali a usa. Ela não se mistura com a cobrança da plataforma (`skeleton/billing`, que cobra as organizações): nunca importa `skeleton.billing`, e o dinheiro vem de `skeleton.common.money` (inteiros da menor unidade da moeda, nunca `float`).

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

### Regras de lançamento (#545)
Uma app lança pelos registros dela com uma **regra declarada em código** (`PostingRule`, registrada por `register_posting_rule` no `ready()`; nada de `eval`, expressão ou nome de classe vindo de dado). A regra diz a tabela e a ação de origem (`source_table_key`, da própria app, e `source_action`) e as linhas: cada `RuleLine` tem o lado, a conta (`Key(código)`, resolvida pelo vínculo do livro, ou `Param(nome)`, passada na chamada) e o nome do componente de valor. Um componente zerado elimina as linhas dele; o resultado precisa fechar. O check E041 recusa uma regra cuja tabela de origem não está registrada ou não é uma tabela de negócio exposta nem uma tabela derivada (`TableSpec.access_parent`, autorizada pelo registro âncora, #565).

**A autorização vem do registro de origem**: quem tem a ação da regra no atendimento (por exemplo, `update`) gera o lançamento sem ler o livro, e lê os lançamentos daquele registro por `entries_for_source`. O estorno de um lançamento com origem também é autorizado pela origem. A descrição de um lançamento automático cita o número do documento de origem, nunca o nome de uma pessoa; a contraparte vai só como `party_ref` opaco. O kit `skeleton.contrib.ledger.testing` (`enable_ledger`, `prepare_book`, `account_id`) monta o livro, o plano e os vínculos para os testes de quem consome.

### Títulos e baixas (#546)
`Title` é um valor a receber (`receivable`) ou a pagar (`payable`) de um livro: `amount` (principal, > 0), `amount_settled` (0 ≤ baixado ≤ valor), `status` (`open`, `partial`, `settled`, `cancelled`, coerente com os valores por restrição no banco), datas de emissão e vencimento, a conta de categoria (resolvida pela chave `category_key`), o centro de custo, a origem opcional e as parcelas. `counterparty_name` é dado pessoal (`personal_fields`): legível por quem lê o livro e por quem lê a origem, `[REDACTED]` na auditoria. `Settlement` é uma baixa (data, `amount` baixado, `discount`, `interest`, `fee`, conta de pagamento, forma) com o lançamento que ela gerou; ela é estornada, nunca editada.

| Evento | Regime de caixa | Regime de competência |
|---|---|---|
| Criar título a receber | nada | D `ledger.receivables` / C categoria |
| Receber | D conta de pagamento (`amount + interest - discount - fee`); D `discounts_given`; D `fees`; C categoria (`amount`); C `interest_income` | igual, com C `ledger.receivables` no lugar da categoria |
| Criar título a pagar | nada | D categoria / C `ledger.payables` |
| Pagar | D categoria (`amount`); D `interest_expense`; D `fees`; C conta de pagamento (`amount + interest + fee - discount`); C `discounts_received` | igual, com D `ledger.payables` no lugar da categoria |

- Baixa parcial soma em `amount_settled` e leva a `partial` e depois a `settled`; baixa acima do saldo é recusada. A baixa trava o título e confere a revisão: duas baixas com a mesma revisão têm um vencedor.
- Estornar uma baixa estorna o lançamento dela e devolve o saldo; a linha da baixa fica, com o estorno.
- Cancelar exige nenhuma baixa ativa; em competência, estorna o reconhecimento.
- Parcelas: a soma é o total, o resto do arredondamento na última, vencimentos mensais no dia da primeira, limitados ao último dia dos meses curtos.
- Com origem, o título e as baixas são autorizados pelo registro de origem (`update`), como as regras; sem origem, pela gestão do livro.
- Os eventos de domínio previstos (`ledger.title_created`, `ledger.title_settled`, `ledger.title_cancelled`, `ledger.entry_posted`) não foram publicados nesta versão: a API de eventos do núcleo publica só sobre tabelas expostas e legíveis pelo ator, e títulos e lançamentos ficam fora do gateway. Publicá-los pede uma extensão do núcleo.

### Títulos recorrentes (#547)
`RecurringTitle` gera um título por ocorrência: direção, descrição, contraparte (`counterparty_name` em `personal_fields`), categoria, centro de custo, valor, `frequency` (`weekly`, `monthly`, `yearly`) a cada `interval`, `day_of_month` (só mensal), `start_date`, `end_date`, `max_occurrences`, `days_ahead` (0 a 366), `next_due`, `generated_count` e `is_active`. Livro, direção, início e a forma da repetição são imutáveis.

- Datas: a primeira ocorrência é `start_date` (na mensal com `day_of_month`, esse dia no mês do início, ou no seguinte se já passou); dias além do fim de um mês curto caem no último dia e voltam no mês seguinte (31/01, 28/02, 31/03); anual a partir de 29/02 cai em 28/02 nos anos comuns.
- O job `ledger.recurrences` roda todo dia (03:00) por organização, em contexto de sistema, com a app ativa (desativada, ele não gera; o worker cancela o job de organização inativa ou suspensa, como em qualquer app). Ele cria cada título que vence até `days_ahead` dias à frente com a chave `recurrence:<sysid>:<vencimento>` e emissão no vencimento (a competência da ocorrência): reexecutar não duplica, e um agendador atrasado recupera as ocorrências que faltam.
- Cada execução cria no máximo 500 títulos por organização; o resto fica para a execução seguinte.
- Um título que falha (categoria inativa, chave sem vínculo, período travado) vai ao log só com identificadores (`ledger.recurrence_failed`, `target_sysid`, `reason_code`), para a regra naquele ciclo sem desfazer as demais e é tentado de novo na execução seguinte.
- `end_date` e `max_occurrences` encerram a geração; estender um deles retoma a regra. Pausar interrompe; retomar pula as ocorrências que venceram durante a pausa e volta a gerar a partir de hoje.
- Alterar valor, descrição, contraparte ou centro de custo vale para as próximas ocorrências; os títulos já gerados não mudam.

### Saldos e relatórios (#548)
Todo número sai das partidas dos lançamentos selados (`sealed_at` preenchido), a cada consulta: não há saldo materializado, e um lançamento sem selo (que só existe dentro da transação que o cria) nunca entra. O saldo usa o sinal natural do tipo da conta: débito menos crédito em ativo e despesa; crédito menos débito em passivo, patrimônio e receita. Uma conta-grupo soma as descendentes.

- `balance`: saldo de uma conta (por id ou pela chave vinculada) numa data.
- `trial_balance`: balancete da janela, com saldo inicial, débitos, créditos e saldo final de cada conta; os totais somam só as analíticas, e débitos e créditos fecham em qualquer janela.
- `income_statement`: receitas menos despesas da janela, com o movimento das contas de resultado.
- `account_statement`: razão de uma conta (e descendentes), paginado em ordem estável (data, ano, número, partida), cada linha com o saldo corrente desde o saldo inicial da janela (calculado pelo banco sobre a janela inteira, então qualquer página tem o saldo certo).
- `cash_flow`: realizado (movimento das contas vinculadas a `ledger.cash` e `ledger.bank`) e previsto (saldo aberto dos títulos que vencem na janela), por mês.
- `aging`: saldo aberto atual dos títulos emitidos até a data, por faixa de atraso: a vencer, 1–30, 31–60, 61–90 e mais de 90 dias.
- Janelas de até cinco anos, com o fim depois do início; cada relatório faz um número fixo de consultas (as contas do livro e uma agregação; o razão e o fluxo de caixa, duas), qualquer que seja o volume. Só quem lê o livro.

### Telas (#549)
Páginas renderizadas no servidor, sem JavaScript, sobre o livro padrão da organização, com abas: livros e trava de período; plano de contas (contas, ativar e desativar, chaves e vínculos, centros de custo, aplicar o modelo); lançamentos (lista por período, detalhe com as partidas, novo manual com até seis partidas, estorno); a receber e a pagar (lista com filtros de tipo, situação e vencimento, novo título ou parcelado, detalhe com baixas, baixar, estornar baixa, cancelar); recorrências (lista, nova, alterar, pausar e retomar); relatórios (balancete, razão por conta, resultado, fluxo de caixa e títulos por atraso).

- Todas leem e escrevem pelo `LedgerService`; nenhuma consulta um modelo.
- Valores são digitados na unidade da moeda, com vírgula ou ponto decimal (`1.234,56`), convertidos por `money.parse` (casas a mais é erro de formulário) e mostrados pelo filtro `money` (`{% load ledger_money %}`), que formata `format_amount` no idioma da página.
- Toda mutação manda a revisão em que a página se baseou; um conflito volta como mensagem legível. Formulários de criação mandam uma chave de idempotência, então reenviar não duplica.
- Datas e filtros inválidos na URL respondem 400; registro ilegível ou de outra organização, 404. Os botões de escrita só aparecem para quem gere o livro, e o servidor revalida.

## Acceptance
- Uma app instalada que depende de `org.skeleton.ledger`, ou `SKELETON_BUNDLED_APPS=ledger`, instala o livro; sem nenhum dos dois, nada do ledger é importado.
- Um livro padrão por organização, inclusive com criação concorrente; moeda e regime imutáveis no banco; moeda fora da tabela recusada.
- A/B, app desativada e default deny em todos os métodos de serviço.
- Plano de contas com grupos e chaves; vínculo só com conta analítica, ativa e do tipo da chave (#543).
- Lançamento balanceado, numerado sem lacunas por livro e ano, selado; partida e lançamento selados não mudam; um estorno por lançamento; trava de período (#544).
- Regras registradas em código, conferidas por E041 e autorizadas pelo registro de origem; chave de idempotência repetida devolve o mesmo lançamento (#545).
- Títulos, parcelas e baixas com os lançamentos de cada regime; CAS na baixa; cancelamento só sem baixa ativa (#546).
- Recorrências geradas pelo job diário sem duplicar; fim, limite, pausa e retomada (#547).
- Relatórios só de partidas seladas, com número fixo de consultas, janelas de até cinco anos (#548).
- Telas sem JavaScript, pelo serviço, com valores no idioma, CAS e 400/404 (#549); cenário de ponta a ponta com uma app consumidora (#550).
