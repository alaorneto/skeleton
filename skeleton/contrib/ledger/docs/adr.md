# Livro contábil como app first-party, separado da cobrança da plataforma

## Context
Apps que movimentam valores financeiros — como atendimentos pagos, despesas e repasses — precisam de lançamentos em partidas dobradas, títulos a pagar e a receber, baixas e recorrências. Incluir essa lógica no núcleo violaria o princípio de não colocar regras de negócio em `skeleton`; por outro lado, deixar cada app construir sua própria contabilidade produziria livros incompatíveis entre si.

A cobrança da plataforma (`skeleton/billing`) cumpre outro papel: ela gerencia assinaturas e faturas cobradas das organizações, e não a contabilidade interna de cada organização.

## Decision
O livro contábil é implementado como app first-party opt-in em `skeleton/contrib/ledger`, acompanhando a versão do núcleo, sem jamais importar `skeleton.billing` (os tipos e utilitários monetários vivem em `skeleton.common.money`). `Book` é um `SecuredRecord` que concentra a autorização; as demais tabelas pertencem à organização e são escritas exclusivamente pelos serviços da app. Cada livro possui moeda e regime (caixa ou competência) imutáveis, e uma organização pode manter múltiplos livros.

## Consequences
Outras apps interagem com o livro exclusivamente por meio da sua API de serviços:

- **Integridade dos lançamentos:** todo lançamento nasce, recebe numeração sem lacunas e é selado na mesma transação. Correções ocorrem apenas por estorno, nunca por edição, e os relatórios consideram exclusivamente partidas seladas.
- **Autorização pela origem:** apps lançam por regras declaradas em código, com autorização derivada do registro de origem em vez do livro — assim, quem registra um atendimento não precisa ter acesso de leitura à contabilidade.
- **Saldos calculados sob demanda:** os saldos são calculados diretamente a partir das partidas a cada consulta, com número fixo de queries, evitando saldos materializados. Se o volume exigir no futuro, saldos materializados podem ser introduzidos sem alterar o contrato.
- **Limites desta versão:** eventos de domínio do livro ficaram de fora porque a API de eventos do núcleo publica apenas sobre tabelas expostas e legíveis pelo ator, enquanto títulos e lançamentos permanecem fora do gateway. Multimoeda em um mesmo livro, impostos, SPED e conciliação bancária também não fazem parte do escopo.
- **Preservação de dados:** remover a app preserva os dados no banco, pois o schema é retido pelo snapshot do wheel.
