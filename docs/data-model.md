# Modelo de dados

Este documento descreve **o que uma linha representa e quais regras ela deve conservar**. Para a visão inicial, leia [identidade e organizações](concepts.md#identidade-e-organizações) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados). As regras de acesso estão em [controle de acesso](access-control.md): ter um identificador não significa ter permissão.

## Uma pessoa, duas organizações

Alice trabalha nas organizações Alfa e Beta. Existe **um `User` de Alice**, com seu email e credenciais, e **um `UserProfile`**, com nome de exibição, idioma e fuso horário. `Profile` é a ideia de perfil; o nome concreto do modelo é `UserProfile`. Nenhum dos dois representa o cargo de Alice em uma organização.

Cada `Organization` representa um espaço organizacional separado. Dois `Membership` ligam o mesmo usuário a Alfa e Beta; cada vínculo pode estar ativo ou inativo e receber papéis diferentes. Desativar o vínculo em Alfa não desativa a identidade nem o vínculo em Beta. Já desativar o `User` impede login e exige verificar os administradores de todas as organizações afetadas.

Uma nota de Alfa pertence a Alfa, mesmo se Alice também trabalha em Beta. Sua organização não pode ser trocada depois da criação. Para uma relação entre dois registros organizacionais, ambos devem pertencer à mesma organização: conhecer o UUID de uma nota de Beta não permite associá-la a um registro de Alfa.

## Como escolher uma base

As bases são **abstratas**: Django copia seus campos para cada modelo concreto, sem criar uma tabela pai universal.

| Necessidade | Base | Consequência |
|---|---|---|
| Entidade própria global, como identidade ou controle de plataforma | `SystemRecord` | UUID, metadados, revisão, exclusão lógica e auditoria; sem organização obrigatória |
| Entidade própria pertencente a uma organização, administrada por serviços específicos | `OrganizationRecord` | Tudo acima e organização obrigatória/imutável; por exemplo, `Membership` |
| Dado de negócio com proprietário e acesso por registro | `SecuredRecord` | Tudo acima e proprietário/visibilidade/grupo; base para negócio organizacional e gateway genérico |
| Tabela de Django ou allauth | Modelo oficial da biblioteca | Não adaptar às bases próprias; registrar a exceção técnica nominal no catálogo |

Herdar a base não instala sozinho a proteção no banco: são necessários `TableSpec` e a migration com `InstallRecordInfrastructure`. Veja [catálogo](table-catalog.md#migration-e-checks) e [extensão](extending.md).

## Identificador não é código comercial

`sysid` é uma chave primária (PK): um UUID gerado pelo servidor que identifica uma linha de forma estável. Um código como `NOTA-2026-001` atende ao negócio, fica em outro campo e pode ter regras próprias de unicidade. Alfa e Beta podem usar esse mesmo código quando a regra é única por organização; as notas continuam tendo UUIDs diferentes. Restaurar uma nota conserva seu UUID, não cria outra identidade.

## Campos herdados

Cada entidade própria concreta tem sua tabela e estes campos de `SystemRecord`:

| Campo | Semântica |
|---|---|
| `sysid` | UUID, chave primária gerada no servidor e imutável; não é código comercial |
| `created_at`, `updated_at` | Data/hora com informação de fuso, armazenada em UTC |
| `created_by`, `updated_by` | UUID histórico do autor; pode ser NULL para sistema/anônimo; não é chave estrangeira |
| `deleted_at` | Marca de exclusão lógica, também chamada tombstone; NULL significa registro vivo |
| `deleted_by` | UUID do autor da exclusão, podendo ser NULL; só preenchido quando `deleted_at` não é NULL |
| `revision` | Inteiro >=1; incrementa exatamente uma vez por UPDATE para detectar concorrência |

`OrganizationRecord` acrescenta `organization`, uma chave estrangeira (FK) obrigatória e imutável com `PROTECT`, e unicidade incondicional `(organization,sysid)`. Django expõe `organization_id`; a coluna física padrão também é `organization_id`. A integridade composta verifica organização e identificador juntos nas relações entre registros organizacionais. O UUID isolado não impede uma referência entre Alfa e Beta.

`SecuredRecord` acrescenta `owner` (User obrigatório), `visibility` (`private`, `group`, `organization`) e `owning_group` (pode ser NULL). O proprietário precisa de Membership ativa na organização. Visibilidade `group` exige um grupo; nas outras visibilidades, `owning_group` deve ser NULL. Esses campos mudam pela gestão de acesso autorizada, nunca pelo PATCH comum. Visibilidade define com quem compartilhar leitura; não concede escrita por si só.

As restrições herdadas recebem nomes com app/model para não colidir entre tabelas. Nas relações próprias, `related_name` evita colisões dos acessos reversos; UUIDs de autores históricos não criam relações reversas nem impedem preservar a história.

## Entidades globais

| Modelo / label | Campos próprios e significado | Invariantes |
|---|---|---|
| User / `sk_accounts.User` | Email normalizado com lowercase+strip; first_name/last_name; password/last_login da biblioteca; is_active/is_staff/is_superuser; auth_version; local_password_allowed | Email único, inclusive entre excluídos. USERNAME_FIELD=email; não há username nem relações próprias groups/user_permissions. Excluído fica inativo. Administrador de plataforma usa staff+superuser; papel organizacional não promove essas flags. |
| UserProfile / `sk_accounts.UserProfile` | user OneToOne PROTECT; display_name; timezone=`UTC`; language=`pt-br` | Criado junto à identidade. Página/API de perfil edita apenas o próprio usuário; plataforma pode administrar perfis no admin global. Fuso validado com zoneinfo e idioma nas opções configuradas. Sem uploads ou negócio. |
| Organization / `sk_tenancy.Organization` | name; slug; is_active | Slug único, inclusive entre excluídos, e imutável. Ciclo de vida controlado pela plataforma; desativar não apaga negócio. |
| TableDefinition / `sk_system.TableDefinition` | database_alias, schema_name, db_table, model_label; label/description; origin, scope, base_class; uuid_managed, soft_delete, audit_mode, api_exposed, available | Chave alias/schema/tabela única. Sincronização controla informações físicas e preserva textos humanos; usuário não pode excluir a entrada. |
| FieldDefinition / `sk_system.FieldDefinition` | table; name (Django), column (física); label/description; type_name; nullable/primary_key; relation_target; sensitive; available | UNIQUE(table,column). Sincronização controla informações físicas e preserva textos humanos. |
| ComponentInstallation / `sk_system.ComponentInstallation` | component_id, kind, app_label/package_name, versão, digest/publisher, geração, estado, referência ao inventário | Global, SystemRecord, identidade imutável/única. Estados distinguem instalado (`installed`), local do projeto (`project-local`) e schema conservado (`schema_retained`). Só controle de plataforma; sem gateway. |
| DeploymentOperation / `sk_system.DeploymentOperation` | operação, estado/etapa, geração esperada, digest/plano aprovado, resultado e error_code | Global, SystemRecord; operação/geração/plano imutáveis; aprovação usa CAS. Plan/result JSON ocultados integralmente na auditoria; tickets efêmeros não são persistidos. |

## Entidades organizacionais

Todas abaixo herdam `OrganizationRecord`; relações próprias usam PROTECT.

Um **papel** (`Role`) reúne permissões; um **grupo** (`AccessGroup`) reúne membros e pode receber papéis. `MemberRole`, `GroupMember` e `GroupRole` são os registros explícitos dessas associações. `TablePermission` permite uma ação em uma tabela; `RecordGrant` concede uma ação em um registro específico, sem dispensar a permissão de tabela. Por exemplo, Alice pode ser administradora em Alfa e apenas membro de um grupo de leitura em Beta. Os papéis de Alfa não atravessam essa fronteira.

| Modelo | Campos próprios | Unicidade/regras |
|---|---|---|
| Membership | user; is_active | UNIQUE(org,user) incondicional; user/org imutáveis; novo convite reativa a mesma linha, não cria duplicata |
| Role | code,name,description,is_builtin | Código único entre vivos por organização; papéis internos organization_admin/member não podem ser removidos/renomeados |
| MemberRole | membership,role | Par único entre vivos; mesma organização; revogação lógica |
| AccessGroup | code,name,description | Código único entre vivos por organização; grupo de negócio, não auth.Group |
| GroupMember | group,membership | Par único entre vivos; Membership precisa estar ativa |
| GroupRole | group,role | Par único entre vivos; atribui um papel ao grupo |
| TablePermission | role,table,action | Tripla única viva; ações read/create/update/delete/restore/manage_access/audit; ausência de permissão nega acesso |
| RecordGrant | table,record_sysid,membership/group/role,action | Exatamente um destinatário; read/update/delete; combinação registro/destinatário/ação única entre vivos; alvo vivo, autorizado e da mesma organização |
| Invitation | email,role,token_digest,expires_at,accepted_at,accepted_by | Resumo SHA256 único; token original só na mensagem; validade sete dias; accepted_by é UUID histórico; papel da mesma organização |

`TablePermission.table` e `RecordGrant.table` apontam ao catálogo global. `record_sysid` guarda o UUID do alvo, mas não é `GenericForeignKey`: o serviço encontra o modelo no registro autorizado em código e valida o acesso ao registro. Administradores precisam revogar dependências antes de excluir papéis/grupos. Alterações que podem retirar o último administrador usam bloqueios e transação `atomic`, para duas mudanças simultâneas não deixarem uma organização sem administrador.

Criar uma organização também é uma unidade transacional: `create_organization` confirma organização, papéis iniciais, vínculo administrador e permissões juntos. Se o proprietário foi desativado enquanto o formulário estava aberto, ou outra validação falha no bootstrap, a operação desfaz todo esse conjunto. Não deve permanecer uma organização ativa sem administrador nem um slug ocupado por uma criação incompleta.

`sk_system.AppInstallation` deriva de OrganizationRecord: `app_label` imutável, `enabled` e `configuration` JSON (ocultado integralmente na auditoria), UNIQUE incondicional `(organization,app_label)`. Esse estado organiza ativação/configuração; não instala código nem altera o schema. Sem linha, usam-se os valores padrão do manifesto de deployment. `organization_admin` faz a gestão com revisão/CAS, bloqueio da organização e validação de dependências.

`ComponentInstallation` e `DeploymentOperation`, os dois modelos de controle global, recebem infraestrutura congelada em `sk_system.0004_deployment`. O banco reflete o inventário e o diário (`journal`) do agente; não escolhe o código carregado pelos processos web. Desinstalar globalmente preserva `AppInstallation` e sua configuração. Sem código, só é permitido desabilitar o estado existente. Reinstalar exige ativação explícita, sem substituir UUIDs ou valores padrão já persistidos.

`sk_system.0005_deploymentoperation_lifecycle` acrescenta `reinstall` e `core_update` ao contrato de operação, sem alterar colunas/infraestrutura da migration anterior. Falha de migration conserva o modo de manutenção; recuperação offline reconciliada aparece como `restored` no histórico.


## AuditEvent

Este é o registro histórico de uma alteração, não o próprio objeto alterado. O evento conserva identificadores mesmo quando uma identidade é desativada ou um objeto deixa de estar disponível. [Auditoria e concorrência](concepts.md#auditoria-e-concorrência) explica revisão e transações; [auditoria e exclusão lógica](audit-soft-delete.md) detalha os limites.

`sk_audit.AuditEvent` é estruturalmente global, mas possui `organization_id` UUID que pode ser NULL para controlar acesso por RLS. Campos: `table_name`, `model_label`, `record_sysid`, `record_revision`, `operation`, `actor_sysid`, `request_id`, `source`, `db_principal`, `changes` JSON. Não tem FK a usuário, organização, registro ou catálogo: eventos sobrevivem aos objetos históricos.

`operation`: create/update/soft_delete/restore/hard_delete. Cada item de `changes` contém old/new/redacted, isto é, valor anterior, valor novo e indicação de ocultação de segredo. A `revision` do evento é sempre 1; `deleted_at`/`deleted_by` são sempre NULL. UPDATE/DELETE são proibidos: eventos só podem ser acrescentados. Exclusão física não é oferecida à aplicação; um evento `hard_delete` só pode existir após manutenção offline revisada que remova o guard (proteção no banco).

## Exemplo opt-in

`example_notes.Note` herda SecuredRecord: `code` CharField(64), `title` CharField(200), `body` TextField opcional com limite 10000; `(organization,code)` único entre vivos. Body é sensível na auditoria. Campos de acesso não pertencem à edição normal. Veja [extending](extending.md).

## Nomes e exceções

Um label como `example_notes.note` identifica o modelo no ORM e no registro de especificações em código; não garante o nome da tabela SQL. O nome físico efetivo vem de `_meta.db_table` e do catálogo. APIs representam UUIDs com hífens; SQLite armazena UUIDField como hexadecimal e PostgreSQL como tipo `uuid` nativo.

Tabelas técnicas de Django/allauth mantêm chaves primárias, campos e ciclo de vida oficiais. Elas guardam infraestrutura — por exemplo, sessão e confirmação de email —, não entidades próprias de negócio. Isso não viola o contrato de UUID das entidades **próprias**; as exceções são nominais em [catálogo](table-catalog.md#exceções-técnicas-nominais). Não converta estruturas de terceiros por migration improvisada.
