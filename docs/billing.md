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
| `SKELETON_BILLING_DEFAULT_CURRENCY` | `BRL` | Moeda das novas contas de cobrança e do preço padrão que elas recebem |
| `SKELETON_BILLING_PAYMENT_TERMS_DAYS` | `7` | Dias para pagar uma fatura de renovação |
| `SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD` | `false` | Trial só com meio de pagamento cadastrado |
| `BILLING_ENCRYPTION_KEYS` | vazio | Keyring Fernet dos segredos dos provedores de pagamento, separado dos outros e nunca derivado de `SECRET_KEY`. O primeiro cifra e todos decifram. |

Todos ficam no catálogo da [instância](deployment.md), editáveis em `/platform/instance/`. Com a cobrança ligada e sem o keyring, `manage.py check` falha com `skeleton.E037`. Um keyring inválido é `skeleton.E026`, como os demais.

## Ganchos de direito

Os pacotes de domínio do núcleo não importam subsistemas (regra R2 de [camadas](architecture.md#direção-das-dependências-entre-pacotes)). Por isso os pontos de decisão consultam um registro em `skeleton/common/entitlements.py`, e a cobrança registra nele a sua política em `ready()` (`skeleton/billing/entitlements.py`): uma app é paga quando existe um produto `app` para ela, e a organização tem direito enquanto um item ativo a vende ou um plano base ativo a inclui; a franquia de armazenamento é a soma do que os itens ativos incluem (zero ou sem assinatura: vale o padrão da instância); o estado de acesso é o `access_state` da conta; as rotas isentas são `billing`, para o administrador da organização. **Sem política registrada, tudo é permitido**, e o núcleo se comporta como antes.

| Pergunta da política | Onde o núcleo pergunta | Efeito da resposta |
|---|---|---|
| `entitled_apps(organização, apps)` | `availability_map` (`skeleton/system/availability.py`) | Uma app sem direito fica indisponível como uma app retirada pela plataforma: fecha em páginas (404), API, API de serviços (`Unavailable`), jobs, fluxos e navegação, e a ativação e a configuração da organização ficam guardadas para quando o direito voltar. `platform_availability_map` dá só a decisão da plataforma. Ativar uma app oferecida pela plataforma mas não contratada é recusado com `ApplicationNotEntitled` (código `app_not_entitled`), para as telas oferecerem a contratação. |
| `organization_access(organização)` | `TenantContextMiddleware`, `service_method`, gateway de registros, worker e agendador | `active` e `past_due`: normal. `restricted`: pessoas leem e não escrevem (método HTTP não seguro → 403 `organization_restricted`; comando da API de serviços → `Unavailable` com o mesmo código; escrita pelo gateway → `OrganizationAccessLimited`). `suspended`: só as rotas isentas, para quem pode regularizar; os demais membros veem a página "organização suspensa" (403 e, na API, `organization_suspended`); serviços de apps ficam `Unavailable` e jobs de apps são cancelados. A infraestrutura (`system_context`) continua escrevendo, e as leituras dos serviços do núcleo continuam, para as páginas de cobrança renderizarem. |
| `may_use_exempt_routes(organização, pessoa)` | `TenantContextMiddleware` | Quem pode chegar às rotas isentas enquanto o acesso está limitado. Uma rota isenta é um segmento registrado com `register_exempt_segment("billing")`, que vale para `/o/<id>/billing/` e `/api/v1/o/<id>/billing/`. |
| `storage_quota(organização)` | `quota_for` (`skeleton/files/quota.py`) | A franquia da assinatura (`None` para ilimitada, `NO_OPINION` para deixar a decisão ao setting). A `StorageQuota` da plataforma continua tendo precedência. |

A validação do vínculo vem primeiro: quem não é membro recebe 404 e não descobre o estado da organização. O estado de acesso sai só da política do servidor, nunca do cliente. Uma app de terceiros escrevendo direto nos próprios modelos, fora dos serviços e do gateway, não é coberta pelo modo só leitura: as superfícies HTTP já recusam a requisição antes, e o contrato do projeto manda escrever pelos serviços. Um serviço do núcleo que precisa funcionar com o acesso limitado (o de cobrança) declara `access_exempt = True`.

## Ciclo de vida

O motor (`skeleton/billing/engine.py`) trabalha numa organização, na transação de quem chama, e recebe `now` (o relógio da cobrança é `engine.clock()`, que os testes substituem). Ele nunca chama um provedor: emite faturas, e uma fatura paga transforma os itens pendentes que ela cobrou em direitos.

| Momento | O que acontece |
|---|---|
| Organização criada | Com a cobrança ligada, `create_organization` chama os ganchos de `skeleton/common/organization_hooks.py`, e a cobrança cria a conta (moeda `SKELETON_BILLING_DEFAULT_CURRENCY`) e a assinatura do **preço padrão** daquela moeda (`Price.is_default`, um por moeda, só de plano base). Sem preço padrão, a organização fica sem assinatura. Uma instância que liga a cobrança depois recebe o mesmo pelo job `billing.cycle`. |
| Trial | Com `trial_days`, a assinatura começa `trialing`, e o período é o trial. O trial vale **uma vez por organização e produto** (os itens encerrados são o histórico). Com `SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD`, só há trial com meio de pagamento cadastrado ([#400](https://github.com/alaorneto/skeleton/issues/400)). |
| Início sem trial | A assinatura começa `incomplete`, com uma fatura do primeiro período vencendo na hora. Ela não renova enquanto não for paga; um pagamento atrasado recomeça o período na data do pagamento (e muda o dia-âncora), para não cobrar meses sem acesso. |
| Contratar app ou pacote | Na hora, com uma linha `proration` pelo tempo restante do período (em segundos, arredondamento half-up). O item fica `pending`, sem direito, até a fatura ser paga. Durante o trial da assinatura, ou no trial do próprio add-on, o item já nasce ativo e sem cobrança, e a renovação cobra proporcionalmente a partir do fim do trial. Preço e assinatura precisam ter a mesma moeda e o mesmo ciclo (`price_cycle_mismatch`). Uma app já contratada ou incluída no plano é recusada (`already_subscribed`, `included_in_plan`). |
| Remover app ou pacote | No fim do período, sem estorno. Um item ainda não pago termina na hora, e a fatura aberta que só cobrava ele é anulada. |
| Subir de plano | Na hora: o novo preço proporcional e, na mesma fatura, um crédito (`adjustment`, negativo) da parte não usada do antigo. O item antigo termina quando a fatura é paga. |
| Descer de plano | No fim do período (`Subscription.scheduled_price`). |
| Cancelar | No fim do período (`cancel_at_period_end`, reversível com `resume`); durante o trial, na hora. Nenhum dado é apagado. |
| Renovar | O job periódico `billing.cycle` (de hora em hora, por organização, só com a cobrança ligada ao subir o processo) fecha cada período vencido: aplica a troca de plano agendada, encerra os itens removidos, cobra os ativos no período novo e acrescenta as linhas de outros módulos (`register_line_source`: excedente de armazenamento, descontos). Uma fatura de renovação vence em `SKELETON_BILLING_PAYMENT_TERMS_DAYS` dias, contados do início do período. Os períodos são em UTC; o dia-âncora vale para todos os meses, e um mês mais curto termina no último dia. Duas renovações simultâneas do mesmo período geram uma única fatura: a linha da assinatura fica travada. |
| Fatura com total zero | Paga na hora, sem provedor. |
| Fatura paga | `settle_invoice`: itens pendentes viram direitos e as apps que eles vendem são **ativadas** pela cobrança (`activate_for_entitlement`, dependências primeiro, com a mesma checagem de disponibilidade e o evento `app.enabled`). Uma ativação recusada fica no log, sem desfazer o pagamento. |

### Eventos

`billing.invoice_finalized`, `billing.invoice_paid` e `billing.subscription_changed`, com número, situação, moeda e total da fatura, ou a situação da assinatura ([eventos](events.md#eventos-da-cobrança)). Não vão para webhooks.

### Impostos

O núcleo não cobra imposto. Um projeto que precisa registra uma calculadora com `skeleton.billing.tax.register_tax_calculator(...)` em `ready()`: ela recebe a conta e as linhas e devolve o imposto na menor unidade da moeda. Nota fiscal fica fora do núcleo; um consumidor de `billing.invoice_paid` pode entregá-la a um emissor.

## Próximas etapas

| Issue | Entrega |
|---|---|
| [#397](https://github.com/alaorneto/skeleton/issues/397) | Vouchers |
| [#398](https://github.com/alaorneto/skeleton/issues/398) | Cotas: excedente de armazenamento e limites de plano |
| [#399](https://github.com/alaorneto/skeleton/issues/399) | Régua de inadimplência e bloqueio progressivo |
| [#400](https://github.com/alaorneto/skeleton/issues/400) | Interface de provedor, webhooks de entrada e conciliação |
| [#401](https://github.com/alaorneto/skeleton/issues/401), [#402](https://github.com/alaorneto/skeleton/issues/402), [#403](https://github.com/alaorneto/skeleton/issues/403) | Adaptadores Stripe, Mercado Pago e Asaas |
| [#404](https://github.com/alaorneto/skeleton/issues/404) | Páginas de cobrança da organização, API e `BillingService` |
| [#405](https://github.com/alaorneto/skeleton/issues/405) | Administração da plataforma |
