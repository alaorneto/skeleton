# Livro contábil como app first-party, separado da cobrança da plataforma

## Context
Apps que mexem com dinheiro (atendimentos pagos, despesas, repasses) precisam de partidas dobradas, títulos a pagar e a receber, baixas e recorrência. Colocar isso no núcleo violaria "sem regra de negócio em `skeleton`"; deixar cada app fazer o seu produziria livros incompatíveis. A cobrança da plataforma (`skeleton/billing`) é outra coisa: ela cobra as organizações, não é a contabilidade delas.

## Decision
O livro é uma app first-party em `skeleton/contrib/ledger`, opt-in, na versão do núcleo, que nunca importa `skeleton.billing` (o dinheiro foi para `skeleton.common.money`, #541). `Book` é `SecuredRecord` e carrega a autorização; as demais tabelas são da organização e escritas só pelos serviços. Um livro tem uma moeda e um regime (caixa ou competência), imutáveis, e uma organização pode ter vários livros.

## Consequences
Outras apps falam com o livro só pela API de serviços dele. Um lançamento nasce, é numerado sem lacunas e é selado na mesma transação (#544); corrige-se por estorno, nunca por edição, e os relatórios só leem partidas seladas (#548). Uma app lança pelas regras que registra em código, e a autorização vem do registro de origem dela, não do livro (#545): quem atende não precisa ler a contabilidade. Saldos são calculados das partidas a cada consulta, com número fixo de consultas, em vez de materializados; se o volume pedir, um saldo materializado entra depois sem mudar o contrato. Os eventos de domínio do livro ficaram de fora desta versão: a API de eventos do núcleo só publica sobre tabelas expostas e legíveis pelo ator, e títulos e lançamentos ficam fora do gateway. Multimoeda no mesmo livro, impostos, SPED e conciliação bancária ficam de fora. Remover a app preserva os dados (schema retido pelo snapshot do wheel).
