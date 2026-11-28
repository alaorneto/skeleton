# Cobrança: assinaturas, cotas e pagamentos

`skeleton/billing` (label `sk_billing`) é o subsistema que permite vender a plataforma como SaaS, gerenciando o catálogo de planos, apps e pacotes de armazenamento, além das assinaturas, faturas e pagamentos de cada organização. É o [epic #406](https://github.com/alaorneto/skeleton/issues/406).

A cobrança vem **desligada por padrão** (`SKELETON_BILLING_ENABLED=false`). Nesse estado, as tabelas existem e permanecem vazias, sem alterar nenhum comportamento do núcleo: instalações próprias que não vendem a plataforma continuam idênticas.

## Decisões

- **O núcleo é a fonte de verdade.** Planos, períodos, trial, vouchers, faturas, inadimplência e cotas são calculados no núcleo. O provedor de pagamento atua apenas para receber o pagamento de cada fatura. Isso garante comportamento uniforme entre provedores e dispensa migração de assinaturas ao trocar de provedor. Assinaturas nativas dos provedores (como Stripe Billing ou preapproval do Mercado Pago) não são usadas.
- **A conta de cobrança pertence à organização, e as apps são itens da assinatura.** Pagamento, inadimplência, meio de pagamento e bloqueio dizem respeito à organização como um todo. Cobrar cada app separadamente geraria múltiplas faturas, réguas paralelas e estados contraditórios ("paga a app A, deve a app B"). A precificação continua granular: um **plano base** (opcional) inclui membros, armazenamento e apps; cada **app paga** é um item com preço próprio; e **pacotes de armazenamento** formam outro tipo de item.
- **A cota de armazenamento é um pool compartilhado da organização.** A franquia corresponde à soma do que os itens contratados incluem. Cotas individuais por app bloqueariam uploads mesmo com espaço disponível em outra app. O consumo por app aparece apenas como relatório.
- **Multimoeda, sem módulo fiscal.** Cada preço tem moeda própria. Impostos e emissão de nota fiscal ficam fora do núcleo, atendidos por um ponto de extensão (`TaxCalculator`) e pelo evento de fatura paga para integração com emissor externo.
- **Provedores plugáveis**, começando por Mercado Pago, Asaas e Stripe ([#400](https://github.com/alaorneto/skeleton/issues/400) a [#403](https://github.com/alaorneto/skeleton/issues/403)). Como não há padrão aberto que abstraia assinaturas entre provedores, o núcleo define um protocolo próprio, com checkout sempre hospedado pelo provedor para que nenhum dado de cartão passe pelo servidor.

## Dinheiro

Valores monetários são sempre **inteiros na menor unidade da moeda** (centavos para BRL, ienes para JPY), nunca `float`. O módulo `skeleton.common.money` (`skeleton.billing.money` permanece como caminho histórico que apenas reexporta) oferece:

| Função | O que faz |
|---|---|
| `validate_currency(code)` | Aceita apenas códigos ISO 4217 da tabela `CURRENCY_EXPONENTS`; moedas fora dela são recusadas, pois presumir duas casas decimais causaria erro de fator 100 em moedas como JPY ou KWD. |
| `scale(amount, numerator, denominator)` | Calcula `amount × numerator / denominator` com arredondamento *half-up*, para longe do zero, de modo que a linha de desconto arredonda da mesma forma que a cobrança compensada. |
| `percent(amount, basis_points)` | Aplica percentual em pontos-base (10000 = 100%). |
| `parse(text, currency)` | Converte `"12.34"` em BRL para `1234`; recusa textos com mais casas decimais do que a moeda admite. |
| `format_amount(amount, currency)` | Faz o inverso em texto decimal simples; a formatação regional fica nos templates. |

## Modelo de dados

O catálogo é global (`SystemRecord`) e só pode ser alterado pela plataforma. Os dados de uma organização (`OrganizationRecord`) só são escritos pelos serviços de cobrança. Nenhuma tabela do subsistema entra no gateway genérico. Veja o registro completo no [catálogo de tabelas](table-catalog.md#migration-e-checks).

| Tabela | Base | Papel |
|---|---|---|
| `Product` | global | O que se vende: `base_plan`, `app` (vinculado ao `app_label` vendido) ou `storage_pack`. Código, tipo e app são imutáveis. Bibliotecas first-party não são vendidas nem entram em `included_apps`: acompanham as apps que as utilizam (`code="library"`). |
| `Price` | global | Preço de um produto: moeda, valor, ciclo (`month` ou `year` × `interval_count`), dias de trial, armazenamento incluído, apps incluídas (no plano base), limites (`{"members": 10, "<app>.<métrica>": N}`) e política de excedente (`block` ou `bill`, com bloco, valor e teto). **Os termos são imutáveis**: para alterar o preço, cria-se outro, mantendo as assinaturas existentes no antigo. Apenas `is_active` pode mudar, encerrando a oferta para novas assinaturas. |
| `InvoiceCounter` | global | Último número de fatura emitido em cada ano, em toda a plataforma. |
| `BillingAccount` | organização | Uma por organização: razão social, e-mail de cobrança e documento (dados pessoais, redigidos na auditoria), país, moeda, estado de acesso (`active`, `past_due`, `restricted`, `suspended`) e versão dos termos aceita. |
| `Subscription` | organização | Contrato recorrente: situação (`trialing`, `active`, `past_due`, `canceled`, `incomplete`), moeda e ciclo (imutáveis), período corrente, fim do trial, cancelamento ao fim do período e modo de cobrança (`auto_charge` ou `send_invoice`). Há apenas uma assinatura ativa por organização; as canceladas ficam no histórico. |
| `SubscriptionItem` | organização | Linha precificada da assinatura (plano, app ou pacote), com quantidade, situação (`pending`, `active`, `ended`), datas e fim do trial. Preço e app são imutáveis; linhas encerradas formam o histórico que impede um segundo trial do mesmo produto. |
| `Invoice` | organização | Valor devido. Número, moeda, subtotal, desconto, imposto, total, período e emissão são **imutáveis** após a emissão; apenas o estado de cobrança muda (`open`, `paid`, `void`, `uncollectible`, `refunded`, valor pago e datas), sempre com controle de revisão. O banco valida `total = subtotal − desconto + imposto`, valor pago ≤ total e exige data e quitação integral para faturas pagas. |
| `InvoiceLine` | organização | Linha da fatura (`subscription`, `proration`, `overage`, `discount`, `adjustment`). **Nunca muda nem é excluída**: todas as colunas, inclusive as de exclusão lógica, são imutáveis pelo guard do banco. Descontos têm valor negativo. |
| `UsageSnapshot` | operacional | Fotografia do armazenamento usado pela organização em um dia UTC, com quebra por app; serve de base para cobrança de excedente e relatórios de uso. |
| `Payment` | organização | Tentativa de pagamento: método (`card`, `pix`, `boleto`, `offline`), situação, valor na moeda da fatura, provedor e referência externa (única por provedor). Pagamentos `offline` são registros manuais da plataforma e não possuem provedor. |

### Numeração das faturas

Como a plataforma emite todas as faturas, a numeração é **global e sem lacunas por ano UTC**, no formato `<prefixo>AAAA-NNNNNN` (`SKELETON_BILLING_INVOICE_PREFIX`, vazio por padrão).

`skeleton.billing.numbering.next_invoice_number(at=...)` bloqueia a linha do ano com `FOR UPDATE` na transação chamadora: se a fatura sofrer rollback, o número volta e é reutilizado. A primeira criação da linha do ano disputa a constraint única em um savepoint, sem upsert. Essa garantia serializa a emissão a uma fatura por vez na plataforma. As [sequências](sequences.md) do núcleo não se aplicam aqui porque têm escopo por organização.

## Configuração

| Setting | Padrão | Efeito |
|---|---|---|
| `SKELETON_BILLING_ENABLED` | `false` | Ativa o subsistema de cobrança |
| `SKELETON_BILLING_INVOICE_PREFIX` | vazio | Prefixo antes do ano no número da fatura (até 16 letras, dígitos ou hífen) |
| `SKELETON_BILLING_DEFAULT_CURRENCY` | `BRL` | Moeda das novas contas de cobrança e do preço padrão atribuído a elas |
| `SKELETON_BILLING_PAYMENT_TERMS_DAYS` | `7` | Prazo em dias para pagamento de faturas de renovação |
| `SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD` | `false` | Exige meio de pagamento cadastrado para iniciar trial |
| `SKELETON_BILLING_GRACE_DAYS` | `7` | Dias completos de atraso até entrar em modo somente leitura |
| `SKELETON_BILLING_SUSPEND_AFTER_DAYS` | `30` | Dias de atraso até a suspensão do acesso |
| `SKELETON_BILLING_CANCEL_AFTER_DAYS` | `60` | Dias de atraso até o cancelamento da assinatura e marcação da fatura como incobrável |
| `SKELETON_BILLING_RETRY_SCHEDULE_DAYS` | `1,3,5` | Dias após o vencimento para novas tentativas de cobrança automática |
| `SKELETON_BILLING_PENDING_PAYMENT_HOLD_DAYS` | `3` | Dias durante os quais um pagamento pendente segura o bloqueio |
| `SKELETON_BILLING_USAGE_RETENTION_DAYS` | `400` | Dias de retenção das fotografias diárias de armazenamento (mínimo 31) |
| `BILLING_ENCRYPTION_KEYS` | vazio | Keyring Fernet para segredos dos provedores de pagamento, separado dos demais e nunca derivado de `SECRET_KEY`. A primeira chave cifra e todas decifram. |

Todas essas chaves ficam no catálogo da [instância](deployment.md) e são editáveis em `/platform/instance/`. Com a cobrança ligada e sem o keyring configurado, `manage.py check` falha com `skeleton.E037`. Um keyring inválido dispara `skeleton.E026`, como nos demais subsistemas.

## Ganchos de direito

Para respeitar a regra R2 de [camadas](architecture.md#direção-das-dependências-entre-pacotes) — que impede pacotes de domínio do núcleo de importar subsistemas —, os pontos de decisão consultam um registro em `skeleton/common/entitlements.py`.

Ao iniciar (`ready()`), a cobrança registra ali sua política (`skeleton/billing/entitlements.py`):
- uma app é paga quando existe um produto `app` para ela, e a organização tem direito enquanto houver um item ativo que a venda ou um plano base ativo que a inclua;
- a franquia de armazenamento é a soma do que os itens ativos incluem (zero ou sem assinatura recorre ao padrão da instância);
- o estado de acesso corresponde ao `access_state` da conta;
- as rotas isentas são `billing`, acessíveis ao administrador da organização.

**Sem política registrada, tudo é permitido**, mantendo o comportamento padrão do núcleo.

| Pergunta da política | Onde o núcleo pergunta | Efeito da resposta |
|---|---|---|
| `entitled_apps(organização, apps)` | `availability_map` (`skeleton/system/availability.py`) | Apps sem direito ficam indisponíveis da mesma forma que apps retiradas pela plataforma: bloqueadas em páginas (404), API, API de serviços (`Unavailable`), jobs, fluxos e navegação. A ativação e a configuração da organização permanecem preservadas até o direito voltar. `platform_availability_map` retorna apenas a decisão da plataforma. Tentar ativar uma app oferecida pela plataforma mas não contratada falha com `ApplicationNotEntitled` (código `app_not_entitled`), permitindo que a interface ofereça a contratação. |
| `organization_access(organização)` | `TenantContextMiddleware`, `service_method`, gateway de registros, worker e agendador | `active` e `past_due`: acesso normal. `restricted`: pessoas apenas leem e não escrevem (método HTTP não seguro retorna 403 `organization_restricted`; comando na API de serviços levanta `Unavailable` com o mesmo código; escrita no gateway levanta `OrganizationAccessLimited`). `suspended`: apenas rotas isentas para quem pode regularizar; os demais membros veem a página "organização suspensa" (403 e, na API, `organization_suspended`); serviços de apps retornam `Unavailable` e jobs de apps são cancelados. A infraestrutura (`system_context`) continua escrevendo e leituras dos serviços do núcleo continuam funcionando para renderizar as telas de cobrança. |
| `may_use_exempt_routes(organização, pessoa)` | `TenantContextMiddleware` | Define quem acessa rotas isentas durante restrição ou suspensão. Uma rota isenta é registrada com `register_exempt_segment("billing")`, cobrindo `/o/<id>/billing/` e `/api/v1/o/<id>/billing/`. |
| `storage_quota(organização)` | `quota_for` (`skeleton/files/quota.py`) | Retorna a franquia da assinatura (`None` para ilimitada ou `NO_OPINION` para delegar ao setting). A `StorageQuota` da plataforma continua tendo precedência. |

A validação de vínculo ocorre antes de tudo: quem não é membro recebe 404 sem descobrir o estado da organização. O estado de acesso sempre vem da política no servidor, nunca do cliente.

Se uma app de terceiros escrever diretamente em seus próprios modelos fora dos serviços e do gateway, o modo somente leitura não a interceptará nessa camada — embora as superfícies HTTP bloqueiem a requisição antes e o contrato do projeto exija que toda escrita passe pelos serviços. Serviços do núcleo que precisam operar mesmo com acesso limitado (como o de cobrança) declaram `access_exempt = True`.

## Ciclo de vida

O motor de cobrança (`skeleton/billing/engine.py`) opera sobre uma organização dentro da transação chamadora e recebe `now` explicitamente (pelo relógio `engine.clock()`, substituível em testes). Ele nunca chama provedores externos: apenas emite faturas, e a quitação de uma fatura converte os itens pendentes cobrados nela em direitos ativos.

| Momento | O que acontece |
|---|---|
| Organização criada | Com a cobrança ligada, `create_organization` dispara os ganchos de `skeleton/common/organization_hooks.py`. A cobrança cria a conta na moeda `SKELETON_BILLING_DEFAULT_CURRENCY` e a assinatura no **preço padrão** dessa moeda (`Price.is_default`, um por moeda, restrito a plano base). Sem preço padrão, a organização nasce sem assinatura. Instâncias que ativarem a cobrança depois recebem o mesmo tratamento pelo job `billing.cycle`. |
| Trial | Quando há `trial_days`, a assinatura começa em `trialing` com período igual ao trial. O trial vale **apenas uma vez por organização e produto** (itens encerrados formam o histórico). Com `SKELETON_BILLING_TRIAL_REQUIRES_PAYMENT_METHOD`, o trial só inicia com meio de pagamento cadastrado ([#400](https://github.com/alaorneto/skeleton/issues/400)). |
| Início sem trial | A assinatura nasce `incomplete`, com fatura do primeiro período vencendo imediatamente. Enquanto essa fatura inicial não for paga, a assinatura não renova e recusa contratações de itens ou trocas de plano (`SubscriptionNotActive`). Quitar outra fatura com a inicial em aberto não a ativa, e o plano base tem sempre quantidade `1`. Um pagamento atrasado reinicia o período na data da quitação (ajustando o dia-âncora), evitando cobrar meses sem acesso. |
| Contratar app ou pacote | Cobrança imediata com linha `proration` proporcional ao tempo restante do período (em segundos, arredondamento *half-up*). O item fica `pending`, sem direito, até o pagamento. Durante o trial da assinatura ou do próprio add-on, o item nasce ativo sem cobrança e a renovação seguinte cobra proporcionalmente após o fim do trial. Preço e assinatura devem compartilhar moeda e ciclo (`price_cycle_mismatch`). Apps já contratadas ou incluídas no plano são recusadas (`already_subscribed`, `included_in_plan`). |
| Remover app ou pacote | Efetivada ao fim do período, sem estorno. Se o item ainda não foi pago, encerra imediatamente e a fatura aberta que cobrava apenas ele é anulada. |
| Subir de plano | Cobrança imediata do novo preço proporcional, somada na mesma fatura a um crédito (`adjustment`, negativo) pela fração não utilizada do plano anterior. O item antigo encerra quando a fatura é paga. Apenas um upgrade de plano base pode ficar pendente de pagamento por vez (`PlanChangePending`). |
| Descer de plano | Agendada para o fim do período (`Subscription.scheduled_price`). |
| Cancelar | Agendado para o fim do período (`cancel_at_period_end`, reversível com `resume`); durante o trial, o cancelamento é imediato. Nenhum dado é apagado. |
| Renovar | O job periódico `billing.cycle` (de hora em hora, por organização, registrado apenas quando a cobrança está ligada na partida) fecha períodos vencidos: aplica trocas de plano agendadas, encerra itens removidos, cobra os ativos no novo período e inclui linhas de outros módulos (`register_line_source`, como excedente de armazenamento e descontos). Faturas de renovação vencem em `SKELETON_BILLING_PAYMENT_TERMS_DAYS` dias a partir do início do período. Os períodos usam UTC; o dia-âncora vale para todos os meses, ajustando-se ao último dia em meses mais curtos. A linha da assinatura é travada para garantir que duas renovações simultâneas produzam uma única fatura. |
| Fatura com total zero | Quitada imediatamente, sem acionar provedor. |
| Fatura paga | `settle_invoice` converte itens pendentes em direitos e ativa as apps correspondentes (`activate_for_entitlement`, dependências primeiro, com a checagem de disponibilidade e publicação de `app.enabled`). Se uma ativação for recusada, o erro vai para o log sem desfazer o pagamento. |

### Eventos

O subsistema publica `billing.invoice_finalized`, `billing.invoice_paid`, `billing.subscription_changed`, `billing.access_state_changed`, `billing.payment_refunded` e `billing.payment_duplicated`, levando apenas identificadores, códigos e valores ([eventos](events.md#eventos-da-cobrança)). Esses eventos não são enviados a webhooks.

### Vouchers

A plataforma cria vouchers globais (`Voucher`, com termos imutáveis) e cada organização pode resgatar um código uma única vez (`skeleton.billing.vouchers.redeem`, registrado em `VoucherRedemption`).

| Tipo | `value` | Efeito |
|---|---|---|
| `percent` | pontos-base (10000 = 100%) | Desconto percentual nas linhas elegíveis de cada renovação coberta |
| `fixed` | menor unidade de `currency` | Desconto fixo em cada renovação coberta, limitado ao total das linhas elegíveis; exige mesma moeda da assinatura |
| `free_cycles` | não usado | Isenta as linhas elegíveis por `duration_cycles` renovações (exige `duration=repeating`) |
| `trial_extension` | dias (1 a 365) | Estende uma única vez o trial em andamento; fora do trial é recusado (`not_trialing`) |

`duration` define quantas renovações o desconto cobre: `once` (uma), `repeating` (`duration_cycles`) ou `forever`. `applies_to` restringe a códigos de produto específicos (vazio cobre todos). Os descontos entram nas **faturas de renovação** como a última fonte de linhas e nunca ultrapassam as cobranças, impedindo totais negativos. Faturas imediatas (início, contratação e upgrade) não recebem desconto.

O código é armazenado normalizado (sem espaços e em maiúsculas), tornando o resgate insensível a maiúsculas, minúsculas e espaços. Código inexistente, inativo, fora da validade, esgotado (`max_redemptions`, contado sob lock da linha do voucher para garantir um único vencedor no último uso) ou de moeda diferente retornam a mesma resposta (`invalid_voucher`); tentar resgatar o mesmo voucher novamente na organização retorna `already_redeemed`. As tentativas são limitadas a 10 por hora por organização e por pessoa (`too_many_attempts`), controladas no cache compartilhado.

### Cotas

**Armazenamento.** A franquia equivale à soma de `included_storage_bytes × quantity` dos itens ativos, em um pool da organização. O preço do plano base determina o comportamento acima dela:

- `block` (padrão): recusa uploads além da franquia (409 `quota_exceeded`);
- `bill`: permite uploads até `overage_hard_cap_bytes` (ou sem limite se não houver teto) e cobra o excedente na renovação, em blocos de `overage_unit_bytes` a `overage_unit_amount`, pelo **pico** das fotografias diárias do período encerrado (linha `overage`).

Na apuração do excedente, a franquia do período fechado soma todos os itens vigentes naquele intervalo (`period_items`): pacotes removidos ao fim do período, pacotes contratados no meio do período (com franquia cheia) e o plano cessante em um downgrade agendado — excluindo itens `pending`, faturas anuladas sem pagamento e itens substituídos no mesmo período. Para auditar faturas históricas com divergência de excedente, execute `manage.py billing_overage_report --since YYYY-MM-DD` (somente leitura).

Sem assinatura ou com franquia zero, prevalece o padrão da instância (`SKELETON_FILES_ORG_QUOTA_BYTES`), e a `StorageQuota` da plataforma continua tendo precedência ([arquivos](files.md#cota-por-organização)). O job diário `billing.usage_snapshot` (às 23h50 UTC) grava em `UsageSnapshot` os bytes dos anexos ativos da organização e a quebra por app (pelo label da app dona de cada tabela), além de apagar fotografias mais antigas que `SKELETON_BILLING_USAGE_RETENTION_DAYS` (padrão 400).

**Limites de plano.** As métricas e a regra de cálculo pertencem ao núcleo ([limites da organização](organization-limits.md)), e a assinatura atua como uma das fontes que somam ao limite:
- o núcleo registra `members` (vínculos ativos de pessoas; contas de serviço e pessoas externas do portal não entram na conta) e `external_people` (acessos externos ativos);
- apps registram métricas próprias no formato `<app_label>.<nome>` via `skeleton.billing.quota.register_quota_metric(código, contador)`.

O campo `limits` de um preço define o acréscimo de cada métrica. Os limites dos itens ativos somam-se entre si (como pacotes de assentos adicionados ao plano) e ao padrão da instância (`SKELETON_ORGANIZATION_MEMBER_LIMIT`), aos acréscimos das apps em uso e ao bônus da plataforma; um limite fixo definido pela plataforma substitui o cálculo inteiro. Sem padrão da instância, qualquer métrica não limitada por nenhum item permanece ilimitada. Exceder o limite levanta `QuotaLimitExceeded`, traduzido pelas superfícies e pela API de serviços como conflito `quota_exceeded`.

### Medidores de uso

Diferentemente de uma métrica de limite — que mede o estado atual e pode subir ou descer —, um **medidor de uso** contabiliza o consumo acumulado dentro de um período de cobrança (como mensagens enviadas ou documentos assinados). A app registra seus medidores no `ready()` pela superfície pública, sem importar `skeleton.billing`:

```python
from skeleton.service_api.billing import register_usage_meter

register_usage_meter("acme_reminders.messages", label=gettext_lazy("Mensagens"), unit_label=gettext_lazy("mensagem"))
```

O código deve seguir `<app_label>.<nome>`, com ponto obrigatório. Um mesmo código não pode ser registrado como métrica de limite e como medidor simultaneamente (o registro levanta `ValueError`, assim como ao repetir um código com rótulos diferentes). `registered_usage_meters()` lista os medidores registrados.

**Termos no preço.** `Price.usage` mapeia cada medidor aos seus termos e, como todo termo de preço, é imutável no ORM e no guard do banco:

```json
{"acme_reminders.messages": {"included": 1000, "overage": "bill", "unit": 100, "unit_amount": 900, "hard_cap": 20000}}
```

| Chave | Regra |
|---|---|
| `included` | inteiro ≥ 0; franquia por período, multiplicada pela quantidade do item |
| `overage` | `block` (padrão) ou `bill`; `bill` é aceito apenas em preços de `base_plan` ou `app` |
| `unit`, `unit_amount` | obrigatórios e ≥ 1 com `bill`; proibidos com `block`. O excedente é cobrado em blocos de `unit` a `unit_amount` (menor unidade da moeda) |
| `hard_cap` | opcional com `bill`: teto máximo no período (≥ `included`); omitido, não há teto |

O produto `usage_pack` vende franquia recorrente, de modo análogo ao `storage_pack`: seu preço aceita apenas `included`. Medidores não registrados são recusados ao criar o preço (a mensagem informa os códigos registrados); o formulário de preço da plataforma traz o campo **Termos de uso por medidor** (JSON) e preserva o texto digitado em caso de erro.

**Resolução dos termos** de um medidor `M` da app `a` em uma assinatura (`skeleton/billing/usage.py`, função pura `resolve_terms` sobre os itens vivos):

1. A franquia é a soma de `included × quantity` dos itens que citam `M`.
2. A política vem do item `app` de `a` que cobra `M` (`bill`); na ausência dele, do plano base que cobra `M`; caso contrário, `block`.
3. Enquanto a assinatura estiver em trial ou com cancelamento agendado para o fim do período, a política cai para `block`, pois não haverá fatura seguinte para cobrar o excedente.
4. Se nenhum item vivo citar `M`, o medidor é ilimitado naquela organização, mas o consumo continua sendo gravado para relatório e eventual precificação futura.

O limite efetivo corresponde à franquia quando a política é `block`, ao `hard_cap` quando é `bill` (ou sem limite, se não houver teto) e a nenhum quando ilimitado.

**Gravar consumo.** `BillingService().record_usage(actor=..., meter=..., idempotency_key=..., quantity=1)` grava, na mesma transação chamadora, um `UsageEvent` (evidência do consumo) e incrementa o `UsageBalance` do período (contador). Ambas as tabelas são `OperationalRecord` e ficam fora do gateway:

- A chave de idempotência é única por organização e medidor: repetir a mesma chave devolve o recibo original (`created=False`), mesmo sob concorrência.
- O contador do período é travado (`select_for_update`) antes da soma, garantindo um único vencedor na disputa pela última unidade; acima do limite, levanta `Conflict("quota_exceeded")` sem gravar nada.
- O consumo entra no período corrente; entre o fim do período e a execução da renovação, já é contabilizado no período seguinte.
- Com a cobrança desligada ou sem assinatura, nada é gravado e o consumo é permitido (`metered=False`).
- `void_usage` estorna a unidade de um consumo no período aberto (por exemplo, um envio que falhou definitivamente após contabilizado). Repetir o estorno não tem efeito; estornar período fechado levanta `Conflict("usage_period_closed")`; reutilizar uma chave anulada levanta `Conflict("usage_voided")`. `check_usage` verifica disponibilidade sem gravar, e `usage` lista os totais do período de cada medidor das apps ativas.
- A retenção no job diário `billing.usage_snapshot` remove eventos e contadores de períodos iniciados antes de `SKELETON_BILLING_USAGE_RETENTION_DAYS`, preservando sempre o período corrente e o anterior; esse é o único caminho de exclusão física.

**Uso na página.** A página de cobrança da organização (`billing.view`) exibe a seção **Uso neste período** com cada medidor contratado das apps ativas: quantidade usada, franquia, política e barra de progresso (`<progress>`, sem script). Medidores ilimitados não são exibidos, e a seção desaparece se não houver nenhum limitado. `GET /api/v1/o/<org>/billing/` retorna os mesmos dados no campo `usage`, e a visão da organização na administração da plataforma também exibe esses números.

**Faturar o excedente.** Na renovação, a fonte de linhas `usage_lines` (ordem 510: após o armazenamento e antes dos descontos) lê o `UsageBalance` de cada medidor do período encerrado. Os termos seguem os itens vivos no último instante do período, de modo que um pacote contratado no meio dele já conta. Com `bill` e `used > included`, é gerada uma linha `overage` com:
- `quantity = ceil((used - included) / unit)`;
- `amount = quantity × unit_amount`;
- o `app_label` extraído do prefixo e o código do medidor em `InvoiceLine.metric` (imutável, como toda a linha);
- descrição contendo o rótulo e o código do medidor.

Múltiplos medidores geram linhas separadas em ordem alfabética do código. Períodos em trial ou sob `block` não geram cobrança, e a trava da assinatura evita duplicação em caso de repetição da renovação. Descontos de voucher continuam limitados ao total das cobranças, incluindo linhas de uso.

**Limitação: o último período de uma assinatura cancelada não é faturado.** Com o cancelamento agendado para o fim do período, a renovação encerra a assinatura sem emitir fatura e nenhuma fonte de linhas é executada — nem o excedente de uso nem o de armazenamento daquele período são cobrados. Por essa razão, durante o cancelamento agendado a política de uso passa para `block` (o consumo para na franquia); o armazenamento não tem essa trava e pode ultrapassar a franquia até o teto sem cobrança no último período.

**Regra para as apps.** Registre o consumo na mesma transação que enfileira o efeito externo (o job de envio), usando como chave de idempotência o identificador do registro de negócio (como o `sysid` da mensagem) — nunca telefone, e-mail ou nome. Caso o efeito falhe em definitivo, o job chama `void_usage`. Em testes, `skeleton.testing.billing.subscribe_with_usage(organization, {meter: termos}, now=...)` cria e quita uma assinatura com os termos desejados.

### Inadimplência

O estado de acesso da organização é sempre **derivado**, nunca preenchido manualmente: ele resulta da fatura em aberto mais antiga, dos prazos configurados na instância e de eventual prorrogação concedida pela plataforma. O job `billing.cycle` (de hora em hora) recalcula o estado após as renovações, e cada quitação de fatura o atualiza imediatamente (`skeleton/billing/dunning.py`).

| Fatura em aberto mais antiga | Estado | Efeito ([ganchos](#ganchos-de-direito)) |
|---|---|---|
| nenhuma vencida | `active` | Acesso normal |
| vencida há menos de `SKELETON_BILLING_GRACE_DAYS` (7) dias completos | `past_due` | Acesso normal, com aviso |
| menos de `SKELETON_BILLING_SUSPEND_AFTER_DAYS` (30) | `restricted` | Somente leitura para pessoas |
| a partir desse prazo, ou fatura `uncollectible` ou `refunded` | `suspended` | Apenas rotas de cobrança, para o administrador |

Regras complementares:

- **Pagamento em compensação:** um pagamento `pending` criado nos últimos `SKELETON_BILLING_PENDING_PAYMENT_HOLD_DAYS` (3) dias segura o estado em `past_due`, evitando bloquear quem já pagou via boleto ou Pix em trânsito.
- **Alerta de suspensão:** uma organização em `suspended` gera um alerta diário na caixa da plataforma para quem possui `billing.operate` ([alertas da plataforma](notifications.md#alertas-da-plataforma)).
- **Estorno, chargeback e pagamento duplicado:** um estorno integral ou chargeback sobre uma fatura paga altera seu status para `refunded` (zerando `amount_paid` e preservando todas as `InvoiceLine`), executa `refresh_access()` (suspendendo o acesso como em `uncollectible`, respeitando `access_override_until`), publica `billing.payment_refunded` e envia o alerta `platform.billing_payment_refunded`. Um estorno parcial é registrado no pagamento, publica `billing.payment_refunded` (com o valor estornado em `amount`) e envia o alerta `platform.billing_payment_refunded` (`partial=True`), sem suspender o acesso. Um segundo pagamento `succeeded` na mesma fatura já paga publica `billing.payment_duplicated` e envia o alerta `platform.billing_payment_duplicated`.
- **Prorrogação pela plataforma:** `access_override_until` (com motivo obrigatório) mantém a organização em `active` até expirar.
- **Cancelamento por inadimplência:** após `SKELETON_BILLING_CANCEL_AFTER_DAYS` (60), a assinatura é cancelada e as faturas vencidas passam a `uncollectible`. A organização permanece suspensa e **nenhum dado é apagado**. Dar baixa manual na fatura incobrável a quita e reativa o acesso.
- **Retentativas automáticas:** assinaturas `auto_charge` disparam novas tentativas nos dias configurados em `SKELETON_BILLING_RETRY_SCHEDULE_DAYS` (`1,3,5`) após o vencimento, usando os cobradores registrados pelo provedor (`dunning.register_collector`, [#400](https://github.com/alaorneto/skeleton/issues/400)).
- **Evento de mudança:** toda transição emite `billing.access_state_changed` com o novo `access_state`.

**Avisos.** As notificações de cobrança são obrigatórias, entregues por e-mail e na aplicação ao papel de administrador da organização, sem tabela-alvo (a entrega revalida se a pessoa ainda administra a organização). Cada etapa é disparada uma única vez (`dedupe_key`), mesmo que o job execute novamente:
- `billing.trial_ending` (três dias antes do fim do trial);
- `billing.invoice_issued`;
- `billing.invoice_overdue`;
- `billing.payment_failed`;
- `billing.payment_received`;
- `billing.access_restricted`;
- `billing.access_suspended`.

O aviso de uso `billing.usage_threshold` não é obrigatório: é disparado ao cruzar 80% e 100% do limite efetivo de um medidor (a franquia em `block`, ou o teto em `bill`; sem teto, não há aviso), uma vez por medidor, período e limiar (`usage:<medidor>:<início do período>:<80|100>`). Como o cruzamento é verificado sob a trava do contador, consumos concorrentes não geram duplicidade. O payload carrega apenas código do medidor, `used`, `limit`, `threshold` e início do período — nunca chave de idempotência nem dados pessoais.

### Impostos

O núcleo não calcula impostos. Projetos que precisem de tributação registram uma calculadora em `ready()` via `skeleton.billing.tax.register_tax_calculator(...)`, que recebe a conta e as linhas da fatura e devolve o imposto na menor unidade da moeda. A emissão de nota fiscal permanece fora do núcleo e pode ser acionada por um consumidor do evento `billing.invoice_paid`.

## Provedores de pagamento

Como não existe um padrão aberto de assinaturas entre provedores, o núcleo gerencia a assinatura e delega ao provedor apenas operações comuns (`skeleton/billing/providers/base.py`, protocolo `PaymentProvider`): criar cliente, abrir um **checkout hospedado** para uma fatura (sem que dados de cartão passem pelo servidor), cobrar um meio salvo, **consultar um pagamento**, estornar e **verificar webhooks**.

Os adaptadores são registrados em código com `register_provider`. Toda saída HTTP passa obrigatoriamente por `skeleton.common.outbound_http`, com hosts dos provedores fixados como constantes em código e respostas limitadas a 1 MiB (`max_response_bytes`). SDKs oficiais não são usados porque contornariam a proteção anti-SSRF e introduziriam dependências externas.

**Contas de provedor.** A tabela global `ProviderAccount` armazena a chave da API e o segredo de webhook cifrados com `BILLING_ENCRYPTION_KEYS` (redigidos na auditoria), as moedas aceitas (vazio aceita todas), o modo (`test` ou `live`) e a marcação de conta padrão. A `BillingAccount` guarda apenas o provedor, o identificador do cliente nele e o meio salvo (token do provedor, bandeira e últimos quatro dígitos — nunca o número do cartão).

| Fluxo | Como funciona |
|---|---|
| Checkout | `payments.create_checkout(fatura, return_url=...)` grava um `CheckoutAttempt`, solicita a página hospedada ao provedor usando o `sysid` da tentativa como chave de idempotência e armazena o link retornado (dado sensível). Nenhuma linha fica travada durante a chamada externa. A referência enviada ao provedor segue o formato `<sysid da organização>:<sysid da fatura>`. |
| Cobrança automática | Faturas de renovação de assinaturas `auto_charge` enfileiram `billing.collect`, que cobra o meio salvo com a chave `collect:<fatura>:<tentativa>`, impedindo cobrança dupla caso o worker caia após a chamada. As retentativas da [inadimplência](#inadimplência) usam a mesma tarefa com o número da tentativa seguinte. Recusas disparam `billing.payment_failed` e agendam a próxima tentativa. |
| Webhook | `POST /billing/webhooks/<provedor>/<sysid da conta>/` fica fora de `/o/` e `/api/`, sem sessão nem CSRF. Limita corpo (256 KiB) e taxa por conta, valida assinatura ou token em tempo constante, grava `ProviderEvent` uma única vez por `(conta, id do evento)` (repetições respondem 200 sem efeito), enfileira `billing.process_event` e responde 200. Webhooks rejeitados respondem 400 sem efeito colateral. |
| Processar o aviso | O job **consulta o pagamento diretamente no provedor** com a chave da plataforma, pois o corpo do webhook não é confiável. A fatura só é marcada como paga se o pagamento consultado corresponder àquela fatura, na mesma moeda e pelo valor em aberto; divergências registram `amount_mismatch` sem alterar o estado contábil. |
| Conciliação | `billing.reconcile` (a cada 30 minutos, por organização) consulta pagamentos pendentes há mais de 15 minutos usando a `ProviderAccount` vinculada à conta de cobrança (recorrendo à conta padrão do provedor apenas quando não há vínculo), cobrindo eventuais webhooks perdidos. |

### Stripe

Implementado em `skeleton/billing/providers/stripe.py` (código `stripe`). Cadastre a conta de provedor com a chave secreta (`sk_test_…` em teste ou `sk_live_…` em produção) e o segredo do endpoint (`whsec_…`). No painel da Stripe, configure o webhook `https://<origem pública>/billing/webhooks/stripe/<sysid da conta>/` para os eventos `checkout.session.completed`, `payment_intent.succeeded` e `payment_intent.payment_failed`.

- **Checkout:** Checkout Session com `mode=payment`, um item no valor devido, a referência em `client_reference_id` e nos metadados do PaymentIntent, e `setup_future_usage=off_session` para salvar o cartão visando renovações futuras. Os meios aceitos (cartão, boleto, Pix) refletem os métodos habilitados na conta Stripe.
- **Cobrança automática:** PaymentIntent com `off_session=true, confirm=true` usando o meio salvo. Recusa (402) ou exigência de autenticação (`requires_action`) contam como falha, exigindo pagamento via checkout.
- **Consulta e estorno:** `GET /v1/payment_intents/<id>` (referências `cs_` passam antes pela Checkout Session) e leitura do meio de pagamento para bandeira e últimos dígitos. Estorno via `POST /v1/refunds`.
- **Webhook:** cabeçalho `Stripe-Signature` com `t` e `v1`, HMAC-SHA256 de `"<t>.<corpo>"` e tolerância de cinco minutos; qualquer `v1` válido é aceito, suportando rotação de segredo.
- **Idempotência e versão:** todo POST envia `Idempotency-Key`, e `Stripe-Version` é fixado em `2024-06-20` para evitar mudanças inesperadas de resposta após atualizações no painel.

### Mercado Pago

Implementado em `skeleton/billing/providers/mercadopago.py` (código `mercadopago`). A chave é o access token (`TEST-…` ou `APP_USR-…`), e o segredo é a assinatura secreta das notificações obtida no painel de integrações. Cadastre a URL de notificação `https://<origem pública>/billing/webhooks/mercadopago/<sysid da conta>/` com o evento de pagamentos. Aceita as moedas do Mercado Pago (ARS, BRL, CLP, COP, MXN, PEN, UYU).

- **Checkout:** Checkout Pro (`POST /checkout/preferences`) com um item, referência em `external_reference` e `back_urls`, permitindo pagamento por cartão, Pix ou boleto na página do Mercado Pago (`sandbox_init_point` em teste). Os valores são enviados em formato decimal.
- **Cobrança automática:** **não oferecida**. Cartões salvos no Mercado Pago exigem código de segurança a cada cobrança fora das assinaturas nativas do provedor, que o núcleo não utiliza. Cada fatura é paga por checkout, Pix ou boleto, e essa capacidade fica desabilitada.
- **Consulta e estorno:** `GET /v1/payments/<id>`: `approved` quita a fatura, `rejected` e `cancelled` marcam falha, `refunded` e `charged_back` registram estorno, e os demais status permanecem pendentes (como Pix ou boleto aguardando compensação). A notificação carrega apenas o identificador. Estorno via `POST /v1/payments/<id>/refunds`.
- **Webhook:** cabeçalho `x-signature` com `ts` e `v1`, HMAC-SHA256 do manifesto `id:<data.id>;request-id:<x-request-id>;ts:<ts>;` (usando `data.id` da query string em minúsculas ou do corpo) e tolerância de cinco minutos.
- **Idempotência:** todo POST envia `X-Idempotency-Key`.

### Asaas

Implementado em `skeleton/billing/providers/asaas.py` (código `asaas`), restrito a BRL: contas com outra moeda são recusadas já no cadastro, e contas sem moeda especificada assumem BRL. A chave é a API key (`$aact_…`), e o modo direciona chamadas para `api-sandbox.asaas.com` ou `api.asaas.com`. No painel, cadastre o webhook `https://<origem pública>/billing/webhooks/asaas/<sysid da conta>/` com os eventos de cobrança e um **token de autenticação**, que atua como segredo de webhook da conta. O cliente precisa ter CPF ou CNPJ informado no documento da conta de cobrança.

- **Checkout:** `POST /v3/payments` com `billingType=UNDEFINED` (o pagador escolhe Pix, boleto ou cartão em `invoiceUrl`) e referência em `externalReference`. Como o Asaas não possui cabeçalho de idempotência, o adaptador busca antes uma cobrança aberta com a mesma referência e a reaproveita, evitando duplicidade em novas tentativas.
- **Cobrança automática:** `billingType=CREDIT_CARD` com o `creditCardToken` obtido no primeiro pagamento por cartão e `externalReference` estável por fatura e tentativa (`<org>:<fatura>:<hash da tentativa>`). Antes de criar nova cobrança, consulta cobranças existentes em `PENDING`, `CONFIRMED`, `RECEIVED` ou `RECEIVED_IN_CASH` para impedir cobrança dupla após quedas de rede. Atenção ao testar no sandbox: algumas contas Asaas exigem `remoteIp` na cobrança tokenizada.
- **Consulta e estorno:** `GET /v3/payments/<id>`: `RECEIVED`, `CONFIRMED` e `RECEIVED_IN_CASH` quitam a fatura; `REFUNDED`, `REFUND_REQUESTED`, `REFUND_IN_PROGRESS`, `PARTIALLY_REFUNDED` e `CHARGEBACK_*` registram estorno; `OVERDUE` permanece pendente (quem decide o atraso é a régua do núcleo). Estorno via `POST /v3/payments/<id>/refund`.
- **Webhook:** o Asaas envia o token no cabeçalho `asaas-access-token`, sem HMAC. O token é comparado em tempo constante e, como em todos os provedores, o pagamento é consultado antes de qualquer alteração, de modo que corpos forjados não têm efeito. Quando o payload não inclui `id`, o identificador do evento combina tipo de evento, pagamento, status e `dateCreated` para não confundir transições distintas. Em todos os provedores, segredos de webhook vazios são recusados e entregas simultâneas do mesmo evento respondem 200 de forma idempotente.

Em testes, `skeleton.testing.billing.use_fake_provider()` registra um provedor em memória, determinístico e sem rede, acompanhado de uma conta padrão. Com a cobrança ligada e sem `SKELETON_PUBLIC_URL` válido, `check --deploy` emite o aviso `skeleton.W037`, pois o provedor não teria endereço para enviar webhooks nem para retornar após o checkout.

## Superfícies

- **Capacidades:** `billing.view` (consultar assinatura e faturas) e `billing.manage` (contratar, pagar, cancelar, resgatar voucher e editar dados de cobrança), atribuídas ao papel de administrador da organização. Rotas isentas em modo suspenso exigem `billing.manage`; avisos são enviados a quem tem `billing.view`.
- **Página `/o/<org>/billing/`:** exibe estado de acesso, assinatura e itens (com ações de remover, cancelar ou retomar), faturas com o botão **Pagar** (redireciona para a página hospedada do provedor fora do lock da organização, evitando travar linhas durante a chamada externa), armazenamento com quebra por app da última fotografia, ofertas com **Contratar** (exigindo aceite dos termos, cuja versão fica registrada na conta), resgate de voucher e dados de cobrança. Permanece acessível mesmo sob acesso limitado.
- **Apps da organização (`/o/<org>/apps/`):** apps oferecidas pela plataforma mas ainda não contratadas aparecem como "Não contratada", com o botão **Contratar** direcionando para a cobrança (`ApplicationState.contract_required`).
- **API REST:** `/api/v1/o/<org>/billing/` e `/api/v1/o/<org>/billing/<ação>/` ([API](api.md#recursos)).
- **API de serviços:** `BillingService` ([API de serviços](service-api.md#billingservice)).

## Operação da plataforma

A administração da cobrança fica em `/platform/billing/` (`skeleton/web/platform/billing_views.py` sobre `skeleton/billing/platform.py`):

- **Catálogo:** gerencia produtos, preços (termos imutáveis: para mudar valor, cria-se novo preço e desativa-se o antigo; um preço de plano base pode ser marcado como **padrão** da moeda para novas organizações), vouchers (criar, ativar e desativar) e contas de provedor (segredos nunca são exibidos; campo vazio mantém o segredo atual; salvar exige autenticação recente; a tela mostra a URL de webhook de cada conta). Tanto o administrador da plataforma quanto o papel limitado **Operador de cobrança** (`billing`, capacidade `billing.operate`) operam o catálogo.
- **Conta de uma organização (`/platform/organizations/<id>/billing/`, restrito ao administrador):** exibe assinatura, faturas e pagamentos e oferece quatro ações, todas com **motivo obrigatório** gravado junto ao registro alterado:
  - **baixa manual:** registra um pagamento `offline` com a nota e quita a fatura, mesmo se incobrável;
  - **anular:** cancela a fatura gravando o motivo em `Invoice.status_note`;
  - **prorrogação de acesso:** mantém o acesso ativo até uma data definida (`access_override_reason`);
  - **cortesia:** contrata o preço desejado, aplica um voucher privado de 100% permanente para as renovações e dá baixa manual nas faturas imediatas com a nota.

  Essas ações abrem `system_context` da organização com o motivo `platform:billing`, permitido apenas ao administrador da plataforma durante uma requisição.
- A página da organização na administração da plataforma traz o atalho **Abrir a cobrança**.

Relatórios financeiros (como receita recorrente e taxa de cancelamento) ficam previstos como evolução futura.
