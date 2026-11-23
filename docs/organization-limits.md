# Limites da organização

Uma organização pode ter um limite de membros, de pessoas externas do portal ou de qualquer métrica que uma app registre. O limite é calculado pelo núcleo, com ou sem a [cobrança](billing.md) ligada, e conferido no servidor em todo caminho que cria, restaura ou reativa o que ele conta. É o [epic #694](https://github.com/alaorneto/skeleton/issues/694): o motor ([#695](https://github.com/alaorneto/skeleton/issues/695)), o acréscimo por app ([#696](https://github.com/alaorneto/skeleton/issues/696)), o ajuste da plataforma ([#697](https://github.com/alaorneto/skeleton/issues/697)), o uso visível e os convites que reservam vaga ([#698](https://github.com/alaorneto/skeleton/issues/698)) e a atribuição de apps pela plataforma ([#699](https://github.com/alaorneto/skeleton/issues/699)).

## A regra

```
base = padrão da instância para a métrica (vazio: sem padrão)
contribuições = o que cada fonte acrescenta para a organização
se não há base nem contribuição: ilimitado
senão: limite = (base ou 0) + soma das contribuições
se a plataforma deu um limite fixo à organização: limite = o valor fixo (vazio: ilimitado)
```

Uma instância que não define o padrão e não tem nenhuma fonte com valor continua sem limite, como antes. Com a cobrança ligada e sem padrão, o limite é a soma dos itens da assinatura, como em [#398](https://github.com/alaorneto/skeleton/issues/398).

Exemplo: padrão 1; a app X acrescenta 2 e a app Y acrescenta 7; com as duas em uso o limite é 10, só com X é 3, só com Y é 8. Com a assinatura acrescentando mais 4, 14.

| Peça | Onde |
|---|---|
| Registro das métricas e das fontes, e a regra pura (`combine`) | `skeleton/common/limits.py` (sem import de `skeleton.*`) |
| Métricas do núcleo, padrão da instância, contagem e recusa (`limit_status`, `check_limit`) | `skeleton/system/limits.py`, registrado em `SystemConfig.ready` |
| Fonte `apps` (ordem 100): `AppAvailability.limits` de cada app **em uso** na organização | `skeleton/system/limits.py` (`app_limit_contributions`) |
| Fonte `billing` (ordem 200): `Price.limits × quantity` dos itens vivos da assinatura | `skeleton/billing/quota.py` (`plan_source`) |
| Fonte `platform` (ordem 300) e limite fixo: `OrganizationLimitOverride` | `skeleton/system/limits.py` (`platform_bonus`, `live_override`) |
| Gancho chamado pelos pacotes de domínio | `skeleton.common.entitlements.check_limit` |

## Métricas

| Métrica | Conta | Padrão da instância |
|---|---|---|
| `members` | Vínculos ativos de **pessoas** na organização. Contas de serviço não ocupam vaga (são integrações); pessoas externas do portal têm métrica própria | `SKELETON_ORGANIZATION_MEMBER_LIMIT` |
| `external_people` | Acessos externos ativos do [portal](portal.md) | Nenhum |
| `<app_label>.<nome>` | O que o contador da app devolver | Nenhum |

Uma app registra a sua métrica no `ready()` com `skeleton.service_api.billing.register_quota_metric("<app_label>.<nome>", contador)`, em que `contador(organization_id)` devolve a quantidade atual no contexto da organização. Um código é métrica de limite ou [medidor de uso](billing.md#medidores-de-uso), nunca os dois. Para conferir antes de criar, a app chama `BillingService().check_quota(actor=..., metric=..., increment=1)`, que responde `Conflict("quota_exceeded")` acima do limite.

## Configuração

| Setting | Padrão | Efeito |
|---|---|---|
| `SKELETON_ORGANIZATION_MEMBER_LIMIT` | vazio | Limite padrão de membros de toda organização. Vazio ou 0: sem padrão. No catálogo da [instância](deployment.md), editável em `/platform/instance/` |

Uma instância que vende um produto em níveis define o padrão (por exemplo 1) e deixa as fontes acrescentarem vagas.

## Acréscimo por app

O administrador da plataforma define, em `/platform/apps/<app>/availability/` (seção "Acréscimos por organização"), quanto cada app acrescenta a cada métrica registrada: `{"members": 2}`. É uma decisão comercial da instância, não do código da app: a mesma app pode valer diferente em outra instância, e o manifesto não declara acréscimo. O valor fica em `AppAvailability.limits` (inteiros de 1 a 100000; 0 ou vazio não acrescenta) e só é gravado por `skeleton.system.availability.set_app_limits(app_label, *, user, limits, revision)`, com CAS na linha da disponibilidade (que precisa existir antes) e auditoria.

O acréscimo vale em cada organização onde a app está **em uso**: instalada, disponível pela plataforma, contratada quando a cobrança está ligada, ativada pela organização e com todas as dependências em uso. Uma app desativada, retirada pela plataforma, sem direito na cobrança ou com uma dependência fechada não acrescenta nada. A mudança do valor vale na requisição seguinte de todas as organizações. Sem `SKELETON_ORGANIZATION_MEMBER_LIMIT`, o acréscimo de `members` vira o limite inteiro, e a página avisa disso.

## Ajuste da plataforma

Em `/platform/organizations/<id>/limits/`, o administrador da plataforma vê, por métrica, o padrão, cada acréscimo (apps, cobrança, bônus), o ajuste, o limite e o uso, e dá a uma organização:

- um **bônus**, que soma ao limite calculado (fonte `platform`);
- um **limite fixo**, que substitui o cálculo inteiro; vazio é ilimitado.

Cada ajuste tem motivo obrigatório e validade opcional; vencido, deixa de valer sem job nenhum. Há um ajuste por organização e métrica (`OrganizationLimitOverride`, tabela global): remover é exclusão lógica e definir de novo restaura a mesma linha. Os serviços são `set_limit_override` e `remove_limit_override` de `skeleton.system.limits`, com CAS e só para o administrador da plataforma; `limit_breakdown(*, actor, organization)` lê o detalhamento no contexto de inspeção somente leitura da plataforma.

## Atribuir apps pela plataforma

Na página da organização (`/platform/organizations/<id>/`), a seção "Aplicativos" mostra o acréscimo de membros de cada app e os botões **Atribuir** e **Retirar**:

- **Atribuir** (`skeleton.system.applications.assign_app`) concede a app e as dependências que ainda não estão disponíveis para a organização (no modo `selected`) e as ativa, dependências primeiro, com as mesmas verificações da ativação pela organização. Uma app paga sem direito na cobrança é recusada ("App paga: use a cobrança da organização") e nada é gravado. Uma app já em uso não muda.
- **Retirar** (`unassign_app`) desativa a app, guardando a configuração e os dados, e, no modo `selected`, revoga a concessão. Uma app dependente ainda ativa bloqueia, e a mensagem diz qual.

Um **pacote de apps** (#714) faz o mesmo para um nível inteiro: atribuir o pacote ativa as apps dele e desativa as do pacote anterior que ele não mantém, com a pré-visualização do limite de membros ([instância de produto](product-instances.md#4-pacotes-de-apps-e-com-cobrança-preços-e-ofertas)). As duas abrem o contexto de sistema `platform:apps` só para aquela organização, a partir da requisição do administrador da plataforma. Depois da ação, a página mostra a mudança do limite de membros ("Limite de membros: 3 → 10").

## Onde é conferido

O limite de `members` é conferido, sob o `organization_lock` da organização, antes de gravar o vínculo:

- `admit_member`: convite, pedido de entrada aprovado, link de entrada, domínio verificado e provisionamento JIT de SSO;
- `create_membership`: vínculo novo ativo e restauração de um vínculo excluído como ativo;
- `update_membership`: reativação.

Como todos esses caminhos seguram o lock da organização, duas admissões disputando a última vaga têm um vencedor; a outra recebe `QuotaLimitExceeded`, que as superfícies e a API de serviços tratam como conflito `quota_exceeded`. `external_people` é conferido ao aceitar um convite externo.

## O que a organização vê

Quem tem `members.view` ou `members.manage` vê o uso de cada métrica limitada:

- nas páginas de vínculos, de convites e de pedidos de ingresso, uma faixa "2 de 3 membros", com os convites pendentes que reservam vaga; sem vaga, o botão de convidar fica desabilitado com a explicação, e acima do limite aparece o aviso de que novas entradas estão bloqueadas;
- na API de serviços, `OrganizationService().limits(actor=...)` (`LimitInfo` com `metric`, `used`, `limit`, `reserved`, `over_limit`);
- na API REST, `GET /api/v1/o/<org>/limits/` ([API](api.md)).

Uma métrica ilimitada não aparece.

### Convites reservam vaga

Emitir um convite confere, sob o lock da organização, que pessoas ativas + convites pendentes de outros emails + este cabem no limite. Pendente é o convite não aceito, não revogado e não vencido; um email conta uma vez, e reenviar o convite não reserva outra vaga. Convidar no limite é recusado com `quota_exceeded` ("A organização atingiu o limite de N membros."). O convite aceito ocupa a vaga que reservou, sem contar duas vezes; um convite vencido libera a vaga.

### Recusas nos caminhos de entrada

Uma entrada recusada pelo limite diz o motivo: "Esta organização não tem vagas no momento. Fale com o administrador." A aprovação de um pedido de ingresso (página e API) responde 409 `quota_exceeded` e o pedido continua pendente; o link de entrada e o aceite de convite mostram a mensagem com 409. O provisionamento JIT de SSO e a entrada automática por domínio verificado recusam de forma neutra: a pessoa não entra, nenhum dado da organização aparece, e o log registra `membership.limit_reached` com `reason_code` (`sso`, `domain` ou `invitation`) e o `sysid` da organização, sem email nem nome.

### Aviso ao chegar no limite

Quando uma admissão ocupa a última vaga, quem tem `members.manage` recebe a notificação `organization.member_limit_reached` ([notificações](notifications.md)), uma por dia e valor de limite, com só `limit` e `used` no conteúdo.

## Redução do limite

Um limite menor nunca remove ninguém. Quando o uso passa do limite (uma app desligada, um item removido, um padrão menor), a organização fica **acima do limite** (`LimitStatus.over_limit`) e não admite ninguém até voltar ao limite.

## Leitura

`skeleton.system.limits.limit_status(métrica)` devolve, para a organização do contexto, `used`, `limit` (`None`: ilimitado), `base`, `contributions` (fonte e valor de cada uma), `over_limit` e `remaining`. Uma métrica desconhecida é `ValueError`; fora do contexto de uma organização, `MissingTenantContext`. `check_limit` chamado para outra organização que não a do contexto não confere nada: as linhas dela não são legíveis ali.
