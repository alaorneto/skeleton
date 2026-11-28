# Modelo de dados

Este documento descreve **o que cada registro representa e quais regras ele deve preservar**. Para uma visão introdutória, leia [identidade e organizações](concepts.md#identidade-e-organizações) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados). As regras de autorização estão em [controle de acesso](access-control.md): conhecer um identificador não concede permissão.

## Uma pessoa, duas organizações

Suponha que Alice trabalhe nas organizações Alfa e Beta. Existe **um único `User`** para Alice, com seu email e suas credenciais, e **um único `UserProfile`**, com nome de exibição, idioma e fuso horário. `Profile` é o conceito de perfil; o modelo concreto chama-se `UserProfile`. Nenhum dos dois representa o papel de Alice dentro de uma organização.

Cada `Organization` define um espaço organizacional isolado. Dois registros de `Membership` vinculam Alice a Alfa e a Beta; cada vínculo pode estar ativo ou inativo e receber papéis distintos. Desativar o vínculo em Alfa não afeta a identidade de Alice nem seu vínculo em Beta. Já uma pessoa externa do portal não possui `Membership`: o vínculo dela é `ExternalAccess` ([acesso externo](access-control.md#acesso-externo-portal)). Por outro lado, desativar o `User` impede o login em toda a plataforma e exige verificar se nenhuma organização afetada ficará sem administrador.

Uma nota criada em Alfa pertence exclusivamente a Alfa, mesmo que Alice também atue em Beta. A organização de um registro não pode ser alterada após a criação. Em relações entre dois registros organizacionais, ambos precisam pertencer à mesma organização: conhecer o UUID de uma nota de Beta não permite associá-la a um registro de Alfa.

## Como escolher uma base

As classes base de registro são **abstratas**: o Django copia seus campos para cada modelo concreto, sem criar uma tabela pai compartilhada.

| Necessidade | Base | Consequência |
|---|---|---|
| Entidade própria global, como identidade ou controle de plataforma | `SystemRecord` | UUID, metadados, revisão, exclusão lógica e auditoria, sem organização obrigatória |
| Entidade própria pertencente a uma organização, administrada por serviços específicos | `OrganizationRecord` | Tudo acima mais organização obrigatória e imutável (por exemplo, `Membership`) |
| Dado de negócio com proprietário e controle de acesso por registro | `SecuredRecord` | Tudo acima mais proprietário, visibilidade e grupo; base para negócio organizacional e gateway genérico |
| Tabela de Django ou allauth | Modelo oficial da biblioteca | Não adaptar às bases próprias; registrar a exceção técnica nominal no catálogo |

Um `OrganizationRecord` não exposto que pertence a um registro de negócio — como o item de uma área de trabalho ou a linha de um documento — pode atuar como **tabela derivada**: ele declara `TableSpec.access_parent` e tem seu acesso decidido pelo `SecuredRecord` exposto do pai, sem autorização própria ([tabelas derivadas](access-control.md#tabelas-derivadas)).

Existe ainda uma quarta base, **`OperationalRecord`**, reservada ao estado de pipelines do núcleo (jobs, outbox, entregas e estado de leitura de notificações). Ela não deve ser usada para dados de negócio; veja a seção [Tabelas operacionais (ADR)](#tabelas-operacionais-adr).

Herdar a classe base não instala automaticamente a proteção no banco de dados: é necessário registrar o `TableSpec` e criar a migration com `InstallRecordInfrastructure`. Consulte [catálogo](table-catalog.md#migration-e-checks) e [extensão](extending.md).

## Identificador não é código comercial

`sysid` é a chave primária (PK): um UUID gerado pelo servidor que identifica a linha de forma estável. Códigos de negócio, como `NOTA-2026-001`, ficam em campos próprios e seguem regras de unicidade específicas. Alfa e Beta podem utilizar esse mesmo código quando a unicidade é por organização; ainda assim, as notas mantêm UUIDs diferentes. Restaurar um registro excluído logicamente preserva seu `sysid` original, sem criar uma nova identidade.

## Campos herdados

Cada entidade própria concreta tem sua tabela e herda os seguintes campos de `SystemRecord`:

| Campo | Semântica |
|---|---|
| `sysid` | UUID, chave primária gerada pelo servidor e imutável; não é código comercial |
| `created_at`, `updated_at` | Data e hora com fuso, armazenadas em UTC |
| `created_by`, `updated_by` | UUID histórico do autor; pode ser `NULL` para sistema ou ação anônima; não é chave estrangeira |
| `deleted_at` | Marca de exclusão lógica (tombstone); `NULL` indica registro vivo |
| `deleted_by` | UUID do autor da exclusão, podendo ser `NULL`; preenchido apenas quando `deleted_at` não é `NULL` |
| `revision` | Inteiro `>= 1`; incrementado exatamente uma vez por `UPDATE` para detectar concorrência |

`OrganizationRecord` acrescenta `organization` — uma chave estrangeira (FK) obrigatória e imutável com `PROTECT` — e unicidade incondicional em `(organization, sysid)`. O Django expõe o atributo `organization_id`, que corresponde à coluna física padrão `organization_id`. A integridade referencial composta verifica organização e identificador em conjunto nas relações entre registros organizacionais, pois o UUID isolado não impediria uma referência cruzada entre Alfa e Beta.

`SecuredRecord` acrescenta `owner` (`User` obrigatório), `visibility` (`private`, `group` ou `organization`) e `owning_group` (pode ser `NULL`). O proprietário precisa manter `Membership` ativa na organização. A visibilidade `group` exige um grupo definido; nas demais visibilidades, `owning_group` deve ser `NULL`. Esses campos são alterados exclusivamente pela gestão de acesso autorizada, nunca pelo `PATCH` comum. Além disso, visibilidade controla apenas compartilhamento de leitura e não concede permissão de escrita por si só.

As restrições herdadas recebem nomes qualificados com app e modelo para evitar colisões entre tabelas. Nas relações próprias, o `related_name` previne colisões nos acessos reversos; os UUIDs de autores históricos não criam relações reversas nem dificultam a preservação do histórico.

## Entidades globais

| Modelo / label | Campos próprios e significado | Invariantes |
|---|---|---|
| User / `sk_accounts.User` | Email normalizado (minúsculas e sem espaços nas pontas); `first_name` e `last_name`; `password` e `last_login` da biblioteca; `is_active`, `is_staff` e `is_superuser`; `auth_version`; `local_password_allowed` | Email único, inclusive entre excluídos. `USERNAME_FIELD=email`; não há `username` nem relações próprias `groups` ou `user_permissions`. Registro excluído fica inativo. Administrador de plataforma exige `is_staff` e `is_superuser`; papel organizacional não altera essas flags. |
| UserProfile / `sk_accounts.UserProfile` | `user` (`OneToOne`, `PROTECT`); `display_name`; `timezone` e `language`, em branco por padrão (herdam da organização e da instância); `color_scheme` (`""` segue o sistema, `light` ou `dark`) | Criado junto com a identidade. A página e a API de perfil editam apenas o próprio usuário; a plataforma altera apenas nome, sobrenome e flags da identidade em `/platform/users/<id>/`. Fuso validado com `zoneinfo`, idioma restrito às opções configuradas e tema limitado aos três valores; o tema é espelhado no cookie `sk_color_scheme` ([modo escuro](ui-design.md#modo-escuro)). Sem uploads nem dados de negócio. |
| Organization / `sk_tenancy.Organization` | `name`; `slug`; `is_active` | Slug único, inclusive entre excluídos. Muda somente via `skeleton/tenancy/slugs.py` (por administrador da plataforma ou da organização, com CAS, validado sob trava e pela restrição `unique`, e recusado quando há provedor de identidade vinculado à organização). Organizações novas recebem slug no formato de rótulo DNS (1 a 63 caracteres `a-z`, `0-9` e `-`, sem hífen nas pontas e sem `xn--`) fora da lista de reservados (`skeleton/common/hostnames.py`); organizações antigas fora dessa regra continuam acessíveis em `/o/<uuid>/`, sem endereço próprio. Ciclo de vida controlado pela plataforma; desativar não apaga dados de negócio. |
| TableDefinition / `sk_system.TableDefinition` | `database_alias`, `schema_name`, `db_table`, `model_label`; `label` e `description`; `origin`, `scope`, `base_class`; `uuid_managed`, `soft_delete`, `audit_mode`, `api_exposed`, `available` | Chave `(database_alias, schema_name, db_table)` única. A sincronização atualiza informações físicas e preserva textos humanos; usuários não podem excluir a entrada. |
| FieldDefinition / `sk_system.FieldDefinition` | `table`; `name` (Django) e `column` (física); `label` e `description`; `type_name`; `nullable` e `primary_key`; `relation_target`; `sensitive`; `available` | `UNIQUE(table, column)`. A sincronização atualiza informações físicas e preserva textos humanos. |
| ComponentInstallation / `sk_system.ComponentInstallation` | `component_id`, `kind`, `app_label` e `package_name`, versão, `digest` e `publisher`, geração, estado e referência ao inventário | Global (`SystemRecord`), com identidade imutável e única. Os estados distinguem instalado (`installed`), local do projeto (`project-local`) e schema conservado (`schema_retained`). Uso exclusivo da plataforma; fora do gateway. |
| DeploymentOperation / `sk_system.DeploymentOperation` | Operação, estado e etapa, geração esperada, `digest` e plano aprovado, resultado e `error_code` | Global (`SystemRecord`); operação, geração e plano são imutáveis; a aprovação usa CAS. Os JSONs de plano e resultado são ocultados integralmente na auditoria; tickets efêmeros não são persistidos. |

## Entidades organizacionais

Todos os modelos abaixo herdam `OrganizationRecord`, e suas relações próprias utilizam `PROTECT`.

Um **papel** (`Role`) reúne permissões, enquanto um **grupo** (`AccessGroup`) reúne membros e pode receber papéis. As tabelas `MemberRole`, `GroupMember` e `GroupRole` registram explicitamente essas associações. `TablePermission` autoriza uma ação sobre uma tabela; `RecordGrant` concede uma ação sobre um registro específico, sem dispensar a permissão de tabela. Assim, Alice pode ser administradora em Alfa e apenas integrante de um grupo de leitura em Beta: os papéis de Alfa nunca cruzam essa fronteira.

| Modelo | Campos próprios | Unicidade e regras |
|---|---|---|
| OrganizationSettings / `sk_tenancy.OrganizationSettings` | `display_name`; `default_language`; `default_timezone`; `brand_color` (`#RRGGBB`); `logo_key` (sensível); `icon_key` (sensível, PNG 512 × 512); `business_hours` e `holidays` (calendário de expediente); `site_enabled` (endereço próprio) | Uma linha por organização (`UNIQUE`), criada na primeira gravação (antes disso, a revisão virtual é `0`). Somente o administrador grava, via `OrganizationService.update_settings` ou `/o/<org>/settings/`, com CAS. Valores em branco herdam o padrão da instância. `logo_key` só muda pelo upload do logo e é redigido na auditoria. Fora do gateway ([#167](https://github.com/alaorneto/skeleton/issues/167)). O **calendário de expediente** ([#416](https://github.com/alaorneto/skeleton/issues/416)) é editado em `/o/<org>/settings/` como texto — uma linha por dia (`seg 09:00-12:00 13:00-18:00`, aceitando também `mon`…`sun`) e uma data de feriado por linha — e armazenado em JSON: `business_hours` (dia da semana `"0"` segunda a `"6"` domingo → intervalos) e `holidays` (datas ISO). Aplica-se no fuso da organização (ou no da instância, se omitido). `skeleton.tenancy.business_hours.add_business_hours` soma horas úteis pulando dias fechados e feriados; sem calendário configurado, recorre-se a tempo corrido. |
| OrganizationSite / `sk_tenancy.OrganizationSite` | `site_organization` (FK única e imutável); `enabled`; `display_name`; `brand_color`; `logo_key` (sensível); `default_language`; `icon_key` (sensível) | `SystemRecord` global lido pelo host antes do login ([#480](https://github.com/alaorneto/skeleton/issues/480)). Escrito apenas por `skeleton.tenancy.site.sync_site`, na mesma transação e sob o lock de `update_settings`, `replace_logo` e `remove_logo`. Criado na primeira ativação de `site_enabled` e nunca excluído: desligar grava `enabled=False` e esvazia os dados de marca. Guarda apenas o que o administrador optou por publicar. Usa FK `site_organization`, nunca `organization` (E017). Fora do gateway. |
| Membership | `user`; `is_active` | `UNIQUE(org, user)` incondicional; `user` e `organization` imutáveis; um novo convite reativa a mesma linha em vez de duplicá-la. |
| Role | `code`, `name`, `description`, `is_builtin` | Código único entre registros vivos por organização; papéis internos (`organization_admin`, `people_manager`, `app_manager`, `member`, declarados em `BUILTIN_ROLES`) não podem ser removidos nem renomeados e são os únicos que carregam capacidades. |
| MemberRole | `membership`, `role` | Par único entre vivos na mesma organização; revogação por exclusão lógica. |
| AccessGroup | `code`, `name`, `description` | Código único entre vivos por organização; grupo de negócio, distinto de `auth.Group`. |
| GroupMember | `group`, `membership` | Par único entre vivos; exige `Membership` ativa. |
| GroupRole | `group`, `role` | Par único entre vivos; atribui um papel ao grupo. |
| TablePermission | `role`, `table`, `action` | Tripla única entre vivos; ações `read`, `create`, `update`, `delete`, `restore`, `manage_access` e `audit`; ausência de permissão nega acesso. |
| RecordGrant | `table`, `record_sysid`, `membership`/`group`/`role`, `action` | Exatamente um destinatário; ações `read`, `update` ou `delete`; combinação de registro, destinatário e ação única entre vivos; exige alvo vivo, autorizado e da mesma organização. |
| OrganizationAdmissionPolicy / `sk_tenancy.OrganizationAdmissionPolicy` | `discoverable`; `join_requests_enabled`; `request_default_role` (FK `Role`); `join_links_enabled`; `signup_via_link_enabled`; `domain_join_enabled` | Uma linha por organização (`UNIQUE`); sem linha, tudo permanece desligado com revisão virtual `0`. Somente `admission.configure` grava, via `configure_policy`, com CAS; o papel deve ser da mesma organização e nunca `organization_admin`. Fora do gateway ([#26](https://github.com/alaorneto/skeleton/issues/26)). |
| OrganizationDirectoryEntry / `sk_tenancy.OrganizationDirectoryEntry` | `listed_organization` (FK única); `discoverable`; `accepts_requests`; `accepts_domain` | `SystemRecord` global lido por `/join/` antes de haver tenant; escrito apenas pelo serviço da política, na mesma transação. Usa FK `listed_organization`, nunca `organization` (E017). |
| IdentityProvider / `sk_accounts.IdentityProvider` | `provider_id` (único); `protocol` (`oidc`, `saml` ou `ldap`); `bound_organization` (FK); `issuer`; `entity_id`; `directory` (URL LDAP com base); `client_id`; `sp_entity_id`; `fingerprint` (v2); `name`; `enabled`; `show_on_login`; `trust_verified_email`; `settings` (allowlist por protocolo); `secret` e `private_key` (cifrados, sensíveis); `last_check_at`; `last_check_code` | `SystemRecord` global cadastrado pela plataforma; campos de identidade e `fingerprint` são imutáveis (no guard e no serviço), com `CHECK` de identidade por protocolo. O `SocialApp` do allauth é montado em memória ([#29](https://github.com/alaorneto/skeleton/issues/29)). |
| IdentityProviderAdmission / `sk_tenancy.IdentityProviderAdmission` | `provider` (FK única e imutável); `jit_provisioning`; `jit_requires_approval`; `default_role` (FK anulável); `allowed_email_domains` (lista de até 50 domínios) | Regras da organização para seu provedor; sem linha, o JIT fica desligado. Lida pelo adapter em `sso:validated`. |
| VerifiedDomain / `sk_tenancy.VerifiedDomain` | `domain` (IDNA, imutável); `challenge` (público, imutável); `status` (`pending`, `verified` ou `failed`); `admission` (`auto` ou `approval`); `role`; `verified_at`; `last_checked_at`; `failure_count`; `last_error_code` | Único entre vivos por `(organização, domínio)`; estado `verified` exige `verified_at`. Somente `admission.configure` cadastra, verifica e remove. Fora do gateway ([#28](https://github.com/alaorneto/skeleton/issues/28)). |
| DomainClaim / `sk_tenancy.DomainClaim` | `domain` (único entre vivos, imutável); `claiming_organization` (FK imutável) | `SystemRecord` global consultado antes de haver tenant; escrito apenas pela verificação, na mesma transação do `VerifiedDomain`; revogado pela organização, pela reverificação ou pela plataforma. Usa FK `claiming_organization` (E017). |
| SiteDomain / `sk_tenancy.SiteDomain` | `domain` (imutável, IDNA); `challenge` (imutável, público); `status` (`pending`, `verified` ou `failed`); `verified_at`; `last_checked_at`; `failure_count`; `last_error_code` | Domínio próprio da organização para o endereço próprio ([#488](https://github.com/alaorneto/skeleton/issues/488)); limite de até 3 por organização, com apenas um registro vivo por domínio na organização. Somente o administrador cadastra, verifica e remove (exclusão lógica derruba a posse na mesma transação). Desafio e tabela separados de `VerifiedDomain`: provar domínio de email não autoriza host. Fora do gateway. |
| SiteDomainClaim / `sk_tenancy.SiteDomainClaim` | `domain` (único entre vivos, imutável); `claiming_organization` (FK imutável) | `SystemRecord` global lido na validação do host; escrito apenas pela verificação, na transação do `SiteDomain`, após a consulta DNS; revogado pela organização, pela reverificação ou pela plataforma. Usa FK `claiming_organization` (E017). |
| JoinLink / `sk_tenancy.JoinLink` | `label`; `token_digest` (sensível, único); `role`; `mode` (`direct` ou `approval`); `expires_at` (até 90 dias); `max_uses` (1 a 10.000); `use_count` | Armazena somente o SHA-256 do token; `CHECK` garante `0 <= use_count <= max_uses`; resumo, papel, modo, validade e limite são imutáveis; revogar é exclusão lógica. Criado e revogado por `invitations.manage` com a política ligada. Fora do gateway ([#27](https://github.com/alaorneto/skeleton/issues/27)). |
| JoinRequest / `sk_tenancy.JoinRequest` | `user` (imutável); `channel` (`directory`, `domain`, `link` ou `sso`, imutável); `join_link` (imutável, anulável); `role`; `status`; `message` (até 500 caracteres); `expires_at`; `decided_at`; `decided_by` | Apenas um pedido aberto por `(organização, pessoa)`; `CHECK` de coerência na decisão (decisão exige data; apenas decisão tem decisor; recusa exige decisor; aprovação sem decisor corresponde à aprovação automática de link direto após confirmação do email). Suporta autodescoberta por `user_id` (o solicitante lê seus pedidos em todas as organizações, mas escritas ocorrem só no tenant). Expira em 30 dias (estado derivado). Fora do gateway. |
| Invitation | `email`, `role`, `token_digest`, `expires_at`, `accepted_at`, `accepted_by` | Resumo SHA-256 único; token original enviado apenas na mensagem; validade de sete dias; `accepted_by` é UUID histórico; papel deve pertencer à mesma organização. |

`TablePermission.table` e `RecordGrant.table` referenciam o catálogo global. O campo `record_sysid` guarda o UUID do alvo, mas não é uma `GenericForeignKey`: o serviço resolve o modelo no registro autorizado em código e valida o acesso ao registro. Administradores precisam revogar dependências antes de excluir papéis ou grupos. Qualquer alteração capaz de remover o último administrador usa bloqueios e transação `atomic`, impedindo que duas mudanças simultâneas deixem a organização sem administrador.

A criação de uma organização também é atômica: `create_organization` confirma a organização, os papéis iniciais, o vínculo do administrador e as permissões em conjunto. Se o proprietário for desativado enquanto o formulário estava aberto ou se alguma validação do bootstrap falhar, toda a operação é desfeita. Assim, nunca permanece uma organização ativa sem administrador nem um slug ocupado por uma criação incompleta.

**Apps e limites da plataforma:**

- **`sk_system.AppInstallation`** (`OrganizationRecord`): armazena `app_label` imutável, `enabled` e `configuration` em JSON (ocultado integralmente na auditoria), com `UNIQUE` incondicional em `(organization, app_label)`. Controla apenas a ativação e a configuração na organização — não instala código nem altera o schema. Sem linha gravada, a app fica desativada na organização e usa os defaults dos campos. O `organization_admin` faz a gestão com CAS, bloqueio da organização e validação de dependências.
- **`sk_system.AppAvailability`** (`SystemRecord` global): registra, por app instalada, `app_label` único e imutável, `mode` (`all` ou `selected`), `limits` (o que a app adiciona aos limites de cada organização onde está em uso, como `{"members": 2}`, escrito apenas por `set_app_limits`) e `upgrade_state` (`""` em linhas antigas ainda não migradas, `pending` durante `upgrade_app_activation` e `done` após concluir; código novo sempre grava `done`). Sem linha, a app fica indisponível para todas as organizações. Os defaults dos modelos também são fechados: `AppAvailability.mode` nasce como `selected` e `AppInstallation.enabled` nasce como `false` (o código do núcleo sempre informa o valor explicitamente, e o default serve como proteção).
- **`sk_system.AppOrganizationGrant`** (`SystemRecord` global): liga uma disponibilidade a `granted_organization` (FK `PROTECT`), com `UNIQUE` incondicional; revogar usa exclusão lógica e conceder novamente restaura a mesma linha. Nem `AppAvailability` nem `AppOrganizationGrant` escrevem linhas de tenant ou entram no gateway.
- **`sk_system.OrganizationLimitOverride`** (`SystemRecord` global): registra ajustes da plataforma aos limites de uma organização, com `limited_organization` (FK `PROTECT`) e `metric` imutáveis e únicos em conjunto, `mode` (`bonus` soma ao limite; `fixed` substitui), `value` (vazio apenas no modo fixo, indicando ilimitado), `reason` obrigatório e `expires_at` opcional. Remover usa exclusão lógica, e definir novamente restaura a mesma linha ([limites da organização](organization-limits.md#ajuste-da-plataforma)).

`ComponentInstallation` e `DeploymentOperation`, os dois modelos de controle global de deployment, recebem infraestrutura congelada em `sk_system.0004_deployment`. O banco apenas espelha o inventário e o diário (`journal`) do agente; ele não determina qual código os processos web carregam. Desinstalar uma app globalmente preserva `AppInstallation` e sua configuração; sem código instalado, permite-se apenas desabilitar o estado existente. Reinstalar exige ativação explícita e preserva UUIDs e valores já persistidos.

A migration `sk_system.0005_deploymentoperation_lifecycle` adiciona `reinstall` e `core_update` ao contrato de operações sem alterar colunas nem a infraestrutura da migration anterior. Em caso de falha de migration, o modo de manutenção é mantido; uma recuperação offline reconciliada aparece como `restored` no histórico.

## Fluxos de processo

As seis tabelas de `sk_flows` (`sk_flows.0001_initial`) derivam de `OrganizationRecord`, com relações `PROTECT` no mesmo tenant e infraestrutura congelada na migration. Nenhuma entra no gateway genérico: apenas os serviços de `skeleton.flows` escrevem nelas, e a autorização deriva do registro de negócio e da participação. Veja os detalhes em [fluxos](flows.md#modelo-de-dados).

| Modelo | Campos próprios | Unicidade e regras |
|---|---|---|
| FlowDefinition | `key`, `version`, `app_label`, `label`, `digest`, `specification` (JSON) | `UNIQUE(org, key, version)`; todos os campos são imutáveis — uma versão publicada nunca muda |
| FlowInstance | `definition`, `flow_key`, `flow_version`, `number`, `subject_table`, `subject_sysid`, `status`, `outcome`, `starter`, `data`, `sensitive_data`, `idempotency_key`, `run_count`, `activity_count`, `finished_at` | Uma única instância ativa (`running` ou `suspended`) por fluxo e registro de negócio; chave de idempotência única por fluxo; `sensitive_data` redigido na auditoria; `subject_table` + `subject_sysid` não é `GenericForeignKey` (o serviço resolve o modelo no registro em código) |
| FlowStepRun | `instance`, `step_key`, `kind`, `status`, `sequence`, `scope`, `branch`, `via`, `outcome`, `state`, `due_at`, `finished_at` | `UNIQUE(instance, sequence)`; `scope` aponta para a ativação da bifurcação que originou o ramo |
| FlowTask | `instance`, `step_run`, `step_key`, `kind`, `status`, `slot`, `assignee`, `candidate_group`, `candidate_role`, `delegated_from`, `decision`, `action`, `comment`, `due_at`, `reminded_count`, `escalated_at`, `completed_by`, `completed_at` | No máximo uma fila (grupo ou papel); a fila é limpa ao fechar a tarefa, de modo que apenas tarefas abertas impeçam remover um grupo ou papel; `comment` redigido na auditoria |
| FlowSubmission | `instance`, `step_run`, `task`, `step_key`, `transition_key`, `origin`, `values`, `sensitive_values` | Todos os campos são imutáveis; `sensitive_values` redigido na auditoria |
| FlowActivity | `instance`, `sequence`, `kind`, `step_key`, `step_run`, `task`, `detail`, `comment` | `UNIQUE(instance, sequence)`; todos os campos são imutáveis; `comment` redigido na auditoria |

Essas tabelas armazenam dados de negócio, não estado de pipeline: por isso possuem revisão, exclusão lógica e auditoria por linha. Aspectos operacionais — como timers, retentativas e entregas — utilizam a fila de jobs.

## Registros selados (#526)

Um documento assinado, um lançamento contábil ou um registro de atendimento nasce como rascunho editável e torna-se imutável após um ato de negócio. O atributo `immutable_fields` não atende a esse cenário, pois congela campos desde a criação. Para isso, um modelo **selável** herda `SealableMixin` (`skeleton/common/models.py`) em conjunto com a base de registro — por exemplo, `class Document(SealableMixin, SecuredRecord)` — recebendo as colunas `sealed_at`, `sealed_by` e `seal_digest` (SHA-256 em hexadecimal) e as seguintes declarações:

| Declaração | Efeito |
|---|---|
| `seal_fields` | Colunas congeladas pelo selo (nome do campo ou da coluna; obrigatório; nunca inclui colunas de sistema, de acesso ou do próprio selo) |
| `seal_allows_delete` | `False` (padrão) recusa exclusão lógica e restauração após o selo; `True` permite |
| `seal_condition_sql` | Opcional: expressão booleana sobre `NEW`, validada pelo banco na transição (por exemplo, verificar se as partidas fecham); lê apenas tabelas da própria app |

| Depois de selado | Regra |
|---|---|
| `sealed_at`, `sealed_by`, `seal_digest` | Nunca mudam: não existe operação para desselar |
| Colunas de `seal_fields` | Nunca mudam |
| `deleted_at`, `deleted_by` | Não mudam, salvo quando `seal_allows_delete = True` |
| Demais colunas (situação, dono, visibilidade, grupo) | Editáveis pelas regras normais, com revisão e auditoria |

**Coluna imutável e selada.** Uma coluna pode aparecer simultaneamente em `immutable_fields` e em `seal_fields` — por exemplo, uma FK fixada na criação (como o registro corrigido por um adendo ou o pai de acesso) que também precisa integrar o resumo do selo. A imutabilidade prevalece antes e depois do selo: o modelo levanta `ImmutableRecord` e o guard do banco recusa com `immutable record identity`. O resumo do selo continua cobrindo a coluna, validada por `verify_seal`. Colunas `NOT NULL` não exigem tratamento especial: como o guard roda em um trigger `BEFORE`, a tentativa de alteração esbarra no selo antes da constraint.

A transição para selado (quando `sealed_at` passa de nulo para preenchido, em `UPDATE` ou já no `INSERT`) exige as três colunas preenchidas em conjunto: `sealed_by` não nulo e `seal_digest` com 64 caracteres hexadecimais minúsculos. Na mesma instrução, nenhuma coluna de `seal_fields` pode ser alterada — sela-se exatamente o conteúdo já gravado e lido. Definir `sealed_by` ou `seal_digest` sem `sealed_at` é recusado.

**Duas camadas de proteção.** No banco de dados, o guard (`<tabela>_sk_guard_fn`, gerado a partir da especificação congelada na migration sob a chave `seal`: `fields` com nomes de coluna, `allow_delete` e `condition_sql`) impõe a barreira definitiva. Ele recusa violações com SQLSTATE `23514` e as mensagens `sealed record`, `invalid seal` ou `seal condition failed`, seja a alteração originada no ORM, em `queryset.update`, em SQL direto ou em outra app — inclusive para o owner (embora DBA e owner continuem sendo fronteira privilegiada, pois o selo não é WORM).

Na camada Python, `save()`, `delete()` e `restore()` levantam `RecordSealed` antes de consultar o banco, retornando mensagem clara. `apply_seal(actor_id=..., digest=..., at=...)` preenche as três colunas apenas em memória, sem autorizar nada, cabendo ao chamador salvar. A expressão `seal_condition_sql` é literal da migration e é recusada na construção da operação se contiver `;`, comentários SQL ou `$`. Tabelas não seláveis permanecem inalteradas, sem chave nova na especificação e com o corpo usual do guard (`tests/unit/test_record_infrastructure_sql.py`). O check `E040` valida a declaração frente à especificação congelada, e `record_infrastructure` gera uma nova migration sempre que o selo muda. Na auditoria, o ato de selar aparece como um `update` comum com `seal_digest` legível.

**Filhos de um registro selado.** Uma tabela filha — como as linhas ou seções de um documento — não herda `SealableMixin`: ela declara `seal_parent = "document"`, apontando para uma FK `PROTECT` do modelo selável da mesma app, e inclui essa FK em `immutable_fields` (ambas exigidas por `E040`). Enquanto o pai estiver selado, o filho não pode ser inserido, alterado, excluído nem restaurado: `save()`, `delete()` e `restore()` levantam `RecordSealed`, e o guard do filho (chave `seal_parent` da especificação, com coluna e tabela do pai) recusa com `sealed parent`, validando tanto o pai novo quanto o antigo. Mover um filho para outro pai é bloqueado pela imutabilidade da FK.

Para evitar condições de corrida, o guard do filho lê o pai com `FOR SHARE`. Como `seal_record` trava o pai com `select_for_update` antes de ler os dados, um filho gravado concorrentemente ou aguarda o selo e é recusado, ou grava antes e integra o conjunto lido por quem sela (`tests/integration/test_sealed_children.py` comprova ambas as ordens com duas conexões). Apps que selam modelos sem usar `seal_record` devem travar o pai da mesma maneira. O conteúdo dos filhos entra no resumo por meio do parâmetro `extra` informado pela app. Pai e filho pertencem à mesma organização: a RLS oculta pais de outra organização e a FK composta bloqueia ligações cruzadas.

**Selar por serviço.** Todo modelo selável deve declarar `TableSpec.seal = SealPolicy(attachments="frozen", comments="open")`, opção obrigatória para modelos seláveis e recusada para os demais (`E040`). A função `skeleton.records.sealing.seal_record(record, user=..., expected_revision=..., extra=None)` exige permissão `update` no registro (em tabela exposta ou [derivada](access-control.md#tabelas-derivadas) pela âncora; em outras bases, a própria app autoriza antes), trava a linha, confere a revisão, recusa registros já selados ou excluídos, calcula o resumo e grava o selo.

O resumo é o SHA-256 produzido por `skeleton.common.sealing.canonical_bytes` — JSON em UTF-8 com chaves ordenadas e separadores compactos, `datetime` com fuso em ISO 8601 UTC, `date` em ISO, `UUID` e `Decimal` como texto, recusando `float` — sobre `{"table", "sysid", "organization", "fields", "attachments", "extra"}`. `verify_seal` recalcula e compara o resumo. Outras apps selam e verificam via `RecordSealService` ([API de serviços](service-api.md#recordsealservice)), que também suporta tabelas derivadas seláveis: selar exige `update` e verificar exige `read`, ambos resolvidos pela âncora.

Nas superfícies genéricas, `sealed_at` e `sealed_by` aparecem nos metadados de sistema (no JSON e no OpenAPI, apenas em tabelas seláveis) e nunca em `write_fields`. Tentar alterar coluna congelada, excluir ou restaurar um registro selado retorna `409 record_sealed` na API e erro no formulário das páginas; alterar colunas livres continua permitido. Na importação CSV, alterar um registro selado gera o erro de linha `record_sealed`, que impede a aplicação do arquivo inteiro, já que a importação é tudo ou nada ([transferência](data-transfer.md)). Um registro selado nunca é corrigido por edição, apenas por adendo ([extending](extending.md#registros-que-não-mudam-depois-de-um-ato)).

## Tabelas operacionais (ADR)

**Contexto.** A fila de jobs, o outbox de eventos, as entregas e o estado de leitura de notificações sofrem atualizações constantes a cada claim, heartbeat ou leitura. Se usassem `SystemRecord`, cada `UPDATE` geraria um `AuditEvent`, e como a role runtime não tem `DELETE`, essas tabelas cresceriam indefinidamente e inflariam a auditoria.

**Decisão.** `OperationalRecord` (`skeleton/common/models.py`) possui apenas `sysid` (UUID), `created_at` e `updated_at`, sem revisão, exclusão lógica nem auditoria. O manager filtra o tenant quando há FK `organization`, permite `update`, `delete` e `bulk_create`, e recusa `bulk_update` bem como alterações em campos imutáveis. A migration congela `lifecycle: operational` na especificação de `InstallRecordInfrastructure`, que instala guard de imutabilidade (`sysid`, `organization_id` e `immutable_fields`), RLS tenant quando há `organization` (com `self_discovery_column` opcional), FKs compostas de mesmo tenant, `GRANT SELECT, INSERT, UPDATE, DELETE` para a role runtime e nenhum trigger de auditoria. O catálogo registra `audit_mode=operational` e `base_class=OperationalRecord`. `check_skeleton_schema` valida triggers, grants, políticas e a ausência da função de auditoria, enquanto o check `E020` reprova tabela operacional exposta ou com `write_fields`.

**Consequências.** A exclusão física fica disponível à conexão runtime, mas é usada apenas pelo serviço de retenção declarado em código. A auditoria permanece como fonte de verdade do negócio: eventos de domínio e jobs carregam `record_revision` e `request_id` para correlação, sem substituir o `AuditEvent`. Desativar a auditoria de tabelas de negócio por flag foi descartado para manter o contrato explícito e verificável via classe base separada.

## AuditEvent

`AuditEvent` representa o registro histórico de uma alteração, não o objeto alterado em si. Ele preserva os identificadores mesmo depois que uma identidade é desativada ou que o objeto deixa de estar disponível. Leia [auditoria e concorrência](concepts.md#auditoria-e-concorrência) para entender revisão e transações, e [auditoria e exclusão lógica](audit-soft-delete.md) para os limites do mecanismo.

`sk_audit.AuditEvent` é estruturalmente global, mas mantém uma coluna `organization_id` (UUID, anulável) para aplicar RLS. Seus campos próprios são `table_name`, `model_label`, `record_sysid`, `record_revision`, `operation`, `actor_sysid`, `request_id`, `source`, `db_principal` e `changes` (JSON). Não possui FKs para usuário, organização, registro ou catálogo, garantindo que os eventos sobrevivam aos objetos históricos.

Como a tabela recebe uma linha a cada escrita do sistema, cada índice existente corresponde a uma consulta específica:

| Índice | Colunas | Consulta |
|---|---|---|
| `sk_audit_org_created` | `(organization_id, created_at)` | Log da organização e log da plataforma (`organization_id IS NULL`), do mais novo ao mais antigo |
| `sk_audit_record_created` | `(record_sysid, created_at)` | Histórico de um registro e filtro por registro no log |
| `sk_audit_org_actor` | `(organization_id, actor_sysid, created_at)` | Filtro por ator no log |
| `sk_audit_request` | `(request_id)` | Tudo o que uma requisição executou |

Novos índices só são adicionados junto com a consulta que os utiliza.

O campo `operation` assume `create`, `update`, `soft_delete`, `restore` ou `hard_delete`. Cada entrada em `changes` contém `old`, `new` e `redacted` — respectivamente valor anterior, valor novo e indicação de ocultação de segredo. A `revision` do evento é sempre `1`, e `deleted_at` e `deleted_by` são sempre `NULL`. `UPDATE` e `DELETE` são proibidos: eventos são apenas acrescentados. A aplicação não oferece exclusão física; um evento `hard_delete` só pode surgir após manutenção offline revisada que remova o guard do banco.

## Exemplo opt-in

`example_notes.Note` herda `SecuredRecord` e define `code` (`CharField(64)`), `title` (`CharField(200)`) e `body` (`TextField` opcional com limite de 10.000 caracteres), com unicidade de `(organization, code)` entre registros vivos. O campo `body` é tratado como sensível na auditoria, e os campos de acesso ficam fora da edição comum. Veja mais em [extending](extending.md).

## Nomes e exceções

Um label como `example_notes.note` identifica o modelo no ORM e no registro de especificações em código, mas não define o nome físico da tabela SQL — este vem de `_meta.db_table` e do catálogo. Nas APIs, UUIDs são serializados com hífens; no PostgreSQL, `UUIDField` usa o tipo nativo `uuid`.

Tabelas técnicas do Django e do allauth conservam suas chaves primárias, campos e ciclos de vida oficiais. Elas sustentam infraestrutura — como sessões e confirmações de email — e não entidades próprias de negócio. Por isso, não violam o contrato de UUID das entidades **próprias**; suas exceções nominais estão listadas em [catálogo](table-catalog.md#exceções-técnicas-nominais). Nunca adapte estruturas de terceiros com migrations improvisadas.
