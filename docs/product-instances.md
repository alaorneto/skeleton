# Montar uma instância de produto

Uma **instância de produto** é uma instalação do Skeleton operada para vender um serviço: cada cliente corresponde a uma organização, os **níveis** comerciais do produto (por exemplo, básico, intermediário e completo) são modelados como **pacotes de apps**, e uma **instância central** — do mesmo operador — acompanha a operação das demais.

Este guia apresenta o roteiro na ordem de configuração, com links para a referência de cada etapa. Corresponde ao [epic #693](https://github.com/alaorneto/skeleton/issues/693). Os exemplos usam níveis genéricos: um pacote "básico" com a app A e um "completo" com as apps A e B.

## 1. Identidade da instância

Em `/platform/instance/`, na seção "Identidade", defina o nome e a finalidade da instância. O identificador (`instance_id`) é gerado automaticamente no `migrate` e nunca muda — é por ele que a instância central reconhece esta instalação ([API de gestão](platform-api.md#identidade-da-instância)).

## 2. Limite padrão de membros

Configure `SKELETON_ORGANIZATION_MEMBER_LIMIT` no catálogo da instância (em `/platform/instance/` ou via `manage.py instance set organization_member_limit 1`). Esse valor define a cota base de toda organização antes dos acréscimos vindos de apps ou assinaturas. Deixá-lo vazio mantém as organizações sem limite ([limites da organização](organization-limits.md#configuração)).

## 3. Apps dos níveis: disponibilidade e acréscimos

Instale as apps que compõem os níveis ([extensão](extending.md)). Bibliotecas first-party, como a agenda e o livro contábil, acompanham automaticamente as apps que dependem delas e não possuem disponibilidade, acréscimos, presença em pacotes nem preço próprios.

Para cada app instalada, acesse `/platform/apps/<app>/availability/` e configure:

- a disponibilidade (`selected` permite que a plataforma decida quais organizações recebem a app);
- na seção "Acréscimos por organização", quanto a app acrescenta a cada limite (por exemplo, A: `+2` membros; B: `+7` membros).

Com base `1`, o nível básico (A) oferece 3 membros e o completo (A e B) oferece 10 ([acréscimo por app](organization-limits.md#acréscimo-por-app)).

## 4. Pacotes de apps e, com cobrança, preços e ofertas

Em `/platform/bundles/`, crie um pacote para cada nível definindo suas apps, a ordem de exibição, o pacote padrão e quais opções aparecem no cadastro de clientes. Na página da organização, o seletor "Pacote de apps" mostra a prévia da troca — apps que entram e saem e o limite de membros antes e depois — e aplica a mudança em uma única ação. Marcar "Travar as apps do pacote" impede que a organização as desative.

Com a [cobrança](billing.md) ativada, cada pacote pode receber uma oferta por moeda (`BundleOffer`, via `skeleton.billing.bundle_offers.set_bundle_offer`): um preço de plano base cujas `included_apps` cubram todas as apps do pacote, ou o preço de uma app dele. Tentar atribuir um pacote contendo uma app paga sem direito na cobrança é recusado (`app_not_entitled`), exigindo contratação pela cobrança da organização.

## 5. Política das organizações novas e travas

Em `/platform/organizations/policy/`, configure o que toda organização nova recebe ao nascer: valores iniciais (opcionais ou travados) ou campos obrigatórios preenchidos pela plataforma na criação.

Para organizações já existentes, trave configurações individualmente em `/platform/organizations/<id>/settings/`. Campos travados ficam desabilitados na interface e têm qualquer tentativa de alteração recusada na tela, na API REST e na API de serviços ([configurações geridas pela plataforma](managed-settings.md)). Campos de app exclusivos da plataforma declaram `"managed_by": "platform"` no manifesto e são definidos em `/platform/organizations/<id>/apps/<app>/configuration/`.

## 6. Armazenamento por cliente e GB adicional

Configure `FILES_STORAGE_ENCRYPTION_KEYS` e cadastre os buckets em `/platform/storage/`, marcando um deles como padrão. Toda organização nova nasce vinculada ao local padrão (ou ao escolhido no formulário de criação) sem visualizar qual local utiliza. A troca de local só é permitida enquanto a organização não tiver arquivos gravados ([locais de armazenamento](files.md#locais-de-armazenamento-da-plataforma)).

A franquia de armazenamento e o valor por GB adicional são definidos no plano: `included_storage_bytes`, `storage_overage="bill"`, `overage_unit_bytes=1073741824` e `overage_unit_amount`. A cota definida manualmente pela plataforma continua atuando como teto soberano.

## 7. Cadastro do cliente

Ative `SKELETON_ORGANIZATION_SIGNUP_ENABLED` e, se necessário, ajuste `SKELETON_ORGANIZATION_SIGNUP_MAX_PER_PERSON` (padrão `1`).

Com o cadastro ligado, qualquer pessoa autenticada com e-mail verificado pode criar sua organização em `/organizations/new/`:
- o campo do identificador verifica em tempo real se o valor já está em uso, e o servidor revalida na submissão;
- a pessoa escolhe um dos pacotes visíveis no cadastro e torna-se administradora da organização, já sujeita à política de organizações novas e ao local de armazenamento padrão;
- se o pacote escolhido for pago, o sistema contrata o preço da oferta e direciona ao pagamento da primeira fatura, liberando as apps após a quitação.

Se a política tiver alguma regra marcada como exigida na criação, o cadastro público fica indisponível (e a lista de organizações da plataforma exibe um aviso). O filtro "Criadas por cadastro" lista as organizações originadas por esse fluxo.

## 8. Cliente da API de plataforma

Ative `SKELETON_INSIGHTS_ENABLED` para que a instância colete o resumo por organização e as métricas diárias consultadas pela central ([resumo e métricas](platform-api.md#resumo-e-métricas-skeletoninsights)).

Em `/platform/api-clients/`, crie um cliente para a instância central e emita um token com os escopos necessários (`platform.instance:read`, `platform.organizations:read`, `platform.billing:read`, `platform.metrics:read` e, somente se precisar do contato de cobrança, `platform.billing_contacts:read`), definindo a validade e, de preferência, restringindo à rede de origem da central. O token é exibido uma única vez ([clientes e tokens de plataforma](platform-api.md#clientes-e-tokens-de-plataforma)).

## 9. Na instância central

Configure `REMOTE_INSTANCE_ENCRYPTION_KEYS` e, em `/platform/remote-instances/`, cadastre uma conexão para cada instância de produto informando o endereço base, o token gerado no passo 8 e a organização da central autorizada a lê-la. O botão "Testar" confirma a conexão e fixa a identidade da instância remota.

A app leitora, instalada na organização autorizada, coleta os dados das remotas em jobs via `RemoteInstanceService`: organizações (com cursor incremental, trazendo apenas o que mudou desde a última coleta), faturas, pagamentos e métricas ([instância central](platform-api.md#instância-central-conexões-remotas)). A própria instância também exibe seu resumo local em `/platform/insights/`.

## 10. Fora deste roteiro

Ficam previstos como evoluções futuras:
- troca de pacote pela própria organização sem uso do subsistema de cobrança;
- movimentação de arquivos entre locais de armazenamento;
- escrita nas instâncias remotas a partir da central;
- relatórios financeiros além das métricas diárias.
