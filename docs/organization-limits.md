# Limites da organização

Uma organização pode ter um limite de membros, de pessoas externas do portal ou de qualquer métrica que uma app registre. O limite é calculado pelo núcleo, com ou sem a [cobrança](billing.md) ligada, e conferido no servidor em todo caminho que cria, restaura ou reativa o que ele conta. É o [epic #694](https://github.com/alaorneto/skeleton/issues/694); esta página descreve o motor ([#695](https://github.com/alaorneto/skeleton/issues/695)).

## A regra

```
base = padrão da instância para a métrica (vazio: sem padrão)
contribuições = o que cada fonte acrescenta para a organização
se não há base nem contribuição: ilimitado
senão: limite = (base ou 0) + soma das contribuições
```

Uma instância que não define o padrão e não tem nenhuma fonte com valor continua sem limite, como antes. Com a cobrança ligada e sem padrão, o limite é a soma dos itens da assinatura, como em [#398](https://github.com/alaorneto/skeleton/issues/398).

Exemplo: padrão 1; a assinatura acrescenta 4; o limite é 5.

| Peça | Onde |
|---|---|
| Registro das métricas e das fontes, e a regra pura (`combine`) | `skeleton/common/limits.py` (sem import de `skeleton.*`) |
| Métricas do núcleo, padrão da instância, contagem e recusa (`limit_status`, `check_limit`) | `skeleton/system/limits.py`, registrado em `SystemConfig.ready` |
| Fonte `billing`: `Price.limits × quantity` dos itens vivos da assinatura | `skeleton/billing/quota.py` (`plan_source`) |
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

## Onde é conferido

O limite de `members` é conferido, sob o `organization_lock` da organização, antes de gravar o vínculo:

- `admit_member`: convite, pedido de entrada aprovado, link de entrada, domínio verificado e provisionamento JIT de SSO;
- `create_membership`: vínculo novo ativo e restauração de um vínculo excluído como ativo;
- `update_membership`: reativação.

Como todos esses caminhos seguram o lock da organização, duas admissões disputando a última vaga têm um vencedor; a outra recebe `QuotaLimitExceeded`, que as superfícies e a API de serviços tratam como conflito `quota_exceeded`. `external_people` é conferido ao aceitar um convite externo.

## Redução do limite

Um limite menor nunca remove ninguém. Quando o uso passa do limite (uma app desligada, um item removido, um padrão menor), a organização fica **acima do limite** (`LimitStatus.over_limit`) e não admite ninguém até voltar ao limite.

## Leitura

`skeleton.system.limits.limit_status(métrica)` devolve, para a organização do contexto, `used`, `limit` (`None`: ilimitado), `base`, `contributions` (fonte e valor de cada uma), `over_limit` e `remaining`. Uma métrica desconhecida é `ValueError`; fora do contexto de uma organização, `MissingTenantContext`. `check_limit` chamado para outra organização que não a do contexto não confere nada: as linhas dela não são legíveis ali.
