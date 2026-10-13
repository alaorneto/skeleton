# Modelo de dados

## Campos herdados

Cada entidade própria concreta tem sua tabela e estes campos de `SystemRecord`:

| Campo | Semântica |
|---|---|
| `sysid` | UUID PK gerado no servidor, imutável, não código comercial |
| `created_at`, `updated_at` | Data/hora aware em UTC |
| `created_by`, `updated_by` | UUID histórico do ator; nullable para sistema/anônimo; não FK |
| `deleted_at` | Tombstone; NULL significa vivo |
| `deleted_by` | Ator UUID nullable; só preenchido se deleted_at não NULL |
| `revision` | Inteiro >=1; incrementa exatamente uma vez por UPDATE |

`OrganizationRecord` acrescenta `organization` FK PROTECT obrigatória/imutável e UNIQUE incondicional `(organization,sysid)`. Django expõe `organization_id`; a coluna física padrão é `organization_id`. Integridade composta assegura relações org→org; UUID globalmente único por si só não assegura same-tenant.

`SecuredRecord` acrescenta `owner` User obrigatório, `visibility` (`private`, `group`, `organization`) e `owning_group` nullable. Owner tem Membership ativa do tenant. `group` exige grupo; outras visibilidades exigem NULL. A gestão de acesso, não PATCH comum, altera esses campos.

Constraints herdadas usam nomes com app/model para não colidir. Bases são abstratas; não geram tabela pai. `related_name` de relações próprias evita colisões; atores históricos não geram relações reversas.

## Entidades globais

| Modelo / label | Campos próprios e significado | Invariantes |
|---|---|---|
| User / `sk_accounts.User` | email normalizado lowercase+strip; first_name/last_name; password/last_login da biblioteca; is_active/is_staff/is_superuser; auth_version; local_password_allowed | Email único incluindo tombstone. USERNAME_FIELD=email. Sem username, groups/user_permissions próprios. Deletado é inativo. Platform admin usa staff+superuser; papel tenant não promove flags. |
| UserProfile / `sk_accounts.UserProfile` | user OneToOne PROTECT; display_name; timezone=`UTC`; language=`pt-br` | Criado com identidade; a página/API de perfil edita somente o próprio usuário. Plataforma pode administrar perfis pelo admin global. Timezone zoneinfo e idioma das opções configuradas. Sem uploads ou negócio. |
| Organization / `sk_tenancy.Organization` | name; slug; is_active | Slug único inclusive tombstones e imutável. Lifecycle de plataforma, sem apagar negócio ao desativar. |
| TableDefinition / `sk_system.TableDefinition` | database_alias, schema_name, db_table, model_label; label/description; origin, scope, base_class; uuid_managed, soft_delete, audit_mode, api_exposed, available | Chave alias/schema/tabela única; físicos pertencem ao sync, textos humanos preservados; sem delete de usuário. |
| FieldDefinition / `sk_system.FieldDefinition` | table; name (Django), column (física); label/description; type_name; nullable/primary_key; relation_target; sensitive; available | UNIQUE(table,column); físicas pertencem ao sync, textos humanos preservados. |
| ComponentInstallation / `sk_system.ComponentInstallation` | component_id, kind, app_label/package_name, versão, digest/publisher, geração, estado, referência ao inventário | Global, SystemRecord, identidade imutável/única; distingue installed/project-local/schema_retained. Só controle de plataforma; sem gateway. |
| DeploymentOperation / `sk_system.DeploymentOperation` | operação, estado/etapa, geração esperada, digest/plano aprovado, resultado e error_code | Global, SystemRecord; operação/geração/plano imutáveis, aprovação CAS. Plan/result JSON integralmente redigidos; tickets efêmeros não são persistidos. |

## Entidades organizacionais

Todas abaixo herdam `OrganizationRecord`; relações próprias usam PROTECT.

| Modelo | Campos próprios | Unicidade/regras |
|---|---|---|
| Membership | user; is_active | UNIQUE(org,user) incondicional; user/org imutáveis; reconvite reativa a linha, não duplica |
| Role | code,name,description,is_builtin | Code único entre vivos por org; builtins organization_admin/member não removíveis/renomeáveis |
| MemberRole | membership,role | Par único entre vivos; mesma org; revogação lógica |
| AccessGroup | code,name,description | Code único entre vivos por org; grupo empresarial, não auth.Group |
| GroupMember | group,membership | Par único entre vivos; Membership ativa |
| GroupRole | group,role | Par único entre vivos; atribui role ao grupo |
| TablePermission | role,table,action | Tripla única viva; ações read/create/update/delete/restore/manage_access/audit; default deny |
| RecordGrant | table,record_sysid,membership/group/role,action | Exatamente um sujeito; read/update/delete; unique vivo por registro/sujeito/ação; alvo vivo autorizado mesma org |
| Invitation | email,role,token_digest,expires_at,accepted_at,accepted_by | Digest SHA256 único; token plaintext só mensagem; validade sete dias; accepted_by UUID histórico; role mesmo tenant |

`TablePermission.table` e `RecordGrant.table` apontam ao catálogo global. `record_sysid` não é GenericForeignKey: o serviço resolve modelo registrado e autoriza o registro. Administradores precisam revogar dependências antes de excluir papéis/grupos; alterações capazes de retirar último admin usam locks/atomic.

`sk_system.AppInstallation` deriva de OrganizationRecord: app_label imutável, enabled e configuration JSON (redigido integralmente na auditoria), UNIQUE incondicional (organization,app_label). O estado não instala código nem altera schema; ausência de linha usa defaults do manifest de deployment. Gestão por organization_admin com revisão/CAS, lock da organização e validação de dependências.

Os dois modelos de controle global recebem infraestrutura congelada em `sk_system.0004_deployment`. O banco reflete inventário/journal do agente; não escolhe o código carregado pelos workers. Desinstalação global preserva `AppInstallation` e sua configuração; com código ausente, apenas desabilitar estado existente é permitido. Reinstalação exige ativação explícita, sem substituir UUIDs ou defaults persistidos.

`sk_system.0005_deploymentoperation_lifecycle` acrescenta as ações explícitas `reinstall` e `core_update` ao contrato de operação, sem alterar colunas/infraestrutura da migration anterior. Falha de migration conserva manutenção; recuperação offline reconciliada aparece como `restored` no histórico.


## AuditEvent

`sk_audit.AuditEvent` é global estruturalmente, mas tem `organization_id` UUID nullable para acesso RLS. Campos: `table_name`, `model_label`, `record_sysid`, `record_revision`, `operation`, `actor_sysid`, `request_id`, `source`, `db_principal`, `changes` JSON. Nenhuma FK a usuário, tenant, registro ou catálogo: eventos sobrevivem aos objetos históricos.

`operation`: create/update/soft_delete/restore/hard_delete. Cada item de `changes` contém old/new/redacted. Revision do evento é sempre 1; deleted_at/deleted_by sempre NULL. UPDATE/DELETE proibidos. Hard delete não é oferecido à aplicação; só pode existir após maintenance offline que remova guard de forma revisada.

## Exemplo opt-in

`example_notes.Note` herda SecuredRecord: `code` CharField(64), `title` CharField(200), `body` TextField opcional com limite 10000; `(organization,code)` único entre vivos. Body é sensível na auditoria. Campos de acesso não pertencem à edição normal. Veja [extending](extending.md).

## Nomes e exceções

Labels são referências de ORM/registry, não nomes de tabela garantidos. O nome físico efetivo vem de `_meta.db_table` e do catálogo. APIs normalizam UUID com hífens; SQLite armazena UUIDField como hex e PostgreSQL como uuid nativo.

Tabelas técnicas de Django/allauth mantêm PKs, campos e lifecycle oficiais. Isso não viola o contrato de UUID das entidades **próprias**; as exceções são nominais em [table-catalog](table-catalog.md). Não converta schemas de terceiros por migration ad hoc.
