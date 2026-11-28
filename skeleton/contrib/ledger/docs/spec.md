# Livro contábil

O livro contábil (`skeleton_ledger`, `org.skeleton.ledger`) é uma app first-party do núcleo ([extensão](../../../../docs/extending.md#apps-first-party-do-núcleo-skeletoncontrib)). Trata-se de uma **biblioteca** (`kind: "bundled_library"`): ela é instalada junto com a app que depende dela ou via `SKELETON_BUNDLED_APPS=ledger`, não possui disponibilidade nem ativação próprias e fica efetiva em uma organização enquanto houver uma app ativa ali que a utilize.

O livro contábil não se confunde com a cobrança da plataforma (`skeleton/billing`, usada para cobrar as organizações): ele nunca importa `skeleton.billing` e representa todos os valores monetários com `skeleton.common.money` (inteiros da menor unidade da moeda, nunca `float`).

## Contract

### Livros (#542)
`Book` (`SecuredRecord`) pertence a uma organização e contém `name` (até 150 caracteres), `currency` (código ISO 4217 da tabela de `skeleton.common.money`, imutável), `basis` (`cash` para caixa ou `accrual` para competência, imutável), `is_default` e `locked_until` (trava de período). Uma organização pode manter vários livros (por exemplo, pessoa física e pessoa jurídica separadas) e possui **um único** livro padrão vivo, garantido por restrição única parcial; duas criações simultâneas resultam em apenas um livro.

- Ler um livro e todos os seus dados exige `read` em `Book`; gerir (contas, vínculos, lançamentos manuais e trava de período) exige `update` nele — concedido ao dono, ao administrador da organização ou via concessão de registro.
- Sem `TablePermission` de leitura em `skeleton_ledger.book`, a app não existe para o membro (404). A ação administrativa `member-access` concede leitura ao papel `member`.
- O gateway genérico da tabela (`/api/v1/o/<org>/tables/skeleton_ledger.book/records/`) permite editar apenas `name`; moeda, regime e trava mudam exclusivamente pelos serviços da app (`RecordOperations`). O livro padrão não pode ser excluído.
- A métrica de estado de plano `skeleton_ledger.books` contabiliza os livros vivos da organização.

### Plano de contas (#543)
`Account` pertence a um livro e possui `code` (único entre as contas vivas do livro), `name`, `kind` (`asset`, `liability`, `equity`, `revenue` ou `expense`), `parent`, `is_group` e `is_active`. Os campos `code`, `kind`, `parent` e `is_group` são imutáveis (protegidos por guard); `name` e `is_active` são editáveis com CAS. A conta pai precisa ser uma conta-grupo do **mesmo livro e do mesmo tipo** — o modelo valida essa regra e o banco a impõe por meio da FK composta `(book_id, kind, parent_id)`. Contas-grupo totalizam suas filhas e não recebem partidas diretas. Contas com filhas, vínculos ou partidas não podem ser excluídas (referência viva).

**Chaves de conta.** Cada chave (`AccountKey`, no formato `<app_label>.<nome>`, com o tipo de conta exigido) é registrada em código via `register_account_key` no `ready()`. `AccountBinding` associa a chave a uma conta analítica, ativa, pertencente ao livro e do tipo esperado (garantido no banco pela FK composta `(book_id, account_id)`). A própria biblioteca registra `ledger.cash`, `ledger.bank`, `ledger.receivables`, `ledger.payables`, `ledger.revenue`, `ledger.expenses`, `ledger.discounts_given`, `ledger.discounts_received`, `ledger.interest_income`, `ledger.interest_expense`, `ledger.fees` e `ledger.opening_balance`. A listagem de chaves exibe apenas as chaves pertencentes a apps ativas na organização.

**Modelo inicial e centros de custo.** `apply_chart` aplica o plano de contas "Prestação de serviços" (`chart.py`), criando contas de ativo, passivo, patrimônio, receitas e despesas junto com os vínculos das chaves base; reaplicar é idempotente e não gera duplicatas. `CostCenter` (`code` único vivo por livro, `name` e `is_active`) complementa a classificação gerencial.

### Lançamentos (#544)
`Entry` é um registro **selado** (`SealableMixin`, [modelo de dados](../../../../docs/data-model.md#registros-selados-526)), e `EntryLine` é sua tabela filha (`seal_parent = "entry"`). Todo lançamento é criado, recebe suas partidas e é selado na mesma transação — após o selo, nenhum dado pode ser alterado via modelo ou SQL. Correções ocorrem apenas por **estorno**: um novo lançamento com débitos e créditos invertidos, ligado ao original por `reverses` (no máximo um estorno por lançamento, via restrição única; estornos não podem ser estornados).

- Cada partida tem exatamente um débito ou um crédito, sempre como inteiro da menor unidade da moeda. O serviço valida e o banco revalida na transição do selo (`seal_condition_sql` congelada na migration): pelo menos duas partidas vivas, soma dos débitos igual à soma dos créditos e igual a `total`, todas as contas analíticas, ativas e do livro, todas as partidas na data do lançamento e data posterior a `locked_until`.
- Numeração sem lacunas por livro e ano (`year` e `number` únicos entre vivos): a linha do livro é travada ao obter o próximo número, garantindo que um rollback devolva o número (o livro usa trava própria porque a API de sequências do núcleo numera por organização, não por livro).
- `set_lock` altera a trava de período com CAS; tentar lançar em data igual ou anterior à trava responde `Conflict("period_locked")`.
- `idempotency_key` é única entre vivos por `(organização, source_app, chave)`: repetir a chamada devolve o lançamento já criado.
- Saldos, razões e relatórios consideram exclusivamente lançamentos selados.

### Regras de lançamento (#545)
Apps lançam a partir de seus registros usando **regras declaradas em código** (`PostingRule`, registradas via `register_posting_rule` no `ready()`; sem `eval`, expressões dinâmicas ou nomes de classe vindos de dados). Cada regra define a tabela e a ação de origem (`source_table_key`, da própria app, e `source_action`) e suas linhas: cada `RuleLine` especifica o lado, a conta (`Key(código)`, resolvida pelo vínculo do livro, ou `Param(nome)`, passada na chamada) e o nome do componente de valor. Componentes zerados omitem suas linhas, e o resultado final precisa fechar. O check `skeleton.E041` recusa regras cuja tabela de origem não esteja registrada ou não seja uma tabela de negócio exposta nem uma tabela derivada (`TableSpec.access_parent`, autorizada pelo registro âncora).

**A autorização deriva do registro de origem:** quem possui a ação da regra no registro (por exemplo, `update` em um atendimento) gera o lançamento sem precisar ler o livro, e consulta os lançamentos daquele registro por `entries_for_source`. O estorno de um lançamento com origem segue a mesma regra de autorização. A descrição de lançamentos automáticos referencia o número do documento de origem, nunca o nome de uma pessoa; a contraparte é registrada apenas em `party_ref` (opaco). O kit `skeleton.contrib.ledger.testing` (`enable_ledger`, `prepare_book` e `account_id`) facilita montar livro, plano e vínculos nos testes de apps consumidoras.

### Títulos e baixas (#546)
`Title` representa um valor a receber (`receivable`) ou a pagar (`payable`) em um livro: `amount` (principal > 0), `amount_settled` (`0 <= baixado <= valor`), `status` (`open`, `partial`, `settled` ou `cancelled`, mantido coerente por restrição no banco), datas de emissão e vencimento, conta de categoria (resolvida por `category_key`), centro de custo, origem opcional e numeração de parcelas. `counterparty_name` é dado pessoal (`personal_fields`): legível por quem lê o livro e por quem lê a origem, e substituído por `[REDACTED]` na auditoria. `Settlement` registra cada baixa (data, `amount` baixado, `discount`, `interest`, `fee`, conta de pagamento e forma) vinculada ao lançamento gerado; baixas são estornadas, nunca editadas.

| Evento | Regime de caixa | Regime de competência |
|---|---|---|
| Criar título a receber | Sem lançamento | D `ledger.receivables` / C categoria |
| Receber | D conta de pagamento (`amount + interest - discount - fee`); D `discounts_given`; D `fees`; C categoria (`amount`); C `interest_income` | Igual, substituindo C categoria por C `ledger.receivables` |
| Criar título a pagar | Sem lançamento | D categoria / C `ledger.payables` |
| Pagar | D categoria (`amount`); D `interest_expense`; D `fees`; C conta de pagamento (`amount + interest + fee - discount`); C `discounts_received` | Igual, substituindo D categoria por D `ledger.payables` |

- Baixas parciais acumulam em `amount_settled`, avançando o estado de `open` para `partial` e depois `settled`; valores acima do saldo em aberto são recusados. A baixa trava a linha do título e valida a revisão (CAS), garantindo um único vencedor em requisições concorrentes.
- Estornar uma baixa gera o estorno do lançamento correspondente e recompõe o saldo em aberto, mantendo a linha da baixa com referência ao estorno.
- Cancelar exige que não haja baixas ativas; no regime de competência, estorna também o lançamento de reconhecimento.
- Parcelamento: a soma das parcelas iguala o total (com eventuais centavos de arredondamento ajustados na última parcela), e os vencimentos seguem mensalmente o dia da primeira, ajustados ao último dia em meses mais curtos.
- Títulos com origem têm criação e baixas autorizadas pelo registro de origem (`update`), como nas regras de lançamento; títulos sem origem exigem gestão do livro.
- Os eventos de domínio planejados (`ledger.title_created`, `ledger.title_settled`, `ledger.title_cancelled` e `ledger.entry_posted`) ainda não são publicados nesta versão, pois a API de eventos do núcleo publica apenas sobre tabelas expostas e legíveis pelo ator, enquanto títulos e lançamentos permanecem fora do gateway. Publicá-los dependerá de uma extensão futura do núcleo.

### Títulos recorrentes (#547)
`RecurringTitle` gera um título por ocorrência periódica, armazenando direção, descrição, contraparte (`counterparty_name` em `personal_fields`), categoria, centro de custo, valor, `frequency` (`weekly`, `monthly` ou `yearly`) a cada `interval`, `day_of_month` (apenas mensal), `start_date`, `end_date`, `max_occurrences`, `days_ahead` (0 a 366), `next_due`, `generated_count` e `is_active`. Livro, direção, data de início e forma de repetição são imutáveis.

- **Cálculo de datas:** a primeira ocorrência cai em `start_date` (na frequência mensal com `day_of_month`, nesse dia dentro do mês inicial ou no mês seguinte se o dia já passou). Dias acima do fim de um mês curto são ajustados para o último dia e retornam ao dia original no mês seguinte (31/01, 28/02, 31/03); recorrências anuais iniciadas em 29/02 caem em 28/02 nos anos não bissextos.
- **Job diário (`ledger.recurrences`):** executa diariamente às 03:00 por organização, em contexto de sistema, enquanto a biblioteca estiver efetiva (com ela inativa nada é gerado, e o worker cancela jobs de organizações inativas ou suspensas). Gera cada título com vencimento em até `days_ahead` dias, usando a chave de idempotência `recurrence:<sysid>:<vencimento>` e data de emissão igual ao vencimento (competência da ocorrência). Reexecuções não duplicam títulos, e eventuais atrasos do agendador são recuperados automaticamente.
- Cada execução gera no máximo 500 títulos por organização; o excedente fica para o ciclo seguinte.
- Falhas na geração de um título (categoria inativa, chave sem vínculo ou período travado) são registradas em log apenas com identificadores (`ledger.recurrence_failed`, `target_sysid`, `reason_code`), interrompendo aquela regra no ciclo sem desfazer as demais e permitindo nova tentativa na execução seguinte.
- `end_date` e `max_occurrences` encerram a regra; estender qualquer um dos limites retoma a geração. Pausar suspende o agendamento; retomar descarta ocorrências vencidas durante a pausa e retoma a partir da data atual.
- Alterar valor, descrição, contraparte ou centro de custo afeta apenas ocorrências futuras, sem modificar títulos já gerados.

### Saldos e relatórios (#548)
Todos os números são calculados diretamente das partidas de lançamentos selados (`sealed_at` preenchido) em cada consulta: não há saldo materializado, e lançamentos ainda sem selo (visíveis apenas dentro da própria transação de criação) nunca são contabilizados. Cada saldo segue o sinal natural do tipo da conta — débito menos crédito em ativo e despesa; crédito menos débito em passivo, patrimônio e receita —, e contas-grupo somam suas descendentes.

- `balance`: saldo de uma conta (por ID ou chave vinculada) em determinada data.
- `trial_balance`: balancete do período com saldo inicial, débitos, créditos e saldo final de cada conta; os totais consolidam apenas contas analíticas, garantindo fechamento entre débitos e créditos em qualquer janela.
- `income_statement`: apuração de resultado (receitas menos despesas) com o movimento das contas de resultado no período.
- `account_statement`: razão de uma conta e suas descendentes, paginado em ordem determinística (data, ano, número e partida), trazendo em cada linha o saldo corrente desde o saldo inicial da janela (calculado no banco sobre toda a janela para garantir consistência em qualquer página).
- `cash_flow`: fluxo de caixa mensal comparando realizado (movimentação das contas vinculadas a `ledger.cash` e `ledger.bank`) e previsto (saldo em aberto dos títulos com vencimento na janela).
- `aging`: saldo em aberto dos títulos emitidos até a data de referência, distribuído nas faixas a vencer, 1–30, 31–60, 61–90 e mais de 90 dias.
- Todos os relatórios aceitam janelas de até cinco anos (com fim posterior ao início), exigem leitura do livro e executam número fixo de consultas independentemente do volume (contas do livro mais uma agregação; duas agregações no razão e no fluxo de caixa).

### Telas (#549)
As páginas são renderizadas no servidor, sem JavaScript, operando sobre o livro padrão da organização e organizadas em abas:

- **Livros e trava de período**;
- **Plano de contas** (contas, ativação e desativação, chaves e vínculos, centros de custo e aplicação do modelo inicial);
- **Lançamentos** (listagem por período, detalhe com partidas, lançamento manual com até seis partidas e estorno);
- **A receber e a pagar** (listagem filtrável por tipo, situação e vencimento, criação de título único ou parcelado, detalhe com baixas, baixa, estorno de baixa e cancelamento);
- **Recorrências** (listagem, criação, edição, pausa e retomada);
- **Relatórios** (balancete, razão por conta, demonstração de resultado, fluxo de caixa e títulos por atraso).

Regras transversais das telas:

- Toda leitura e escrita passa pelo `LedgerService`; nenhuma view consulta modelos diretamente.
- Valores são digitados na unidade da moeda com vírgula ou ponto decimal (`1.234,56`), convertidos por `money.parse` (casas decimais excedentes geram erro de formulário) e renderizados com o filtro `money` (`{% load ledger_money %}`), que aplica `format_amount` no idioma da página.
- Toda mutação envia a revisão lida pela página (CAS), exibindo conflitos como mensagem legível; formulários de criação incluem chave de idempotência para evitar duplicações em reenvios.
- Datas ou filtros inválidos na URL respondem 400; registros ilegíveis ou de outra organização respondem 404. Controles de escrita só aparecem para quem pode gerir o livro, e o servidor sempre revalida.

## Acceptance
- Instalar uma app que depende de `org.skeleton.ledger` ou definir `SKELETON_BUNDLED_APPS=ledger` instala o livro; sem nenhum dos dois, nada do ledger é importado.
- Um único livro padrão por organização, inclusive sob criação concorrente; moeda e regime imutáveis no banco; moedas ausentes da tabela são recusadas.
- Isolamento A/B, comportamento com app desativada e default deny validados em todos os métodos de serviço.
- Plano de contas com grupos e chaves, aceitando vínculos apenas com contas analíticas, ativas e do mesmo tipo da chave.
- Lançamentos balanceados, numerados sem lacunas por livro e ano e selados; lançamentos e partidas selados não mudam; no máximo um estorno por lançamento; respeito à trava de período.
- Regras registradas em código, validadas pelo check `skeleton.E041` e autorizadas pelo registro de origem; repetir `idempotency_key` devolve o mesmo lançamento.
- Títulos, parcelas e baixas gerando os lançamentos esperados em caixa e em competência; CAS nas baixas; cancelamento permitido somente sem baixas ativas.
- Recorrências geradas pelo job diário sem duplicação, respeitando término, limite, pausa e retomada.
- Relatórios considerando apenas partidas seladas, com número fixo de consultas e janelas de até cinco anos.
- Telas sem JavaScript operando via serviço, com formatação monetária no idioma, CAS, tratamento 400/404 e cenário de ponta a ponta com app consumidora.
