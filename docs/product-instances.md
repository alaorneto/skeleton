# Montar uma instância de produto

Uma **instância de produto** é uma instalação do Skeleton que vende um produto: cada cliente é uma organização, os **níveis** do produto (por exemplo básico, intermediário e completo) são **pacotes de apps**, e uma **instância central**, do mesmo dono, acompanha as demais. Este guia é o roteiro, na ordem em que se monta; cada passo aponta a referência. É o [epic #693](https://github.com/alaorneto/skeleton/issues/693).

Os exemplos usam níveis genéricos: um pacote "básico" com a app A e um "completo" com as apps A e B.

## 1. Identidade da instância

Em `/platform/instance/`, seção "Identidade", dê um nome e uma finalidade à instância. O identificador (`instance_id`) foi gerado no `migrate` e nunca muda: é por ele que a central reconhece esta instância ([API de gestão](platform-api.md#identidade-da-instância)).

## 2. Limite padrão de membros

Defina `SKELETON_ORGANIZATION_MEMBER_LIMIT` no catálogo da instância (`/platform/instance/` ou `manage.py instance set organization_member_limit 1`): é a base de toda organização, antes do que as apps acrescentam. Vazio mantém as organizações sem limite ([limites da organização](organization-limits.md#configuração)).

## 3. Apps dos níveis: disponibilidade e acréscimos

Instale as apps dos níveis ([extensão](extending.md)) e, em `/platform/apps/<app>/availability/`:

- defina a disponibilidade (`selected` deixa a plataforma decidir quem recebe a app);
- em "Acréscimos por organização", quanto cada app soma a cada limite (por exemplo A: +2 membros, B: +7).

Com base 1, o básico (A) dá 3 membros e o completo (A e B) dá 10 ([acréscimo por app](organization-limits.md#acréscimo-por-app)).

## 4. Pacotes de apps e, com cobrança, preços e ofertas

Em `/platform/bundles/`, crie um pacote por nível, com as apps, a ordem, um pacote padrão e quais aparecem no cadastro. Na página da organização, o seletor "Pacote de apps" pré-visualiza a troca (apps que entram e saem, limite de membros antes e depois) e a aplica numa ação; com "Travar as apps do pacote", a organização não consegue desativá-las.

Com a [cobrança](billing.md) ligada, cada pacote pode ter uma oferta por moeda (`BundleOffer`, `skeleton.billing.bundle_offers.set_bundle_offer`): um preço de plano base cujas `included_apps` cubram as apps do pacote, ou o preço de uma app dele. Um pacote com app paga sem direito é recusado (`app_not_entitled`): contrate-o pela cobrança da organização.

## 5. Política das organizações novas e travas

Em `/platform/organizations/policy/`, defina o que toda organização nova recebe: um valor inicial (que pode ser travado) ou um valor que a plataforma informa ao criar. Em `/platform/organizations/<id>/settings/`, trave uma configuração de uma organização existente; a organização vê o campo desabilitado e não o muda por tela, API nem serviço ([configurações geridas pela plataforma](managed-settings.md)). Campos de app que só a plataforma define usam `"managed_by": "platform"` no manifesto e são gravados em `/platform/organizations/<id>/apps/<app>/configuration/`.

## 6. Armazenamento por cliente e GB adicional

Configure `FILES_STORAGE_ENCRYPTION_KEYS` e cadastre os buckets em `/platform/storage/`, marcando um como padrão. Cada organização nova nasce no local padrão (ou no escolhido no formulário de criação) e nunca vê qual é; a troca só vale enquanto a organização não tem arquivos ([locais de armazenamento](files.md#locais-de-armazenamento-da-plataforma)). O espaço incluído e o GB adicional vêm do plano: `included_storage_bytes`, `storage_overage="bill"`, `overage_unit_bytes=1073741824` e `overage_unit_amount`; a cota da plataforma continua como teto manual.

## 7. Cadastro do cliente

Ligue `SKELETON_ORGANIZATION_SIGNUP_ENABLED` (e ajuste `SKELETON_ORGANIZATION_SIGNUP_MAX_PER_PERSON`, padrão 1). Uma pessoa autenticada com email verificado cria a própria organização em `/organizations/new/`, escolhe um dos pacotes visíveis no cadastro e vira a administradora; a política das organizações novas e o local padrão se aplicam. Um pacote pago contrata o preço da oferta e leva a pessoa ao pagamento da primeira fatura; as apps chegam com o pagamento. Uma regra da política exigida na criação deixa o cadastro indisponível, e a lista de organizações da plataforma avisa disso. O filtro "Criadas por cadastro" lista as organizações que nasceram assim.

## 8. Cliente da API de plataforma

Ligue `SKELETON_INSIGHTS_ENABLED` para a instância coletar o resumo por organização e as métricas diárias que a central lê ([resumo e métricas](platform-api.md#resumo-e-métricas-skeletoninsights)). Em `/platform/api-clients/`, crie um cliente para a central e emita um token com os escopos que ela precisa (`platform.instance:read`, `platform.organizations:read`, `platform.billing:read`, `platform.metrics:read` e, só se for preciso o contato de cobrança, `platform.billing_contacts:read`), a validade e, de preferência, a rede de origem da central. O token aparece uma vez ([clientes e tokens de plataforma](platform-api.md#clientes-e-tokens-de-plataforma)).

## 9. Na instância central

Configure `REMOTE_INSTANCE_ENCRYPTION_KEYS` e, em `/platform/remote-instances/`, cadastre uma conexão por instância de produto: o endereço base, o token emitido no passo 8 e a organização da central liberada para lê-la. "Testar" fixa a identidade da remota. A app leitora, instalada na organização liberada, lê as remotas em jobs por `RemoteInstanceService`: as organizações (por cursor, só as que mudaram desde a última leitura), as faturas, os pagamentos e as métricas ([instância central](platform-api.md#instância-central-conexões-remotas)). A própria instância mostra o resumo em `/platform/insights/`.

## 10. Fora deste roteiro

Ficam como evolução: a troca de pacote pela própria organização sem a cobrança, mover arquivos entre locais de armazenamento, escrita nas instâncias remotas pela central e relatórios financeiros além das métricas diárias.
