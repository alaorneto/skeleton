# Cobrança: assinaturas, cotas e pagamentos

`skeleton/billing` (label `sk_billing`) é o subsistema que permite vender a plataforma como SaaS: catálogo de planos, apps e pacotes de armazenamento, assinatura de cada organização, faturas e pagamentos. É o [epic #406](https://github.com/alaorneto/skeleton/issues/406), entregue em etapas; esta página diz o que já existe e o que vem em cada issue.

A cobrança é **desligada por padrão** (`SKELETON_BILLING_ENABLED=false`). Desligada, as tabelas existem e ficam vazias, e nenhum comportamento do núcleo muda: instalações próprias, que não vendem a plataforma, seguem idênticas.

## Decisões

- **O núcleo é a fonte de verdade.** Planos, períodos, trial, vouchers, faturas, inadimplência e cotas são calculados aqui. O provedor de pagamento só recebe o pagamento de cada fatura. Assim o comportamento é o mesmo com qualquer provedor, e trocar de provedor não migra assinaturas. As assinaturas nativas dos provedores (Stripe Billing, preapproval do Mercado Pago) não são usadas.
- **A conta de cobrança é a organização, e as apps são itens da assinatura dela.** Pagamento, inadimplência, meio de pagamento e bloqueio são fatos da organização. Cobrar cada app à parte geraria várias faturas e réguas de cobrança, e estados contraditórios ("paga a app A, deve a app B"). O preço continua granular: um **plano base** (opcional) traz membros, armazenamento e apps incluídas; cada **app paga** é um item com preço próprio; **pacotes de armazenamento** são outro tipo de item.
- **A cota de armazenamento é um pool da organização.** A franquia é a soma do que os itens incluem. Uma cota por app bloquearia uploads numa app com espaço sobrando em outra. O uso por app aparece só como relatório.
- **Multimoeda, sem módulo fiscal.** Cada preço tem moeda própria. Impostos e nota fiscal ficam fora; haverá um ponto de extensão (`TaxCalculator`) e o evento de fatura paga para um emissor externo.
- **Provedores plugáveis**, começando por Mercado Pago, Asaas e Stripe ([#400](https://github.com/alaorneto/skeleton/issues/400) a [#403](https://github.com/alaorneto/skeleton/issues/403)). Não existe padrão aberto que abstraia assinaturas entre provedores; o núcleo define um protocolo próprio, com checkout sempre hospedado pelo provedor, para que nenhum dado de cartão passe pelo servidor.

## Dinheiro

Valores são **inteiros na menor unidade da moeda** (centavos para BRL, ienes para JPY), nunca `float`. `skeleton.billing.money` traz:

| Função | O que faz |
|---|---|
| `validate_currency(code)` | Aceita só códigos ISO 4217 da tabela `CURRENCY_EXPONENTS`; uma moeda fora dela é recusada, porque supor duas casas erraria o preço em JPY ou KWD por um fator de 100 |
| `scale(amount, numerator, denominator)` | `amount × numerator / denominator` com arredondamento half-up, para longe do zero: a linha de desconto arredonda como a cobrança que compensa |
| `percent(amount, basis_points)` | Percentual em centésimos de ponto (10000 = 100%) |
| `parse(text, currency)` | `"12.34"` em BRL vira `1234`; mais casas decimais do que a moeda permite é recusado |
| `format_amount(amount, currency)` | O inverso, em texto decimal simples; a formatação por idioma fica nos templates |

## Modelo de dados

O catálogo é global (`SystemRecord`) e só a plataforma o escreve. O que pertence a uma organização (`OrganizationRecord`) só é escrito pelos serviços de cobrança. Nenhuma tabela entra no gateway genérico. O catálogo das tabelas está em [catálogo de tabelas](table-catalog.md#migration-e-checks).

| Tabela | Base | Papel |
|---|---|---|
| `Product` | global | O que se vende: `base_plan`, `app` (com o `app_label` vendido) ou `storage_pack`. Código, tipo e app são imutáveis. |
| `Price` | global | Preço de um produto: moeda, valor, ciclo (`month`/`year` × `interval_count`), dias de trial, armazenamento incluído, apps incluídas (plano base), limites (`{"members": 10, "<app>.<métrica>": N}`) e política de excedente (`block` ou `bill`, com bloco, valor e teto). **Os termos nunca mudam**: mudar o preço é criar outro, e as assinaturas existentes mantêm o antigo. Só `is_active` muda, e fecha a oferta a novas assinaturas. |
| `InvoiceCounter` | global | Último número de fatura de cada ano, na plataforma inteira. |
| `BillingAccount` | organização | Uma por organização: razão social, email de cobrança e documento (dados pessoais, redigidos na auditoria), país, moeda, estado de acesso (`active`, `past_due`, `restricted`, `suspended`) e a versão dos termos aceita. |
| `Subscription` | organização | O contrato recorrente: situação (`trialing`, `active`, `past_due`, `canceled`, `incomplete`), moeda e ciclo (imutáveis), período corrente, fim do trial, cancelamento no fim do período e modo de cobrança (`auto_charge` ou `send_invoice`). Uma assinatura viva por organização; as canceladas ficam como histórico. |
| `SubscriptionItem` | organização | Uma linha precificada da assinatura (plano, app ou pacote), com quantidade, situação (`pending`, `active`, `ended`), datas e fim do trial. Preço e app são imutáveis; as linhas encerradas são o histórico que impede um segundo trial do mesmo produto. |
| `Invoice` | organização | O valor devido. Número, moeda, subtotal, desconto, imposto, total, período e emissão são **imutáveis** depois de emitida; só o estado de cobrança muda (`open`, `paid`, `void`, `uncollectible`, valor pago e datas), sempre com a revisão. O banco confere `total = subtotal − desconto + imposto`, valor pago ≤ total e que uma fatura paga tem data e valor integral. |
| `InvoiceLine` | organização | Uma linha da fatura (`subscription`, `proration`, `overage`, `discount`, `adjustment`). **Nunca muda nem é excluída**: todas as colunas, inclusive as de exclusão lógica, são imutáveis no guard do banco. Desconto é negativo. |
| `Payment` | organização | Uma tentativa de pagamento: método (`card`, `pix`, `boleto`, `offline`), situação, valor na moeda da fatura, provedor e a referência dele (única por provedor). O pagamento `offline` é o registro manual da plataforma e não tem provedor. |

### Numeração das faturas

Quem emite as faturas é a plataforma, então a numeração é **global e sem lacunas por ano UTC**, no formato `<prefixo>AAAA-NNNNNN` (`SKELETON_BILLING_INVOICE_PREFIX`, vazio por padrão). `skeleton.billing.numbering.next_invoice_number(at=...)` trava a linha do ano com `FOR UPDATE` na transação de quem chama: se a fatura for desfeita, o número volta e é reusado. A primeira criação da linha de um ano disputa a constraint única num savepoint, sem upsert. O preço é emitir uma fatura por vez na plataforma. As [sequências](sequences.md) do núcleo não servem aqui, porque são por organização.

## Configuração

| Setting | Padrão | Efeito |
|---|---|---|
| `SKELETON_BILLING_ENABLED` | `false` | Liga a cobrança |
| `SKELETON_BILLING_INVOICE_PREFIX` | vazio | Texto antes do ano no número da fatura (até 16 letras, dígitos ou hífen) |
| `BILLING_ENCRYPTION_KEYS` | vazio | Keyring Fernet dos segredos dos provedores de pagamento, separado dos outros e nunca derivado de `SECRET_KEY`. O primeiro cifra e todos decifram. |

Os três ficam no catálogo da [instância](deployment.md), editáveis em `/platform/instance/`. Com a cobrança ligada e sem o keyring, `manage.py check` falha com `skeleton.E037`. Um keyring inválido é `skeleton.E026`, como os demais.

## Próximas etapas

| Issue | Entrega |
|---|---|
| [#395](https://github.com/alaorneto/skeleton/issues/395) | Ganchos de direito no núcleo: apps sem direito fecham, organização inadimplente fica só leitura ou suspensa, cota de armazenamento vinda do plano |
| [#396](https://github.com/alaorneto/skeleton/issues/396) | Motor de assinatura: plano padrão, trial, renovação, proração e liberação da app pelo pagamento |
| [#397](https://github.com/alaorneto/skeleton/issues/397) | Vouchers |
| [#398](https://github.com/alaorneto/skeleton/issues/398) | Cotas: excedente de armazenamento e limites de plano |
| [#399](https://github.com/alaorneto/skeleton/issues/399) | Régua de inadimplência e bloqueio progressivo |
| [#400](https://github.com/alaorneto/skeleton/issues/400) | Interface de provedor, webhooks de entrada e conciliação |
| [#401](https://github.com/alaorneto/skeleton/issues/401), [#402](https://github.com/alaorneto/skeleton/issues/402), [#403](https://github.com/alaorneto/skeleton/issues/403) | Adaptadores Stripe, Mercado Pago e Asaas |
| [#404](https://github.com/alaorneto/skeleton/issues/404) | Páginas de cobrança da organização, API e `BillingService` |
| [#405](https://github.com/alaorneto/skeleton/issues/405) | Administração da plataforma |
