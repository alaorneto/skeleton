# Guia dos conceitos

Este guia é a porta de entrada para a documentação técnica. Ele explica o vocabulário usado pelo skeleton; os documentos ligados em cada seção descrevem os contratos e os procedimentos completos.

Pense em um exemplo: Ana trabalha na Clínica Norte e no Laboratório Sul. Ela tem **uma conta**, mas participa de **duas organizações**. Uma anotação da Clínica Norte não pode aparecer no Laboratório Sul. Mesmo dentro da clínica, Helena só pode ler essa anotação se as regras de acesso permitirem. Esses são problemas diferentes: identificar a pessoa, separar organizações e autorizar uma operação.

## Identidade e organizações

### Conta, perfil e organização

**Identidade global** é a representação de uma pessoa no sistema inteiro. `User` guarda sua conta e os controles de autenticação; `UserProfile` guarda preferências como nome de exibição, idioma e fuso horário. Entrar em outra organização não cria uma segunda conta.

**Organização** (`Organization`) é a unidade à qual os dados de negócio pertencem: por exemplo, uma clínica, empresa ou equipe. A palavra inglesa **tenant** significa essa organização no contexto de isolamento de dados. **Multi-tenancy** significa que uma instalação atende várias organizações sem misturar seus dados.

**Vínculo** (`Membership`) liga uma conta a uma organização. Ter uma conta não significa pertencer a todas as organizações. Ana pode ter um papel de administradora na clínica e um papel de membro no laboratório. Um vínculo desativado deixa de permitir acesso, mas sua história continua armazenada.

### Plataforma e administração da organização

A **plataforma** é a instalação inteira do sistema. Seu administrador usa `/admin/` somente para configurações, parametrizações e operações de software. Identidades e outros registros globais ficam em `/platform/records/`, fora desse painel; o viewer de negócio fica no catálogo/tabelas de `/o/<org>/`. Acesso de plataforma exige flags Django, conta ativa e MFA quando configurado, sem conceder automaticamente vínculo/autorização tenant.

O **administrador de organização** tem o papel empresarial `organization_admin` dentro de uma organização. Ele gerencia seus vínculos, permissões e configuração das apps, mas não instala código nem recebe automaticamente `is_staff` ou `is_superuser`.

A distinção importa: conceder administração de uma clínica não pode conceder administração das outras clínicas ou permissão para atualizar o servidor. E ser superusuário Django não elimina os requisitos de vínculo e autorização nas rotas organizacionais.

Veja [modelo de dados](data-model.md) e [controle de acesso](access-control.md).

## Registros e banco de dados

### Entidade, modelo, tabela e registro

Uma **entidade** é um conceito que precisa ser persistido, como uma anotação. Um **modelo Django** é a classe Python que descreve seus campos e comportamento. Uma **tabela** é a estrutura física no banco. Um **registro**, ou linha, é uma ocorrência: a anotação escrita por Ana.

Uma **coluna** é um campo armazenado na tabela. O nome Python e o nome SQL podem diferir: o campo de relação `organization` normalmente usa a coluna `organization_id`.

O **ORM** é a camada Django que traduz consultas e operações sobre modelos em SQL. Usar o ORM não autoriza uma operação por si só: a consulta também precisa respeitar organização, permissões e exclusão lógica.

### Identificadores: UUID, chave primária e código comercial

A **chave primária** (**PK**, de *primary key*) identifica uma linha de forma única na tabela. Nas entidades próprias do skeleton, ela é `sysid`, um **UUID** gerado no servidor. O UUID permanece o mesmo ao editar, excluir logicamente ou restaurar um registro.

Um código comercial, como o número de atendimento `AT-2026-0042`, é outra coisa. Pode obedecer a regras do negócio e fica em um campo separado. Não deve substituir `sysid`.

O UUID não é senha nem prova de autorização. Conhecer o endereço de uma anotação não concede acesso a ela.

Uma **chave estrangeira** (**FK**, de *foreign key*) representa uma relação com outro registro. Relações próprias usam UUID e `PROTECT`, em vez de apagamento automático em cascata. Para relações organizacionais, as duas pontas também precisam pertencer à mesma organização.

### Bases abstratas

Uma **classe abstrata** fornece campos e regras às classes filhas, mas não cria uma tabela própria. Portanto, o skeleton não possui uma tabela universal que armazene todos os objetos:

- `SystemRecord`: metadados comuns, UUID, revisão, exclusão lógica e controles de persistência; usada por entidades globais próprias.
- `OrganizationRecord`: acrescenta uma organização obrigatória e imutável; usada por dados e vínculos organizacionais.
- `SecuredRecord`: acrescenta proprietário e visibilidade; é a base usual de negócio com autorização por registro.

Ao criar `Note(SecuredRecord)`, esses campos ficam na tabela concreta de `Note`. A escolha da base não dispensa o cadastro da especificação, a migration de infraestrutura ou os serviços de autorização.

### Nomes que não são intercambiáveis

| Nome | Exemplo | Significado |
|---|---|---|
| ID da app | `org.skeleton.example-notes` | Identidade estável do componente, usada em dependências e pacotes |
| Label Django da app | `example_notes` | Identidade da app no ORM e no histórico de migrations |
| Pacote Python | `applications.notes` | Caminho de importação do código gerenciado |
| Label do modelo / `table_key` | `example_notes.note` | Chave registrada que a API aceita para esse modelo |
| Tabela física | `example_notes_note` | Nome SQL efetivo, obtido do modelo e da introspecção |
| ID do registro | `sysid` | UUID de uma linha, não o nome de um tipo ou tabela |

Alterar um label já usado em migrations não é a mesma coisa que mudar o título de uma página. A interface nunca usa um nome SQL arbitrário recebido do cliente para escolher uma tabela.

### Estrutura, schema e migrations

A **estrutura do banco** descreve tabelas, colunas, índices e relações. No PostgreSQL, **schema** também significa um espaço de nomes que agrupa objetos: este projeto usa `skeleton`. No SQLite, a introspecção usa `main`. Não confunda esse espaço de nomes com a estrutura lógica de um modelo ou o formato de um JSON.

Uma **migration** é uma mudança versionada que o executor Django aplica ao banco: criar uma tabela, acrescentar uma coluna ou transformar dados. Seu número não é a versão da app. Migrations já aplicadas precisam conservar seus nomes e conteúdo para que instalações antigas continuem atualizáveis.

**DDL** é SQL que muda estrutura, como `CREATE TABLE`. **DML** é SQL que opera dados, como `INSERT` e `UPDATE`. O usuário do processo web não deve poder executar DDL. O administrador visual solicita uma operação; o agente separado executa migrations com outra credencial.

Veja [modelo de dados](data-model.md), [catálogo](table-catalog.md) e [extensão](extending.md).

## Autorização e contexto

### Autenticar não é autorizar

**Autenticação** responde “quem está fazendo a requisição?”. **Autorização** responde “essa pessoa pode realizar esta operação sobre estes dados?”. Um login válido é apenas o começo da decisão.

Um **papel** (`Role`) reúne permissões, como “membro” ou “administrador da organização”. Um **grupo** (`AccessGroup`) reúne vínculos de pessoas. Um papel pode ser atribuído diretamente ao vínculo ou por um grupo, sem usar os grupos globais de permissões do Django como atalho.

**RBAC** significa controle de acesso baseado em papéis. O skeleton combina RBAC com regras por registro:

- `TablePermission`: permite uma ação sobre um tipo de registro, como ler anotações.
- `RecordGrant`: concede uma ação sobre um registro específico a um vínculo, grupo ou papel.
- **Proprietário** (`owner`): pessoa responsável pelo registro, ativa na mesma organização.
- **Visibilidade**: determina quais leitores podem ver o registro; não concede escrita automaticamente.

Um grant não substitui a permissão de tabela. Visibilidade de organização não publica a anotação na internet ou em outro tenant. A regra **negação por padrão** (*default deny*) significa que ausência de permissão não é tratada como permissão implícita.

### Contexto da operação

O **ator** (*actor*) é a identidade efetiva que realiza a operação. O **contexto** informa ator, organização, origem e ID da requisição às camadas de persistência e autorização. Em HTTP, o servidor obtém o ator da sessão e valida a organização da URL; não aceita esses valores como flags escolhidas pelo cliente.

- `request_context`: delimita a transação de uma requisição global.
- `tenant_context`: entra em uma organização após validar o vínculo.
- `system_context`: contexto explícito de infraestrutura ou CLI, com motivo obrigatório; não é uma promoção de privilégio oferecida por HTTP.

No PostgreSQL, o contexto também é refletido em **GUCs**, variáveis de configuração da conexão, usando `SET LOCAL`. Elas valem apenas dentro da transação. Em jobs que percorrem organizações, é preciso abrir uma transação independente por organização; não basta trocar um atributo enquanto uma consulta anterior permanece pendente.

### RLS e as credenciais do banco

**RLS** (*row-level security*) é a proteção PostgreSQL que filtra linhas conforme a política da tabela e o contexto da conexão. Aqui, ela ajuda a impedir que uma consulta de negócio esquecida leia ou altere outra organização.

A credencial **runtime** é a usada pelo processo que atende usuários. A credencial **migrator/owner** é a usada pelo operador para estrutura e migrations. “Owner” neste contexto significa proprietário de objetos SQL, não o campo `owner` de uma anotação. **BYPASSRLS** é um privilégio PostgreSQL que ignora essas políticas; a credencial runtime não o possui.

RLS não implementa papéis, visibilidade ou exclusão lógica: as policies e os serviços Django continuam necessários. Também não é uma defesa contra quem controla o banco ou uma credencial runtime maliciosa capaz de alterar o contexto por SQL. SQLite não tem RLS.

Veja [controle de acesso](access-control.md) para o algoritmo completo e [deployment](deployment.md) para provisionar as credenciais.

## Auditoria e concorrência

### Transação, commit e rollback

Uma **transação** agrupa mudanças que devem ser confirmadas juntas. **Commit** confirma essas mudanças. **Rollback** desfaz as mudanças ainda não confirmadas. Um **savepoint** permite desfazer uma parte de uma transação, sem equivaler a um novo commit independente.

No skeleton, a alteração de um registro e seu evento de auditoria pertencem à mesma transação. Se a auditoria não puder ser gravada, a alteração também falha. Logs de segurança são outra fonte: um log escrito fora do banco pode permanecer depois de um rollback e não deve ser confundido com uma alteração confirmada.

### Revisão, CAS e ETag

`revision` é um contador do registro. **CAS** (*compare-and-swap*, comparar antes de trocar) significa gravar somente se a revisão que a pessoa leu ainda for a revisão atual. Se Ana e Helena leem a revisão 7, a primeira gravação cria a revisão 8; a segunda não pode sobrescrever essa mudança usando a revisão 7.

A API representa a revisão em um **ETag**, cabeçalho HTTP que identifica a versão da representação. `If-Match` informa o ETag que o cliente pretende alterar. Receber conflito exige reler os dados e decidir o que fazer; reenviar silenciosamente com a revisão nova perderia o objetivo do controle.

Isso é diferente da **geração de deployment**, que identifica o conjunto de software ativo, e da versão `0.2.0` de um pacote.

### Exclusão lógica, tombstone e restauração

**Exclusão lógica** (*soft delete*) marca `deleted_at`, sem apagar fisicamente a linha. **Tombstone** é essa marca de exclusão. As consultas usuais deixam de mostrar o registro, mas UUID, dados e história são preservados.

**Restaurar um registro** significa retirar a marca depois de verificar unicidade e referências ativas. Não cria uma cópia com novo UUID. Se o código comercial já estiver ocupado por outro registro ativo, a restauração falha em vez de sobrescrever o outro.

`with_deleted()` inclui marcas de exclusão na consulta; não remove isolamento nem autorização. E desativar uma conta ou vínculo é uma transição de acesso diferente: restaurar uma identidade não deve reativar automaticamente seus antigos privilégios.

### Triggers, redação e append-only

Um **trigger** é uma rotina do banco acionada por uma mudança de linha. Os triggers são a fonte dos eventos de auditoria: cobrem ORM e SQL, não apenas chamadas Python a `save()`.

**Redação** (*redaction*) significa substituir valores sensíveis por `[REDACTED]` antes de gravá-los no evento. Não significa apenas esconder um valor na tela depois de armazená-lo. Campos novos precisam de declaração e infraestrutura apropriadas; não há detecção garantida de segredos desconhecidos.

**Append-only** significa que o runtime acrescenta eventos, mas não os altera nem exclui. Isso não é **WORM** (*write once, read many*, armazenamento que impediria alteração posterior inclusive por atores privilegiados). DBA, owner de migrations e controlador de um arquivo SQLite continuam fronteiras privilegiadas.

Veja [auditoria, concorrência e exclusão](audit-soft-delete.md) e [API](api.md).

## Apps, pacotes e deployment

### Especificação, manifesto e catálogo

`TableSpec` é uma **especificação de tabela no código**: informa campos autorizados para leitura/escrita, sensibilidade e exposição. É uma allowlist, isto é, uma lista explícita do que é permitido. O **registry** é o registro dessas especificações durante o startup; não cria tabelas.

O **catálogo físico** no banco (`TableDefinition`/`FieldDefinition`) descreve o que realmente existe. Editar sua descrição não altera estrutura nem permissões. São responsabilidades diferentes, não dois lugares alternativos para executar DDL.

`manifest.json` é o **contrato canônico da app**: identidade, versão, dependências, entradas de código, configuração, arquivos e migrations. `AppManifest` é sua representação usada no runtime. “Canônico” significa que não se mantém outro manifesto manual divergente para a mesma app.

### Pacote, wheel, assinatura e publicador

Um **pacote `.skapp`** é um artefato fechado e assinado que pode ser enviado ao instalador. Uma **wheel** é uma distribuição Python pronta para instalação; o wheel do core contém o namespace `skeleton`, não os arquivos de negócio e configuração do projeto derivado.

Um **hash/digest SHA-256** identifica o conteúdo de um arquivo: mudar bytes muda o digest. Uma **assinatura** associa aquele conteúdo a uma chave. O **publicador** (*publisher*) é quem controla a chave autorizada para publicar um release. O **conjunto de chaves confiáveis** (*trust store*) pertence ao operador, não ao pacote recebido.

Uma assinatura válida prova procedência e integridade sob essa política de confiança; não prova que código Python seja inofensivo. Apps e migrations aprovadas continuam código privilegiado. O formato não é uma sandbox.

Um **lock de dependências** fixa versões e hashes. Um **wheelhouse** é o diretório dos wheels disponíveis para montar um ambiente, inclusive sem buscar pacotes na rede. A instalação trabalha em ambiente novo; não faz upgrade solto das bibliotecas usadas pelos workers ativos.

### Versão, instalação e ativação

A **versão do componente**, como `0.2.0`, identifica uma versão declarada do core ou da app. Major/minor/patch descrevem o contrato de compatibilidade; versões e intervalos são comparados por biblioteca, não como texto. `manifest_version` identifica a versão do formato do manifesto, e o número de uma migration identifica um passo de estrutura: não são o mesmo contador.

A **instalação global** diz qual código está disponível para todas as organizações. `ComponentInstallation` representa seu estado confirmado. A **ativação por organização** (`AppInstallation`) decide se uma organização pode usar a app e com qual configuração. Desativar por organização não remove código nem executa migrations.

- `installed`: componente gerenciado presente no release.
- `project-local`: código pertencente ao projeto derivado; seu inventário não autoriza sobrescrevê-lo com um pacote coincidente.
- `schema_retained`: código retirado, com estrutura, dados e histórico preservados; não é uma app ativa parcialmente carregada.

Todas as organizações usam a mesma versão de código por deployment. Reinstalar uma app preserva registros e exige ativação explícita, em vez de ressuscitar acesso só porque o pacote voltou.

### Release, preparação e troca

**Deployment** é a instalação operacional que atende usuários. Um **release** é um conjunto preparado de core, apps, ambiente e recursos. **Staging** é sua preparação em diretório ou imagem separado do conjunto ativo.

A **geração** aumenta quando o conjunto ativo muda. O **inventário** descreve componentes e arquivos daquele conjunto. O **journal** é o registro durável das etapas de uma operação, mantido fora do banco da aplicação para sobreviver a uma restauração desse banco. `DeploymentOperation` é a solicitação e seu acompanhamento auditado no controle da plataforma.

**Cutover**, ou troca do release ativo, é o momento em que novos workers passam a usar o novo conjunto. O agente usa comandos provisionados pelo operador, não shell informado pelo manifesto. O código não é copiado por cima de workers em execução.

**Idempotência** significa reconhecer uma solicitação lógica já recebida sem executar novamente seus efeitos. Ela não elimina conflitos de geração, CAS ou diferenças entre planos. Um status “aprovado” não significa que migrations, checks e reinício já terminaram.

### Backup, rollback e recuperação

**Backup** é uma cópia consistente usada em recuperação. **Restaurar um backup** recoloca dados e recursos naquele estado. Isso é diferente de restaurar uma linha excluída logicamente.

**Rollback do release** significa voltar ao software anterior; não desfaz automaticamente mudanças no banco. Se uma migration já alterou o banco, trocar apenas arquivos pode deixar código e estrutura incompatíveis.

**Manutenção** fecha tráfego de aplicação, drena trabalho em andamento e permite a sequência de backup, migrations, checks e troca. Se houver falha, o gate permanece até provar um estado coerente. Depois que o tráfego reabre, restaurar um backup antigo pode perder novas escritas; exige decisão explícita de recuperação/reconciliação.

Veja [extensão e publicação](extending.md) e [deployment](deployment.md).

## Login e protocolos

### Sessão, CSRF e MFA

Depois do login, a **sessão Django** permite reconhecer a conta nas requisições seguintes por cookie. A API do projeto usa essa sessão; um token do gov.br não vira um token público da API.

**CSRF** é a proteção contra um site externo induzir o navegador autenticado a enviar uma alteração não autorizada. Uma operação que muda estado exige a proteção apropriada, mesmo quando o usuário já está autenticado. Um GET não deve instalar software ou encerrar a sessão.

**MFA** é autenticação com mais de um fator. Ser administrador ou receber um indicador de confiança do provedor externo não dispensa a política de MFA local. `auth_version` permite invalidar sessões quando a política de acesso da identidade muda.

### SSO, IdP, OIDC e SAML

**SSO** (*single sign-on*) é entrar em serviços usando uma identidade de um provedor. O **IdP** (*identity provider*) autentica a pessoa e fornece uma prova. Ele não decide automaticamente suas permissões dentro das organizações do skeleton.

**OAuth 2.0** é um protocolo de autorização de acesso; **OIDC** (*OpenID Connect*) acrescenta a camada de identidade usada nesse login. **SAML** é outro padrão de intercâmbio de provas de identidade, baseado em XML. O skeleton reutiliza allauth e validadores de biblioteca; não implementa parsers próprios desses protocolos.

**Issuer** identifica o emissor da prova. **Subject** identifica a pessoa dentro daquele domínio de identidade. **Audience** identifica para quem a prova foi emitida. Email não substitui essa identidade estável, e coincidência de email ou CPF não autoriza ligar contas automaticamente.

**Claims** são afirmações contidas na prova, como email. Elas precisam ser validadas e minimizadas antes da persistência; não são permissões de tabela ou papéis de plataforma. No perfil gov.br, o `sub` técnico é dado pessoal e não deve ser duplicado em perfil, log ou auditoria de payload.

### State, nonce e PKCE

**State** vincula o retorno ao fluxo de login que o servidor iniciou e é consumido uma vez. **Nonce** vincula o ID token àquela tentativa de autenticação. **PKCE S256** vincula a troca do código ao segredo temporário criado no início do fluxo. São proteções complementares; validar só a assinatura não as substitui.

**Email verificado** exige uma prova aceita pelo perfil, não apenas a presença do endereço no JSON. Quando a admissão é autorizada mas falta email gov.br, allauth pode coletar e confirmar um endereço real. Isso não cria automaticamente uma organização ou um vínculo empresarial.

Testes locais com JWT assinado ou Keycloak real não são **homologação gov.br**. Homologar exige credenciais, URLs autorizadas, ambiente governamental e aprovação do fluxo real. HTTPS, SMTP e infraestrutura produtiva também precisam de provas próprias.

Veja [autenticação e SSO](authentication-sso.md) e [testes e evidências](testing.md).
