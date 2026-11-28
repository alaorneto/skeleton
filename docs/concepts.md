# Guia dos conceitos

Este guia é a porta de entrada para a documentação técnica. Ele apresenta o vocabulário do `django-skeleton` e como as peças se relacionam; os guias ligados ao fim de cada bloco trazem os contratos e procedimentos completos.

Imagine este cenário: Ana trabalha na Clínica Norte e no Laboratório Sul. Ela tem **uma única conta**, mas participa de **duas organizações**. Uma anotação da Clínica Norte não pode aparecer no Laboratório Sul e, mesmo dentro da clínica, Helena só pode lê-la se as regras de acesso permitirem.

Esse exemplo separa os três problemas centrais que o núcleo resolve de forma independente: **identificar a pessoa**, **isolar cada organização** e **autorizar cada operação**.

## Identidade e organizações

### Conta, perfil e organização

**Identidade global** representa a pessoa no sistema inteiro. O modelo `User` guarda credenciais e controles de autenticação; `UserProfile` guarda preferências pessoais como nome de exibição, idioma e fuso horário. Participar de uma nova organização não cria uma segunda conta.

O **idioma e o fuso horário** de cada resposta e de cada email seguem esta precedência:

1. Preferência do perfil da pessoa.
2. Padrão da organização atual, configurado pelo administrador em `/o/<org>/settings/`.
3. Padrão da instância (`SKELETON_DEFAULT_LANGUAGE` e `SKELETON_DEFAULT_TIMEZONE`).

Se um nível estiver em branco, vale o seguinte. Antes do login, usa-se apenas o cabeçalho `Accept-Language` do navegador, limitado aos idiomas de `LANGUAGES`. A organização ativa vem sempre do contexto aberto pelo middleware de tenant, nunca de cabeçalho ou parâmetro livre. O núcleo entrega apenas `pt-BR`; outros idiomas entram pelo catálogo do projeto derivado.

**Organização** (`Organization`) é a unidade dona dos dados de negócio — uma clínica, empresa ou equipe. No contexto de isolamento de dados, chamamos essa organização de **tenant**. **Multi-tenancy** significa atender várias organizações na mesma instalação sem misturar os dados de uma com os de outra.

**Vínculo** (`Membership`) conecta uma conta a uma organização. Ter conta no sistema não significa pertencer a todas as organizações: Ana pode ser administradora na Clínica Norte e membro comum no Laboratório Sul. Um vínculo desativado encerra o acesso imediatamente, mas mantém seu histórico preservado.

### Endereço da organização

Quando o **domínio base** (`SKELETON_ORGANIZATION_BASE_DOMAIN`, como `skeleton.com`) está configurado, cada organização pode ativar um **host de organização** próprio — seja um subdomínio (`acme.skeleton.com`) ou um **domínio próprio** comprovado por DNS (`portal.cliente.com.br`). O **host da plataforma** continua sendo o domínio principal.

O **site** é a organização acessada pelo seu host, cujo índice global `OrganizationSite` guarda apenas os dados públicos de marca. Nesse host vale o **escopo de site**: as URLs ficam **limpas** (`/tables/x/`) em vez de **canônicas** (`/o/<uuid>/tables/x/`), e somente aquela organização fica visível. O host apenas restringe o que pode ser acessado — nunca concede permissão. Veja [endereços de organização](organization-sites.md).

### Plataforma e administração da organização

A **plataforma** é a instalação inteira. Seu administrador opera em `/platform/` — gerindo identidades, organizações, apps instaladas, catálogo de tabelas, auditoria global e atualizações — enquanto os dados de negócio ficam em `/o/<org>/`.

O status de administrador da plataforma é decidido por um único predicado, `is_platform_admin` (`skeleton/accounts/platform.py`): conta autenticada, ativa, não excluída e com `is_staff` **e** `is_superuser` (ter apenas uma dessas flags não concede nada), além de MFA quando exigido. A plataforma nunca fica sem administrador: rebaixar, desativar ou remover o último administrador ativo retorna `409`.

Mesmo o administrador da plataforma não acessa os dados internos de uma organização sem vínculo. Sobre cada organização, ele vê apenas a contagem de membros ativos e os emails dos administradores, podendo designar ou substituir um administrador por serviço auditado ([controle de acesso](access-control.md#designação-de-administrador-pela-plataforma)).

Existem também dois **papéis limitados de plataforma** (`PlatformRoleAssignment`), que não concedem administração geral:

- **Auditor de plataforma:** lê a auditoria global e o catálogo de tabelas, sem editar o catálogo nem ver identidades ou organizações.
- **Operador de releases:** opera apenas `/platform/updates/`.

Somente o administrador da plataforma concede ou revoga esses papéis, e a revogação passa a valer na requisição seguinte ([controle de acesso](access-control.md#papéis-limitados-de-plataforma)).

Dentro de uma organização, o **administrador de organização** tem o papel `organization_admin`: gerencia vínculos, permissões e parâmetros das apps daquela organização, mas não instala código nem recebe `is_staff`/`is_superuser`. Parte dessa rotina pode ser delegada por papéis internos com **capacidades** definidas em código — o **gestor de pessoas** (`people_manager`) administra membros e convites e o **gestor de apps** (`app_manager`) configura apps. Papéis criados pela organização nunca carregam capacidades internas ([capacidades e papéis internos](access-control.md#capacidades-e-papéis-internos)).

Essa separação evita escalonamento indevido: administrar uma clínica não dá acesso às demais nem ao servidor, e ser superusuário Django não dispensa vínculo nem autorização nas rotas de uma organização.

### Ingresso numa organização

Uma conta global só passa a participar de uma organização por um destes caminhos:

- **Convite por email:** uso único, enviado por quem gerencia pessoas.
- **Pedido com aprovação:** a organização habilita pedidos (e opcionalmente aparece no diretório `/join/`); um gestor aprova ou recusa.
- **Link de ingresso:** multiuso, com validade, limite de usos e entrada direta ou sob aprovação; pode liberar o cadastro de novas contas.
- **Domínio verificado:** quem confirma um email de um domínio comprovado por DNS pela organização entra direto ou abre um pedido.
- **SSO ou LDAP:** configurado pela plataforma para a organização, que decide provisionamento JIT, aprovação, papel padrão e domínios aceitos.

Admissões automáticas (link, domínio, SSO ou sincronização) nunca reativam um vínculo encerrado anteriormente: quem saiu ou foi removido sempre depende de aprovação humana para retornar. Sair de uma organização é livre, exceto para o último administrador ativo.

Veja [modelo de dados](data-model.md) e [controle de acesso](access-control.md).

## Registros e banco de dados

### Pessoa externa e portal

Uma **pessoa externa** (como um cliente ou paciente) acessa recursos específicos que uma app expõe para ela, sem se tornar membro da organização: não consome vaga no limite de membros, não aparece nas listas internas e não tem poderes de membro.

Seu vínculo é um **acesso externo** (`ExternalAccess`), criado exclusivamente por convite e aceito com email verificado. Ela navega pelo **portal** (`/portal/<org>/`), que abre um **contexto externo**: políticas de membro, capacidades e o gateway genérico de registros negam acesso por padrão, e somente métodos de serviço marcados para uso externo respondem, autorizando pela relação que a app mantém entre aquela pessoa e os registros dela ([portal](portal.md)).

### Entidade, modelo, tabela e registro

- **Entidade:** um conceito de negócio persistido, como uma anotação.
- **Modelo Django:** a classe Python que define campos e regras da entidade.
- **Tabela:** a estrutura física no PostgreSQL.
- **Registro (ou linha):** uma ocorrência concreta, como a anotação criada por Ana.
- **Coluna:** o campo físico na tabela (o nome em Python pode diferir do SQL, como o campo `organization` gravado na coluna `organization_id`).

O **ORM** traduz operações em modelos para SQL, mas não autoriza nada sozinho: toda consulta precisa respeitar organização, permissões e exclusão lógica.

### Identificadores: UUID, chave primária e código comercial

A **chave primária** (**PK**, de *primary key*) identifica cada linha de forma única. Nas tabelas próprias do skeleton, ela é sempre `sysid`, um **UUID** gerado no servidor que nunca muda — nem em edição, exclusão lógica ou restauração.

Um **código comercial** (como `AT-2026-0042`) segue regras de negócio, vive em um campo separado e não substitui `sysid`. O subsistema de [sequências](sequences.md) gera códigos numéricos sem lacunas por organização e período.

O UUID não é segredo nem prova de permissão: conhecer a URL de um registro não autoriza lê-lo ou alterá-lo.

Uma **chave estrangeira** (**FK**, de *foreign key*) liga dois registros. Relações entre entidades próprias usam UUID e `PROTECT` (em vez de exclusão em cascata), e em tabelas organizacionais ambas as pontas precisam pertencer à mesma organização.

### Bases abstratas

Uma **classe abstrata** fornece campos e comportamentos às subclasses sem criar tabela própria. O skeleton não possui uma supertabela universal:

- `SystemRecord`: metadados comuns, `sysid`, revisão, exclusão lógica e guards de persistência; base das entidades globais próprias.
- `OrganizationRecord`: acrescenta organização obrigatória e imutável; base para vínculos e dados organizacionais sem autorização individual direta.
- `SecuredRecord`: acrescenta proprietário (`owner`) e visibilidade; é a base padrão para dados de negócio com controle de acesso por registro.

Ao definir `Note(SecuredRecord)`, todos os campos existem diretamente na tabela concreta de `Note`. Herdar da base correta ainda exige registrar o `TableSpec`, instalar a infraestrutura na migration e passar pelos serviços de autorização.

### Nomes que não são intercambiáveis

| Nome | Exemplo | Significado |
|---|---|---|
| ID da app | `org.skeleton.example-notes` | Identidade estável do componente, usada em dependências e pacotes |
| Label Django da app | `example_notes` | Identidade da app no ORM e no histórico de migrations |
| Pacote Python | `applications.notes` | Caminho de importação do código |
| Label do modelo / `table_key` | `example_notes.note` | Chave registrada que a interface e a API aceitam |
| Tabela física | `example_notes_note` | Nome real no SQL, obtido do modelo e da introspecção |
| ID do registro | `sysid` | UUID de uma linha específica |

Alterar um label já usado em migrations não é um ajuste cosmético: quebra o histórico do banco. A interface nunca aceita nomes SQL livres enviados pelo cliente para escolher uma tabela.

### Estrutura, schema e migrations

A **estrutura do banco** reúne tabelas, colunas, índices e relações. No PostgreSQL, **schema** também designa o espaço de nomes que agrupa esses objetos — este projeto usa o schema `skeleton` (não confunda com o formato de um JSON ou a modelagem lógica).

Uma **migration** é uma mudança versionada aplicada pelo Django para evoluir o schema ou transformar dados. Seu número não tem relação com a versão da app, e migrations já aplicadas não podem ser renomeadas nem reescritas sem comprometer atualizações de instalações existentes.

**DDL** é o SQL que altera estrutura (`CREATE TABLE`, `ALTER TABLE`); **DML** é o SQL que lê ou grava dados (`INSERT`, `UPDATE`). O processo web não executa DDL: o administrador aprova uma operação pela interface e o agente de deployment aplica as migrations com outra credencial.

Já a **configuração da instância** (hosts, banco, email, cache, armazenamento e segredos) não é dado de aplicação: ela fica em um SQLite próprio da instância, com segredos cifrados por uma chave mestra separada, e variáveis de ambiente sempre têm precedência sobre ele. Essa configuração nasce no **setup** inicial (protegido por token de uso único) e depois é alterada em `/platform/instance/` pelo administrador da plataforma, com autenticação recente.

Veja [modelo de dados](data-model.md), [catálogo](table-catalog.md) e [extensão](extending.md).

## Autorização e contexto

### Autenticar não é autorizar

**Autenticação** responde *"quem está fazendo a requisição?"*. **Autorização** responde *"essa pessoa pode executar esta ação sobre estes dados?"*. Estar autenticado é apenas o primeiro passo.

Um **papel** (`Role`) agrupa permissões (como *membro* ou *administrador*); um **grupo** (`AccessGroup`) agrupa vínculos de pessoas. Papéis podem ser atribuídos diretamente ao vínculo ou via grupo, sem usar os grupos globais do Django como atalho.

O skeleton combina **RBAC** (controle de acesso baseado em papéis) com regras por registro:

- `TablePermission`: autoriza uma ação sobre um tipo de tabela (por exemplo, ler anotações).
- `RecordGrant`: concede uma ação sobre um registro específico a um vínculo, grupo ou papel.
- **Proprietário** (`owner`): membro ativo da mesma organização responsável pelo registro.
- **Visibilidade**: define quem pode ler o registro (privado, grupo ou organização), sem conceder escrita automaticamente.

Um `RecordGrant` não dispensa `TablePermission`, e visibilidade de organização nunca expõe dados fora do tenant. Vale sempre a regra de **negação por padrão** (*default deny*): o que não foi explicitamente permitido é negado.

### Contexto da operação

O **ator** (*actor*) é a identidade efetiva que executa a operação. O **contexto** carrega ator, organização, origem e identificador da requisição para as camadas de persistência e autorização. Em requisições HTTP, o servidor extrai o ator da sessão e valida a organização pela URL — nunca aceita esses dados por cabeçalho ou corpo da requisição.

- `request_context`: delimita a transação de uma requisição global.
- `tenant_context`: entra em uma organização após validar o vínculo ativo.
- `system_context`: contexto explícito de infraestrutura ou CLI, com motivo obrigatório; nunca é exposto via HTTP.

No PostgreSQL, o contexto é gravado em **GUCs** (variáveis locais da sessão) via `SET LOCAL`, valendo apenas até o fim da transação. Por isso, jobs que processam várias organizações abrem uma transação independente para cada organização.

### RLS e as credenciais do banco

**RLS** (*row-level security*) é o recurso do PostgreSQL que filtra linhas automaticamente com base na política da tabela e no contexto da conexão. No skeleton, ela funciona como rede de segurança para evitar que uma consulta mal filtrada acesse outra organização.

O banco opera com duas credenciais principais:

- **Runtime:** usada pela aplicação que atende usuários; não possui `BYPASSRLS` nem permissão de DDL.
- **Owner (ou migrator):** proprietária dos objetos SQL, usada apenas pelo operador ou agente para rodar migrations (não confunda com o campo `owner` de um registro).

A RLS não substitui papéis, visibilidade nem exclusão lógica — as policies e serviços Django continuam obrigatórios. Ela também não protege contra quem controla o banco ou executa SQL arbitrário com privilégios para alterar o contexto.

Veja [controle de acesso](access-control.md) para o algoritmo completo e [deployment](deployment.md) para o provisionamento das credenciais.

## Auditoria e concorrência

### Transação, commit e rollback

Uma **transação** agrupa operações que devem ser confirmadas juntas (**commit**) ou totalmente desfeitas em caso de erro (**rollback**). Um **savepoint** permite desfazer apenas uma etapa interna sem confirmar o restante antes do commit final.

No skeleton, a gravação de um registro e o registro de auditoria ocorrem na mesma transação: se a auditoria falhar, a alteração também é desfeita. Logs de segurança são um canal separado — como são gravados fora do banco, podem permanecer após um rollback e não devem ser tratados como prova de alteração confirmada.

### Revisão, CAS e ETag

Todo registro próprio mantém um contador `revision`. **CAS** (*compare-and-swap*) garante que uma edição só seja gravada se a revisão lida pela pessoa ainda for a atual no banco. Se Ana e Helena abrem a revisão 7, quem salvar primeiro gera a revisão 8; a segunda tentativa recebe conflito em vez de sobrescrever a edição da outra.

Na API REST, a revisão aparece no cabeçalho **ETag** e é reenviada pelo cliente em `If-Match`. Diante de um conflito, o cliente deve reler o registro e decidir como mesclar — reenviar silenciosamente com a revisão nova anularia a proteção.

Não confunda `revision` de registro com a **geração de deployment** (que identifica o conjunto de software em execução) nem com a versão semântica de um pacote (`0.2.0`).

### Exclusão lógica, tombstone e restauração

**Exclusão lógica** (*soft delete*) preenche `deleted_at` sem apagar a linha fisicamente. Essa marca é chamada de **tombstone**: o registro some das consultas comuns, mas preserva UUID, dados e histórico.

**Restaurar** significa remover essa marca após validar unicidade e referências ativas — o registro volta com o mesmo UUID, sem ser duplicado. Se outro registro ativo já ocupa o mesmo código comercial, a restauração falha em vez de sobrescrevê-lo.

`with_deleted()` inclui registros excluídos na consulta, mas nunca desativa isolamento de organização nem autorização. Da mesma forma, desativar uma conta ou vínculo é uma transição de acesso distinta: restaurar uma identidade não reativa automaticamente seus privilégios antigos.

### Triggers, redação e append-only

Um **trigger** é uma rotina executada pelo próprio PostgreSQL sempre que uma linha muda. No skeleton, triggers geram os eventos de auditoria diretamente no banco, cobrindo tanto o ORM quanto SQL direto.

**Redação** (*redaction*) substitui valores sensíveis por `[REDACTED]` **antes** de gravar o evento de auditoria — não se limita a esconder o campo na tela. Campos novos precisam ser declarados corretamente; o sistema não tenta adivinhar segredos não declarados.

**Append-only** significa que a role runtime só pode inserir eventos de auditoria, nunca alterá-los ou apagá-los. Isso não equivale a armazenamento **WORM** (*write once, read many*): DBA e owner de migrations continuam sendo fronteiras privilegiadas.

Veja [auditoria, concorrência e exclusão](audit-soft-delete.md) e [API](api.md).

## Apps, pacotes e deployment

### Especificação, manifesto e catálogo

- `TableSpec` é a **especificação no código** de cada tabela: lista explicitamente (*allowlist*) campos de leitura, escrita, sensibilidade e exposição na API. O **registry** reúne essas especificações na inicialização, sem executar DDL.
- O **catálogo físico** no banco (`TableDefinition` e `FieldDefinition`) inventaria o que realmente existe no PostgreSQL. Alterar rótulos ou descrições no catálogo não modifica schema nem permissões.
- `manifest.json` é o **contrato canônico da app**: declara identidade, versão, dependências, entradas de código, parâmetros, arquivos e migrations, carregado em runtime como `AppManifest`. "Canônico" significa existir uma única fonte de verdade, sem manifestos paralelos.

### API de serviços e API REST

A **API de serviços** reúne as classes que uma app publica em `service_api.py` para outras apps chamarem dentro do mesmo processo — por exemplo, a agenda chama `NotificationService().notify(...)` do núcleo sem importar os modelos de notificação. A chamada roda na transação e no contexto do chamador, revalida autorização internamente e retorna **DTOs** imutáveis (*data transfer objects*), nunca modelos do banco.

A **API REST** (`/api/v1/`) atende navegadores e clientes HTTP. As duas trabalham em camadas: a REST de uma app deve ser uma camada fina sobre sua API de serviços. Toda app é obrigada a publicar sua API de serviços ([API de serviços entre apps](service-api.md)).

### Pacote, wheel, assinatura e publicador

Um **pacote `.skapp`** é um arquivo fechado e assinado entregue ao instalador. Um **wheel** é uma distribuição Python pronta para instalação; o wheel do núcleo contém apenas o pacote `skeleton`, deixando `config` e `applications` no projeto derivado.

- **Hash/digest SHA-256:** identifica o conteúdo exato de um arquivo (qualquer byte alterado muda o hash).
- **Assinatura:** vincula esse conteúdo à chave privada de um **publicador** (*publisher*).
- **Trust store:** conjunto de chaves públicas confiáveis, mantido exclusivamente pelo operador (nunca vindo de dentro do pacote).

Uma assinatura válida comprova procedência e integridade segundo a política do operador, mas **não** garante que o código Python seja inofensivo: apps e migrations aprovadas continuam sendo código privilegiado, não executam em sandbox.

Um **lock de dependências** trava versões e hashes exatos; um **wheelhouse** guarda os wheels locais para montar o ambiente sem depender da rede. Cada instalação prepara um ambiente virtual novo, sem alterar as bibliotecas dos workers em execução.

### Versão, instalação e ativação

A **versão do componente** (`0.2.0`) segue compatibilidade semântica (major/minor/patch) e é comparada por biblioteca, nunca como texto puro. Ela é independente de `manifest_version` (formato do manifesto) e da numeração das migrations.

O ciclo de vida de uma app passa por três camadas distintas — **instalada ≠ disponível ≠ ativa**:

1. **Instalação global** (`ComponentInstallation`): indica qual código está presente no deployment (todas as organizações compartilham o mesmo código).
2. **Disponibilidade** (`AppAvailability`): decidida pela plataforma, que libera a app para todas as organizações (`all`) ou apenas para selecionadas (`selected`, via `AppOrganizationGrant`).
3. **Ativação por organização** (`AppInstallation`): define se a organização usa a app e com quais parâmetros.

Os dois portões começam fechados por padrão: sem disponibilidade a app retorna `404` mesmo que tenha sido ativada antes, e revogar a disponibilidade preserva a ativação e os parâmetros da organização caso ela volte a ser liberada. A plataforma também pode **atribuir** uma app diretamente a uma organização, liberando e ativando-a junto com suas dependências ([limites da organização](organization-limits.md#atribuir-apps-pela-plataforma)). O antigo campo `default_enabled` do manifesto é obsoleto; instalações anteriores migram via `upgrade_app_activation` ([extensões](extending.md#default-fechado-e-upgrade-da-ativação-22)). Desativar uma app numa organização não remove código nem roda migrations.

Estados de um componente no inventário:

- `installed`: componente gerenciado presente no release atual.
- `project-local`: código pertencente ao projeto derivado, protegido contra sobrescrita por pacotes.
- `schema_retained`: código retirado do release, mantendo tabelas, dados e histórico preservados; reinstalar exige reativação explícita em cada organização.

Algumas apps genéricas são distribuídas **dentro do wheel do núcleo** (apps first-party em `skeleton/contrib`), sem pacote `.skapp`. Entre elas, a agenda e o livro contábil são **bibliotecas**: recursos consumidos por outras apps via API de serviços. Uma biblioteca entra automaticamente quando alguma app instalada depende dela, não possui disponibilidade nem ativação próprias e permanece efetiva na organização enquanto houver uma app efetiva que a utilize; retirá-la preserva os dados ([extensão](extending.md#apps-first-party-do-núcleo-skeletoncontrib)).

### Livro, lançamento e título

O **livro contábil** (`skeleton_ledger`) cuida da contabilidade de cada organização, independente da cobrança da plataforma:

- **Livro:** define moeda, regime (caixa ou competência) e **plano de contas**.
- **Chave de conta:** nome estável usado pelas apps (como `ledger.cash` ou `acme_services.revenue`) e associado pela organização a uma conta real do plano.
- **Lançamento:** conjunto balanceado de partidas (débitos = créditos), numerado e **selado** na criação; correções só ocorrem por **estorno**.
- **Título:** valor a receber ou a pagar com vencimento; a **baixa** registra a quitação e gera o lançamento correspondente.

Apps gravam lançamentos por meio de **regras** registradas, com autorização derivada do próprio registro de origem (por exemplo, um atendimento), e não de permissões diretas no livro.

### Produto, preço, assinatura e fatura

Quando a plataforma é operada como SaaS ([cobrança](billing.md)), cada organização possui uma **conta de cobrança**:

- **Produto:** o que é comercializado (plano base, app ou pacote de armazenamento).
- **Preço:** oferta do produto em uma moeda e ciclo; seus termos são imutáveis para preservar contratos vigentes.
- **Assinatura e itens:** o contrato recorrente da organização e cada preço contratado nele.
- **Fatura e pagamento:** o valor devido no período (com número e linhas imutáveis após emissão) e cada tentativa de quitação.

Com a cobrança ativa, **disponível ≠ contratado**: uma app paga só fica acessível à organização enquanto houver direito contratual válido.

### Pacote de release, fonte e caminho de atualização

Um **pacote de release** é um `.skapp` do tipo `kind: "release"` que empacota o wheel do núcleo, o lock e os wheels das dependências para cada **alvo** (Python e plataforma), além do wheel do agente. Esse conjunto de lock e wheels validado pelo agente forma o **ambiente do publicador**.

Uma **fonte** é onde o agente busca releases (`path`, `url` ou repositório GitHub), lendo o **índice de releases** assinado publicado nela. A **linha** de versão corresponde ao par `major.minor`; o **caminho de atualização** calcula a sequência de versões um minor por vez, executando cada **passo** como uma operação completa do agente ([guia de atualização](updates.md)).

### Release, preparação e troca

- **Deployment:** a instalação operacional em produção.
- **Release:** o conjunto imutável de núcleo, apps, ambiente virtual e recursos.
- **Staging:** a montagem desse release em diretório ou imagem separado antes da troca.
- **Geração:** contador que avança a cada troca bem-sucedida do release ativo.
- **Inventário:** registro dos componentes e arquivos daquela geração.
- **Journal:** log durável das etapas da operação, mantido fora do banco da aplicação para resistir a uma restauração do banco.
- `DeploymentOperation`: solicitação e acompanhamento auditado da operação na interface da plataforma.
- **Cutover:** a virada em que novos workers passam a servir o release novo. O código nunca é sobrescrito sobre processos em execução, e o agente só chama comandos provisionados pelo operador (nunca shell vindo do manifesto).

**Idempotência** significa reconhecer uma solicitação repetida sem reaplicar seus efeitos, mas não elimina conflitos de geração, CAS ou planos divergentes. Uma operação com status "aprovado" indica apenas autorização registrada — não que migrations, checks e reinício já concluíram.

### Backup, rollback e recuperação

**Backup** é uma cópia consistente usada para recuperação de desastre. **Restaurar um backup** devolve o banco e os arquivos àquele ponto no tempo — operação completamente distinta de restaurar um registro excluído logicamente.

**Rollback do release** volta o software para a geração anterior, mas **não** desfaz alterações já aplicadas ao banco: se houve migration, trocar apenas o código pode deixar aplicação e schema incompatíveis.

A **manutenção do deploy** bloqueia o tráfego da aplicação e drena tarefas em andamento para executar backup, migrations, checks e troca com segurança; se algo falhar, o bloqueio permanece até que o estado seja reconciliado. Ela é controlada exclusivamente pelo agente. Já a manutenção planejada fora de um deploy é feita na borda da infraestrutura (proxy ou Coolify), que continua operando mesmo se a aplicação estiver instável ([manutenção](maintenance.md)). Após reabrir o tráfego, restaurar um backup antigo pode descartar escritas novas e exige decisão operacional explícita.

Veja [extensão e publicação](extending.md) e [deployment](deployment.md).

## Login e protocolos

### Sessão, CSRF e MFA

Após o login, a **sessão Django** identifica o usuário nas requisições seguintes por cookie. A API do projeto aceita essa sessão (ou tokens próprios); um token externo do gov.br nunca se transforma em token da API.

**CSRF** protege contra sites externos que tentem induzir o navegador autenticado a disparar alterações sem consentimento. Toda mutação exige proteção CSRF, e requisições `GET` nunca alteram estado (como instalar software ou encerrar sessão).

**MFA** exige um segundo fator de autenticação. Ser administrador ou chegar de um provedor externo confiável não dispensa a política local de MFA. O campo `auth_version` invalida sessões anteriores sempre que a política ou as credenciais da conta mudam.

### SSO, IdP, OIDC e SAML

**SSO** (*single sign-on*) permite entrar com a identidade mantida por um **IdP** (*identity provider*), que autentica a pessoa e emite uma prova — mas não decide suas permissões dentro das organizações do skeleton.

- **OAuth 2.0 / OIDC:** OAuth trata de autorização delegada; o *OpenID Connect* adiciona a camada de identidade usada no login.
- **SAML:** padrão corporativo de troca de provas de identidade baseado em XML.

O skeleton reutiliza `django-allauth` e bibliotecas validadas, sem escrever parsers próprios desses protocolos.

Na prova de identidade, **issuer** identifica o emissor, **subject** identifica a pessoa dentro daquele provedor e **audience** identifica o destinatário esperado. O email não substitui essa chave estável, e coincidência de email ou CPF nunca vincula contas automaticamente.

**Claims** são atributos incluídos na prova (como email). Eles são validados e minimizados antes de qualquer gravação e nunca concedem papéis ou permissões de tabela. No perfil gov.br, o `sub` técnico é tratado como dado pessoal e não é replicado em perfil, log ou payload de auditoria.

### State, nonce e PKCE

- **State:** vincula o callback ao fluxo iniciado pelo servidor e é consumido uma única vez.
- **Nonce:** amarra o ID token àquela tentativa específica de login.
- **PKCE S256:** protege a troca do código de autorização com um segredo efêmero gerado no início do fluxo.

Essas proteções se complementam: verificar apenas a assinatura do token não substitui nenhuma delas.

**Email verificado** exige comprovação aceita pelo perfil, não a simples presença do campo no JSON. Quando a pessoa tem admissão autorizada mas o provedor gov.br não fornece email, o allauth pode coletar e confirmar um endereço válido — ainda sem criar organização nem vínculo automaticamente.

Testes locais com JWT assinado ou Keycloak real validam o fluxo técnico, mas não equivalem à **homologação gov.br**, que exige credenciais oficiais, URLs registradas e aprovação no ambiente governamental. HTTPS, SMTP e infraestrutura de produção também exigem validação própria.

Veja [autenticação e SSO](authentication-sso.md) e [testes e evidências](testing.md).

## Instância de produto

Uma **instância de produto** entrega um software comercializado como serviço: cada cliente é uma organização e os **níveis** do plano correspondem a **pacotes de apps** (`AppBundle`), onde o limite de membros acompanha o que cada app adiciona.

A plataforma define a **política padrão das organizações novas**, **trava** configurações que não devem ser alteradas pelo cliente e escolhe o **local de armazenamento** de cada organização, enquanto o cliente pode se cadastrar e escolher seu nível. Uma **instância central** do mesmo operador pode monitorar várias instâncias remotas pela **API de gestão**, usando um token de plataforma por instância ([montar uma instância de produto](product-instances.md)).
