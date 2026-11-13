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

Há uma quarta base, **`OperationalRecord`**, reservada ao estado de pipeline do núcleo (jobs, outbox, entregas, leitura de notificações). Ela não é para negócio: veja a decisão abaixo.

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
| UserProfile / `sk_accounts.UserProfile` | user OneToOne PROTECT; display_name; timezone e language, em branco por padrão (seguem a organização e a instância, #170) | Criado junto à identidade. Página/API de perfil edita apenas o próprio usuário; a plataforma altera só nome, sobrenome e flags da identidade em `/platform/users/<id>/`. Fuso validado com zoneinfo e idioma nas opções configuradas. Sem uploads ou negócio. |
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
| OrganizationSettings / `sk_tenancy.OrganizationSettings` | display_name; default_language; default_timezone; brand_color (`#RRGGBB`); logo_key (sensível) | Uma linha por organização (UNIQUE), criada na primeira gravação; antes dela a revisão virtual é 0. Só o administrador grava, por `OrganizationService.update_settings` ou `/o/<org>/settings/`, com CAS. Valores em branco usam o padrão da instância. `logo_key` muda só pelo upload do logo e é redigido na auditoria. Fora do gateway ([#167](https://github.com/alaorneto/skeleton/issues/167)). |
| Membership | user; is_active | UNIQUE(org,user) incondicional; user/org imutáveis; novo convite reativa a mesma linha, não cria duplicata |
| Role | code,name,description,is_builtin | Código único entre vivos por organização; papéis internos (`organization_admin`, `people_manager`, `app_manager`, `member`, declarados em `BUILTIN_ROLES`) não podem ser removidos/renomeados e são os únicos que carregam capacidades |
| MemberRole | membership,role | Par único entre vivos; mesma organização; revogação lógica |
| AccessGroup | code,name,description | Código único entre vivos por organização; grupo de negócio, não auth.Group |
| GroupMember | group,membership | Par único entre vivos; Membership precisa estar ativa |
| GroupRole | group,role | Par único entre vivos; atribui um papel ao grupo |
| TablePermission | role,table,action | Tripla única viva; ações read/create/update/delete/restore/manage_access/audit; ausência de permissão nega acesso |
| RecordGrant | table,record_sysid,membership/group/role,action | Exatamente um destinatário; read/update/delete; combinação registro/destinatário/ação única entre vivos; alvo vivo, autorizado e da mesma organização |
| OrganizationAdmissionPolicy / `sk_tenancy.OrganizationAdmissionPolicy` | discoverable; join_requests_enabled; request_default_role (FK Role); join_links_enabled; signup_via_link_enabled; domain_join_enabled | Uma linha por organização (UNIQUE), sem linha tudo desligado; revisão virtual 0. Só `admission.configure` grava, por `configure_policy`, com CAS; papel da mesma organização e nunca `organization_admin`. Fora do gateway ([#26](https://github.com/alaorneto/skeleton/issues/26)). |
| OrganizationDirectoryEntry / `sk_tenancy.OrganizationDirectoryEntry` | listed_organization (FK, única); discoverable; accepts_requests; accepts_domain | `SystemRecord` global, lido antes de haver tenant por `/join/`; escrito só pelo serviço da política, na mesma transação. FK `listed_organization`, nunca `organization` (E017). |
| IdentityProvider / `sk_accounts.IdentityProvider` | provider_id (único); protocol (oidc/saml/ldap); bound_organization (FK); issuer; entity_id; directory (URL LDAP com a base, #32); client_id; sp_entity_id; fingerprint (v2); name; enabled; show_on_login; trust_verified_email; settings (allowlist por protocolo); secret e private_key (cifrados, sensíveis); last_check_at; last_check_code | `SystemRecord` global cadastrado pela plataforma; identidade e fingerprint imutáveis (guard e serviço); CHECK de identidade por protocolo. O `SocialApp` do allauth é montado em memória ([#29](https://github.com/alaorneto/skeleton/issues/29)). |
| IdentityProviderAdmission / `sk_tenancy.IdentityProviderAdmission` | provider (FK, único, imutável); jit_provisioning; jit_requires_approval; default_role (FK nula); allowed_email_domains (lista ≤50) | Regras da organização para o seu provedor; sem linha, JIT desligado. Lida pelo adapter em `sso:validated`. |
| VerifiedDomain / `sk_tenancy.VerifiedDomain` | domain (IDNA, imutável); challenge (público, imutável); status (pending/verified/failed); admission (auto/approval); role; verified_at; last_checked_at; failure_count; last_error_code | Único entre vivos por (organização, domínio); `verified` exige `verified_at`. Só `admission.configure` cadastra, verifica e remove. Fora do gateway ([#28](https://github.com/alaorneto/skeleton/issues/28)). |
| DomainClaim / `sk_tenancy.DomainClaim` | domain (único entre vivos, imutável); claiming_organization (FK, imutável) | `SystemRecord` global consultado antes de haver tenant; escrito só pela verificação, na mesma transação do `VerifiedDomain`; revogado pela organização, pela reverificação ou pela plataforma. FK `claiming_organization` (E017). |
| JoinLink / `sk_tenancy.JoinLink` | label; token_digest (sensível, único); role; mode (direct/approval); expires_at (≤90 dias); max_uses (1–10.000); use_count | Só o SHA-256 do token; CHECK `0 ≤ use_count ≤ max_uses`; resumo, papel, modo, validade e limite imutáveis; revogar é exclusão lógica. Criado e revogado por `invitations.manage` com a política ligada. Fora do gateway ([#27](https://github.com/alaorneto/skeleton/issues/27)). |
| JoinRequest / `sk_tenancy.JoinRequest` | user (imutável); channel (directory/domain/link/sso, imutável); join_link (imutável, nulo); role; status; message (≤500); expires_at; decided_at; decided_by | Um pedido aberto por (organização, pessoa); CHECK de coerência da decisão (decisão tem data; só decisão tem decisor; recusa sempre tem decisor; aprovação sem decisor é a automática do link direto depois da confirmação do email). Autodescoberta por `user_id`: o solicitante lê os próprios pedidos em todas as organizações, escritas só no tenant. Expira em 30 dias (derivado). Fora do gateway. |
| Invitation | email,role,token_digest,expires_at,accepted_at,accepted_by | Resumo SHA256 único; token original só na mensagem; validade sete dias; accepted_by é UUID histórico; papel da mesma organização |

`TablePermission.table` e `RecordGrant.table` apontam ao catálogo global. `record_sysid` guarda o UUID do alvo, mas não é `GenericForeignKey`: o serviço encontra o modelo no registro autorizado em código e valida o acesso ao registro. Administradores precisam revogar dependências antes de excluir papéis/grupos. Alterações que podem retirar o último administrador usam bloqueios e transação `atomic`, para duas mudanças simultâneas não deixarem uma organização sem administrador.

Criar uma organização também é uma unidade transacional: `create_organization` confirma organização, papéis iniciais, vínculo administrador e permissões juntos. Se o proprietário foi desativado enquanto o formulário estava aberto, ou outra validação falha no bootstrap, a operação desfaz todo esse conjunto. Não deve permanecer uma organização ativa sem administrador nem um slug ocupado por uma criação incompleta.

`sk_system.AppInstallation` deriva de OrganizationRecord: `app_label` imutável, `enabled` e `configuration` JSON (ocultado integralmente na auditoria), UNIQUE incondicional `(organization,app_label)`. Esse estado organiza ativação/configuração; não instala código nem altera o schema. Sem linha, a app está desativada na organização (#22); a configuração usa os defaults dos campos. `organization_admin` faz a gestão com revisão/CAS, bloqueio da organização e validação de dependências.

`sk_system.AppAvailability` (SystemRecord global) guarda, por app instalada, `app_label` único e imutável, `mode` (`all`/`selected`) e `upgrade_state` (`""` numa linha anterior ao #22 ainda não processada, `pending` durante `upgrade_app_activation`, `done` depois; código novo sempre grava `done`). Sem linha, a app está indisponível para todas as organizações. Os defaults dos modelos também são fechados (#283): `AppAvailability.mode` nasce `selected` e `AppInstallation.enabled` nasce `false`; todo código do núcleo passa o valor explicitamente, e o default só protege uma linha criada sem ele. `sk_system.AppOrganizationGrant` (SystemRecord global) liga uma disponibilidade a `granted_organization` (FK PROTECT), com UNIQUE incondicional; revogar é exclusão lógica e re-conceder restaura a mesma linha. Nenhuma das duas escreve linhas tenant nem entra no gateway.

`ComponentInstallation` e `DeploymentOperation`, os dois modelos de controle global, recebem infraestrutura congelada em `sk_system.0004_deployment`. O banco reflete o inventário e o diário (`journal`) do agente; não escolhe o código carregado pelos processos web. Desinstalar globalmente preserva `AppInstallation` e sua configuração. Sem código, só é permitido desabilitar o estado existente. Reinstalar exige ativação explícita, sem substituir UUIDs ou valores padrão já persistidos.

`sk_system.0005_deploymentoperation_lifecycle` acrescenta `reinstall` e `core_update` ao contrato de operação, sem alterar colunas/infraestrutura da migration anterior. Falha de migration conserva o modo de manutenção; recuperação offline reconciliada aparece como `restored` no histórico.


## Fluxos de processo

As seis tabelas de `sk_flows` (`sk_flows.0001_initial`) derivam de `OrganizationRecord`, com relações `PROTECT` de mesmo tenant e infraestrutura congelada na migration. Nenhuma entra no gateway genérico: só os serviços de `skeleton.flows` as escrevem, e a autorização é derivada do registro de negócio e da participação. Detalhe em [fluxos](flows.md#modelo-de-dados).

| Modelo | Campos próprios | Unicidade/regras |
|---|---|---|
| FlowDefinition | key, version, app_label, label, digest, specification (JSON) | UNIQUE(org,key,version); todos os campos imutáveis: uma versão publicada nunca muda |
| FlowInstance | definition, flow_key, flow_version, number, subject_table, subject_sysid, status, outcome, starter, data, sensitive_data, idempotency_key, run_count, activity_count, finished_at | Uma instância ativa (`running`/`suspended`) por fluxo e registro de negócio; chave de idempotência única por fluxo; `sensitive_data` redigido na auditoria; `subject_table` + `subject_sysid` não é `GenericForeignKey`: o serviço resolve o modelo no registro em código |
| FlowStepRun | instance, step_key, kind, status, sequence, scope, branch, via, outcome, state, due_at, finished_at | UNIQUE(instance,sequence); `scope` aponta para a ativação da bifurcação que criou o ramo |
| FlowTask | instance, step_run, step_key, kind, status, slot, assignee, candidate_group, candidate_role, delegated_from, decision, action, comment, due_at, reminded_count, escalated_at, completed_by, completed_at | No máximo uma fila (grupo ou papel); a fila é limpa quando a tarefa fecha, para só tarefas abertas impedirem a remoção de um grupo ou papel; `comment` redigido na auditoria |
| FlowSubmission | instance, step_run, task, step_key, transition_key, origin, values, sensitive_values | Todos os campos imutáveis; `sensitive_values` redigido na auditoria |
| FlowActivity | instance, sequence, kind, step_key, step_run, task, detail, comment | UNIQUE(instance,sequence); todos os campos imutáveis; `comment` redigido na auditoria |

Essas tabelas são dado de negócio, não estado de pipeline: têm revisão, exclusão lógica e auditoria por linha. O que é operacional (timers, retentativas, entregas) usa a fila de jobs.

## Tabelas operacionais (ADR)

**Contexto.** Fila de jobs, outbox de eventos, entregas e estado de leitura de notificações mudam a cada claim, heartbeat ou leitura. Com `SystemRecord`, cada UPDATE geraria um `AuditEvent`, e a role runtime não tem DELETE: essas tabelas cresceriam para sempre e inflariam a auditoria.

**Decisão.** `OperationalRecord` (`skeleton/common/models.py`) tem `sysid` UUID, `created_at` e `updated_at`; não tem revisão, exclusão lógica nem auditoria. O manager filtra o tenant quando há FK `organization`, permite `update`, `delete` e `bulk_create` e recusa `bulk_update` e mudança de campos imutáveis. A migration congela `lifecycle: operational` no spec de `InstallRecordInfrastructure`, que instala: guard de imutabilidade (`sysid`, `organization_id` e `immutable_fields`), RLS tenant quando há `organization` (com `self_discovery_column` opcional), FKs compostas de mesmo tenant, `GRANT SELECT, INSERT, UPDATE, DELETE` à role runtime e nenhum trigger de auditoria. O catálogo registra `audit_mode=operational` e `base_class=OperationalRecord`; `check_skeleton_schema` compara triggers, grants, políticas e a ausência da função de auditoria, e o check E020 reprova tabela operacional exposta ou com `write_fields`.

**Consequências.** A exclusão física fica disponível ao runtime, mas só o serviço de retenção declarado em código a usa. A auditoria continua a fonte de verdade do negócio: um evento de domínio ou um job carregam `record_revision`/`request_id` para correlação, não substituem o `AuditEvent`. Desligar a auditoria de uma tabela de negócio por flag foi descartado: a base separada deixa o contrato explícito e verificável.

## AuditEvent

Este é o registro histórico de uma alteração, não o próprio objeto alterado. O evento conserva identificadores mesmo quando uma identidade é desativada ou um objeto deixa de estar disponível. [Auditoria e concorrência](concepts.md#auditoria-e-concorrência) explica revisão e transações; [auditoria e exclusão lógica](audit-soft-delete.md) detalha os limites.

`sk_audit.AuditEvent` é estruturalmente global, mas possui `organization_id` UUID que pode ser NULL para controlar acesso por RLS. Campos: `table_name`, `model_label`, `record_sysid`, `record_revision`, `operation`, `actor_sysid`, `request_id`, `source`, `db_principal`, `changes` JSON. Não tem FK a usuário, organização, registro ou catálogo: eventos sobrevivem aos objetos históricos.

A tabela recebe uma linha a cada escrita do sistema, então cada índice tem uma consulta que o lê (#270):

| Índice | Colunas | Consulta |
|---|---|---|
| `sk_audit_org_created` | `(organization_id, created_at)` | log da organização e log da plataforma (`organization_id IS NULL`), do mais novo ao mais antigo |
| `sk_audit_record_created` | `(record_sysid, created_at)` | histórico de um registro e filtro por registro do log |
| `sk_audit_org_actor` | `(organization_id, actor_sysid, created_at)` | filtro por ator do log (#184) |
| `sk_audit_request` | `(request_id)` | tudo o que uma requisição fez (#184) |

Um índice novo entra só junto da consulta que o usa.

`operation`: create/update/soft_delete/restore/hard_delete. Cada item de `changes` contém old/new/redacted, isto é, valor anterior, valor novo e indicação de ocultação de segredo. A `revision` do evento é sempre 1; `deleted_at`/`deleted_by` são sempre NULL. UPDATE/DELETE são proibidos: eventos só podem ser acrescentados. Exclusão física não é oferecida à aplicação; um evento `hard_delete` só pode existir após manutenção offline revisada que remova o guard (proteção no banco).

## Exemplo opt-in

`example_notes.Note` herda SecuredRecord: `code` CharField(64), `title` CharField(200), `body` TextField opcional com limite 10000; `(organization,code)` único entre vivos. Body é sensível na auditoria. Campos de acesso não pertencem à edição normal. Veja [extending](extending.md).

## Nomes e exceções

Um label como `example_notes.note` identifica o modelo no ORM e no registro de especificações em código; não garante o nome da tabela SQL. O nome físico efetivo vem de `_meta.db_table` e do catálogo. APIs representam UUIDs com hífens; PostgreSQL armazena UUIDField como tipo `uuid` nativo.

Tabelas técnicas de Django/allauth mantêm chaves primárias, campos e ciclo de vida oficiais. Elas guardam infraestrutura — por exemplo, sessão e confirmação de email —, não entidades próprias de negócio. Isso não viola o contrato de UUID das entidades **próprias**; as exceções são nominais em [catálogo](table-catalog.md#exceções-técnicas-nominais). Não converta estruturas de terceiros por migration improvisada.
