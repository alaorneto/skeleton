# Limites da organização

Uma organização pode ter limites de membros, de pessoas externas do portal ou de qualquer métrica registrada por uma app. Esses limites são calculados pelo núcleo — com ou sem a [cobrança](billing.md) ligada — e validados no servidor em todo caminho que cria, restaura ou reativa o recurso contabilizado. Esse subsistema corresponde ao [epic #694](https://github.com/alaorneto/skeleton/issues/694): motor de cálculo ([#695](https://github.com/alaorneto/skeleton/issues/695)), acréscimo por app ([#696](https://github.com/alaorneto/skeleton/issues/696)), ajuste da plataforma ([#697](https://github.com/alaorneto/skeleton/issues/697)), visibilidade do uso e reserva de vaga por convites ([#698](https://github.com/alaorneto/skeleton/issues/698)) e atribuição de apps pela plataforma ([#699](https://github.com/alaorneto/skeleton/issues/699)).

## A regra

```
base = padrão da instância para a métrica (vazio: sem padrão)
contribuições = o que cada fonte acrescenta para a organização
se não há base nem contribuição: ilimitado
senão: limite = (base ou 0) + soma das contribuições
se a plataforma deu um limite fixo à organização: limite = o valor fixo (vazio: ilimitado)
```

Instâncias sem padrão definido nem fontes com valor continuam sem limite. Com a cobrança ligada e sem padrão na instância, o limite corresponde à soma dos itens ativos da assinatura, como em [#398](https://github.com/alaorneto/skeleton/issues/398).

Por exemplo: com base 1, app X somando 2 e app Y somando 7, o limite é 10 quando ambas estão em uso, 3 apenas com X e 8 apenas com Y. Se a assinatura acrescentar mais 4, o limite chega a 14.

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
| `members` | Vínculos ativos de **pessoas** na organização. Contas de serviço não ocupam vaga (são integrações), e pessoas externas do portal têm métrica própria. | `SKELETON_ORGANIZATION_MEMBER_LIMIT` |
| `external_people` | Acessos externos ativos do [portal](portal.md) | Nenhum |
| `<app_label>.<nome>` | Quantidade retornada pelo contador da app | Nenhum |

Para registrar uma métrica própria, a app chama `skeleton.service_api.billing.register_quota_metric("<app_label>.<nome>", contador)` em `ready()`, onde `contador(organization_id)` devolve a contagem atual no contexto da organização. Um código deve ser métrica de limite ou [medidor de uso](billing.md#medidores-de-uso), nunca ambos. Antes de criar um recurso, a app valida a disponibilidade chamando `BillingService().check_quota(actor=..., metric=..., increment=1)`, que levanta `Conflict("quota_exceeded")` se o limite for ultrapassado.

## Configuração

| Setting | Padrão | Efeito |
|---|---|---|
| `SKELETON_ORGANIZATION_MEMBER_LIMIT` | vazio | Limite padrão de membros para todas as organizações. Vazio ou `0` significa sem padrão. Faz parte do catálogo da [instância](deployment.md) e pode ser editado em `/platform/instance/`. |

Instâncias que vendem um produto em níveis costumam definir um padrão inicial (por exemplo, `1`) e deixar que as fontes acrescentem vagas adicionais.

## Acréscimo por app

Em `/platform/apps/<app>/availability/` (seção "Acréscimos por organização"), o administrador da plataforma define quanto cada app acrescenta a cada métrica registrada — por exemplo, `{"members": 2}`. Trata-se de uma decisão comercial da instância, não do código da app: a mesma app pode conceder acréscimos diferentes em outra instância, e o manifesto não declara esses valores.

O valor é armazenado em `AppAvailability.limits` (inteiros de 1 a 100000; `0` ou vazio não acrescenta nada) e gravado exclusivamente por `skeleton.system.availability.set_app_limits(app_label, *, user, limits, revision)`, com CAS na linha de disponibilidade (que precisa existir previamente) e registro em auditoria.

O acréscimo só se aplica às organizações onde a app está efetivamente **em uso**: instalada, disponibilizada pela plataforma, contratada (quando a cobrança está ligada), ativada pela organização e com todas as dependências em uso. Apps desativadas, retiradas pela plataforma, sem direito na cobrança ou com dependência fechada não contribuem para o limite. Qualquer alteração passa a valer na requisição seguinte de todas as organizações. Quando `SKELETON_ORGANIZATION_MEMBER_LIMIT` não está definido, o acréscimo de `members` torna-se o limite total, e a tela alerta sobre isso.

## Ajuste da plataforma

Em `/platform/organizations/<id>/limits/`, o administrador da plataforma visualiza, para cada métrica, o padrão da instância, cada acréscimo (apps, cobrança e bônus), o ajuste vigente, o limite efetivo e o uso atual. Nessa tela é possível atribuir à organização:

- um **bônus**, somado ao limite calculado (fonte `platform`);
- um **limite fixo**, que substitui todo o cálculo (vazio torna a métrica ilimitada).

Todo ajuste exige motivo obrigatório e aceita validade opcional — ao vencer, deixa de valer automaticamente, sem necessidade de job. Existe apenas um ajuste por organização e métrica (`OrganizationLimitOverride`, tabela global): remover aplica exclusão lógica e definir novamente restaura a mesma linha. Os serviços `set_limit_override` e `remove_limit_override` de `skeleton.system.limits` operam com CAS e são restritos ao administrador da plataforma; `limit_breakdown(*, actor, organization)` lê o detalhamento no contexto de inspeção somente leitura da plataforma.

## Atribuir apps pela plataforma

Na página da organização (`/platform/organizations/<id>/`), a seção "Aplicativos" exibe o acréscimo de membros de cada app e os botões **Atribuir** e **Retirar**:

- **Atribuir** (`skeleton.system.applications.assign_app`): no modo `selected`, concede a app e as dependências ainda indisponíveis para a organização e as ativa, dependências primeiro, aplicando as mesmas validações da ativação pela organização. Apps pagas sem direito na cobrança são recusadas ("App paga: use a cobrança da organização") sem gravar nada. Apps já em uso permanecem inalteradas.
- **Retirar** (`unassign_app`): desativa a app mantendo configuração e dados preservados e, no modo `selected`, revoga a concessão. Se houver app dependente ainda ativa, a retirada é bloqueada e a mensagem informa qual é.

Um **pacote de apps** aplica a mesma lógica a um nível completo: atribuir o pacote ativa suas apps e desativa as do pacote anterior que não permanecem, exibindo a prévia do limite de membros ([instância de produto](product-instances.md#4-pacotes-de-apps-e-com-cobrança-preços-e-ofertas)). Ambas as operações abrem o contexto de sistema `platform:apps` restrito àquela organização, a partir da requisição do administrador da plataforma. Concluída a ação, a página informa a mudança no limite (por exemplo, `"Limite de membros: 3 → 10"`).

## Onde é conferido

O limite de `members` é verificado sob o `organization_lock` da organização antes de persistir o vínculo nos seguintes caminhos:

- `admit_member`: convite, pedido de ingresso aprovado, link de entrada, domínio verificado e provisionamento JIT via SSO;
- `create_membership`: criação de vínculo ativo e restauração de vínculo excluído como ativo;
- `update_membership`: reativação de vínculo.

Como todos esses fluxos adquirem o lock da organização, duas admissões concorrentes disputando a última vaga produzem um único vencedor; a outra recebe `QuotaLimitExceeded`, tratado pelas superfícies e pela API de serviços como conflito `quota_exceeded`. Já `external_people` é conferido ao aceitar um convite externo.

## O que a organização vê

Usuários com `members.view` ou `members.manage` acompanham o uso de cada métrica limitada:

- nas páginas de vínculos, convites e pedidos de ingresso, aparece uma faixa como `"2 de 3 membros"`, contabilizando convites pendentes que reservam vaga; sem vagas disponíveis, o botão de convidar fica desabilitado com explicação e, acima do limite, surge o aviso de bloqueio de novas entradas;
- na API de serviços, `OrganizationService().limits(actor=...)` retorna `LimitInfo` com `metric`, `used`, `limit`, `reserved` e `over_limit`;
- na API REST, `GET /api/v1/o/<org>/limits/` expõe os mesmos dados ([API](api.md)).

Métricas ilimitadas não são exibidas.

### Convites reservam vaga

Antes de emitir um convite, o servidor confere sob o lock da organização se a soma de pessoas ativas, convites pendentes de outros e-mails e o novo convite cabe no limite.

Considera-se pendente o convite não aceito, não revogado e não expirado. Cada e-mail conta uma única vez, de modo que reenviar um convite não reserva vaga extra. Tentar convidar no limite é recusado com `quota_exceeded` (`"A organização atingiu o limite de N membros."`). Ao ser aceito, o convite ocupa a vaga reservada sem contar em duplicidade; ao vencer, libera a vaga.

### Recusas nos caminhos de entrada

Quando uma entrada é recusada por falta de vaga, a mensagem orienta a pessoa: `"Esta organização não tem vagas no momento. Fale com o administrador."`

- A aprovação de um pedido de ingresso (pela interface web ou API) retorna 409 `quota_exceeded`, mantendo o pedido pendente.
- Links de entrada e aceites de convite exibem a mensagem com status 409.
- O provisionamento JIT de SSO e a entrada automática por domínio verificado falham de maneira neutra: a pessoa não entra, nenhum dado da organização é exposto e o log registra `membership.limit_reached` com `reason_code` (`sso`, `domain` ou `invitation`) e o `sysid` da organização, sem e-mail nem nome.

### Aviso ao chegar no limite

Quando uma admissão preenche a última vaga disponível, quem tem `members.manage` recebe a notificação `organization.member_limit_reached` ([notificações](notifications.md)), limitada a uma por dia para o mesmo valor de limite, levando apenas `limit` e `used` no conteúdo.

## Redução do limite

Reduzir um limite nunca remove membros existentes. Se o uso atual ultrapassar o novo teto — por exemplo, ao desativar uma app, remover um item da assinatura ou reduzir o padrão da instância —, a organização entra no estado **acima do limite** (`LimitStatus.over_limit`) e fica impedida de admitir novas pessoas até o uso voltar para dentro do limite.

## Leitura

`skeleton.system.limits.limit_status(métrica)` retorna, para a organização no contexto atual, `used`, `limit` (`None` quando ilimitado), `base`, `contributions` (fonte e valor de cada contribuição), `over_limit` e `remaining`.

Passar uma métrica desconhecida levanta `ValueError`; chamar fora do contexto de uma organização levanta `MissingTenantContext`. Chamar `check_limit` para uma organização diferente da que está no contexto atual não confere nada, pois suas linhas não são legíveis ali.
