# Controle de acesso e contexto organizacional

Uma organização é a fronteira de dados que o código chama de **tenant**. Autenticar responde “quem é a pessoa?”; autorizar responde “ela pode executar esta ação neste registro desta organização?”. Alice pode ter acesso em Alfa e Beta sem que uma permissão de Alfa valha em Beta. Veja [autorização e contexto](concepts.md#autorização-e-contexto) e [identidade e organizações](concepts.md#identidade-e-organizações).

## Papel, grupo, permissão e concessão

- **Papel (`Role`)**: conjunto de capacidades organizacionais, como leitor ou administrador. A associação direta é `MemberRole`.
- **Grupo (`AccessGroup`)**: reúne vínculos de pessoas (`GroupMember`), por exemplo a equipe Financeiro. Pode receber papéis por `GroupRole`; não é `auth.Group` do Django.
- **Permissão de tabela (`TablePermission`)**: permite uma ação, como `read` ou `update`, sobre um tipo de registro. Sem ela, a ação é negada, mesmo ao proprietário.
- **Concessão de registro (`RecordGrant`, ou grant)**: compartilha uma ação sobre uma linha com um vínculo, grupo ou papel. Não substitui a permissão de tabela, o vínculo ativo nem o isolamento organizacional. Uma app pergunta quais registros têm concessão ao ator por `AccessService.granted_record_ids`, nunca pelas funções internas de `skeleton.access.policies` (#343), e concede ou revoga por `AccessService.grant_record_access`/`revoke_record_access`, com a mesma autorização das telas (`access.manage`), nunca gravando `RecordGrant` direto (#539).
- **Visibilidade**: decide quem pode ler uma linha: proprietário (`private`), membros do grupo indicado (`group`) ou membros da organização (`organization`). Não é autorização para editar.

Esse controle por papéis é chamado RBAC. As regras de visibilidade e concessões acrescentam autorização por registro. O resultado depende de todas as camadas, não da simples existência de um papel ou UUID.

## Entradas públicas

```python
from skeleton.access.policies import authorized_queryset, require_record_action
from skeleton.tenancy.context import tenant_context, system_context

with tenant_context(organization_id=org_uuid, actor=user, source="job:export"):
    rows = authorized_queryset(Note, user=user, action="read")
    # Avalie/materialize antes de sair do contexto.
    result = list(rows.values("sysid", "title"))
```

`authorized_queryset(model, *, user, action='read', include_deleted=False)` filtra antes de paginação/contagem. `require_record_action(record, *, user, action)` revalida um registro. Nunca exponha resultados de `_base_manager`/`all_objects` ou relações reversas como alternativa.

`users_who_can_read(record, user_ids)` decide de uma vez quais pessoas leem um registro, com a mesma regra e na mesma ordem de `require_record_action` (vínculo ativo, papéis efetivos, `TablePermission`, administrador, dono, visibilidade e concessões) e um número fixo de consultas; um teste de equivalência a compara pessoa a pessoa (#296). Serve para decidir muitas pessoas para um registro (o fan-out de notificações); um endpoint continua usando `authorized_queryset`.

A lista de tabelas legíveis do painel, do catálogo e do seletor (`skeleton.records.services.readable_specs`) decide por tabela com uma consulta de `table_permissions` e a checagem de app ativa uma vez por app, em vez de uma autorização inteira por tabela; um teste de equivalência a compara com `authorized_queryset` (#302). Nada é guardado entre chamadas.

## Algoritmo

As verificações seguem esta ordem; falhar em uma camada não pode ser compensado por outra:

1. Organização ativa, usuário ativo/não excluído e `Membership` ativa/viva nessa organização.
2. Papéis efetivos: `MemberRole` vivos e `GroupRole` vivos dos grupos com `GroupMember` vivo. Cada decisão resolve o vínculo do ator uma vez e nada é guardado entre decisões: não há cache de autorização, nem entre requisições nem dentro da mesma transação (uma revogação vale na decisão seguinte).
3. `TablePermission` da ação obrigatória. Uma concessão de registro não a substitui. O padrão é negar (`default deny`); `member` não recebe permissões automaticamente.
4. Leitura: proprietário é o ator **ou** `visibility=organization` **ou** `visibility=group` e ator pertence ao `owning_group` **ou** existe `RecordGrant read` para seu vínculo/grupo/papel efetivo.
5. Atualização/exclusão: proprietário é o ator **ou** existe `RecordGrant` específico da ação. Compartilhar leitura com organização/grupo não compartilha escrita.
6. Restauração: permissão `restore` e condição de proprietário/administrador. Ler uma marca de exclusão (tombstone) também exige autorização de restauração: `authorized_queryset(..., include_deleted=True)` com outra ação e `require_record_action` sobre registro excluído com outra ação levantam `PermissionDenied` (#229).
7. O papel interno `organization_admin` tem concessões reconciliadas para tabelas elegíveis e exceção explícita à visibilidade **só na própria organização**. `is_superuser` global não pula vínculo/papel nas rotas organizacionais.
8. Administrador da plataforma é definido só por `is_platform_admin` (`skeleton/accounts/platform.py`): autenticado, ativo, não excluído, `is_staff` e `is_superuser`. O mesmo predicado liga `Context.platform_admin` e a GUC `skeleton.platform_admin`, abre a auditoria global (`authorized_audit_queryset`), responde `has_perm` e decide menus e selos nos templates (context processor `platform_admin` e filtros `platform_admin`). Conta só superuser ou só staff não é administrador. Os serviços globais da plataforma (disponibilidade de apps, operações de release, telas de plataforma de apps) usam um guard só, `require_platform_context(user, capability=None)`: o predicado (ou, com `capability`, um papel limitado que a tenha), MFA quando a política pede, `user` igual ao ator do contexto e contexto global, sem organização (#285). `update_platform_user` e `deactivate_user` bloqueiam os administradores ativos em ordem de `sysid` (`FOR UPDATE`) antes da identidade alvo e recusam com `LastPlatformAdministrator` (409, sem escrita parcial) a mudança que deixaria a plataforma sem administrador ativo; dois rebaixamentos concorrentes serializam e um deles recebe 409.

### Exemplo com Alfa e Beta

Alice e Bob têm vínculo ativo e `table.read` em Alfa. Alice cria uma nota privada: ela consegue ler; Bob não. Ao mudar a visibilidade para `organization`, Bob passa a ler, mas não a editar. Mesmo um `RecordGrant update` para Bob não permite editar sem `table.update`.

Em Beta, Alice pode ter outro papel e Bob pode nem ter vínculo. Nada do que foi concedido em Alfa abre a nota em Beta. Um grupo Financeiro em cada organização também são dois grupos distintos. Revogar `MemberRole`, `GroupMember` ou uma concessão retira seu efeito na próxima requisição.

Criar exige `table.create`; o proprietário inicial é o ator e a visibilidade é `private`. Alterar `owner`, `visibility`, `owning_group` ou a lista de concessões (ACL, lista de controle de acesso) exige `manage_access` **e** condição de proprietário ou administrador da organização. Esses campos nunca são aceitos pelo PATCH comum. O servidor valida proprietário ativo e grupo vivo da mesma organização.

## Tabelas derivadas

Uma app com um contêiner que a equipe edita (uma área de trabalho e seus itens, um documento e suas linhas) não precisa de autorização própria nas tabelas-filho. O filho é um `OrganizationRecord` não exposto que declara no `TableSpec` a chave estrangeira para o registro que decide o acesso (epic #560, #561):

```python
register(
    TableSpec(
        model=WorkItem,  # OrganizationRecord, não exposto
        description="Item de uma área de trabalho.",
        read_fields=("workspace", "title", "status"),
        write_fields=(),
        expose_api=False,
        access_parent="workspace",  # FK para a tabela que decide o acesso
    )
)
```

Uma tabela com `access_parent` é uma **tabela derivada**. A **âncora** é o `SecuredRecord` exposto no fim da cadeia de `access_parent`, a no máximo 3 chaves estrangeiras (um `WorkItemLine` com `access_parent="item"` chega à área pelo item). A app passa a usar `authorized_queryset` e `require_record_action` no filho como em qualquer tabela; a decisão é a da âncora, com a ação mapeada:

| Ação pedida no filho | Ação exigida na âncora |
|---|---|
| `read` | `read` |
| `create`, `update`, `delete`, `restore` | `update` |
| `manage_access` | sempre negada: o acesso é gerido na âncora |
| `audit` | `audit` na tabela âncora e `read` no registro âncora ([histórico](#auditoria-autorizada)) |

Regras (as estruturais são o check [`skeleton.E042`](checks.md)):

1. Só um `OrganizationRecord` que não é `SecuredRecord` nem `OperationalRecord`, com `expose_api=False` e escopo de organização, declara `access_parent`.
2. `access_parent` nomeia uma `ForeignKey` concreta do próprio modelo, `null=False`, `on_delete=PROTECT`, listada em `immutable_fields`: um filho nunca muda de pai.
3. O alvo é da mesma app e é a âncora (um `SecuredRecord` com `expose_api=True`) ou outra tabela derivada, sem ciclo e a no máximo 3 saltos.
4. `TablePermission`, escopo de token e concessões são os da **tabela âncora**, com a ação mapeada. A tabela derivada não recebe `TablePermission` nem `RecordGrant`: a gestão de acesso recusa a tabela.
5. A app da tabela derivada precisa estar ativa, como em qualquer tabela.
6. Âncora ou elo intermediário com tombstone torna o filho invisível, inclusive para `restore`: restaure o pai antes.
7. A autorização é só em Python: nada muda no banco, na RLS, nos triggers ou na especificação congelada da infraestrutura. O isolamento entre organizações continua sendo o da RLS e do contexto.

Criar um filho não tem registro para revalidar: `require_parent_action(WorkItem, workspace, user=..., action="create")` (de `skeleton.access.policies`) confere que `workspace` é o alvo do `access_parent` e que o ator pode alterá-lo (pela âncora, se o pai também é derivado). `users_who_can_read(filho, ids)` responde como a âncora viva, e vazio quando a âncora ou um elo tem tombstone. `skeleton.access.derivation.is_authorizable(model)` e `is_derived(model)` dizem se uma tabela é autorizável e se é derivada; todo serviço do núcleo que autoriza por tabela pergunta a eles.

Continuam fechados para tabela derivada: o gateway genérico de registros, a API REST genérica e o OpenAPI, o seletor de referências, as operações de registro, o catálogo e a concessão de `TablePermission`/`RecordGrant`, a exportação e a importação CSV, a interface declarativa e o sujeito de fluxo. Cada app mostra os filhos nas próprias páginas e serviços.

## Capacidades e papéis internos

Uma **capacidade** é um direito administrativo da organização, como gerir pessoas ou apps. Capacidades vêm só de **papéis internos** declarados em código (`skeleton/access/capabilities.py`, registry `BUILTIN_ROLES`). Um papel customizado nunca carrega capacidade, mesmo que reutilize um código interno: editar papéis não vira escalada de privilégio.

| Papel interno | Capacidades |
|---|---|
| `organization_admin` | todas: `members.view`, `members.manage`, `invitations.manage`, `admission.decide`, `admission.configure`, `apps.manage`, `access.manage`, `identity.configure`, `integrations.manage`, `flows.manage` |
| `people_manager` (gestor de pessoas) | `members.view`, `members.manage`, `invitations.manage`, `admission.decide` |
| `app_manager` (gestor de apps) | `apps.manage` |
| `process_manager` (gestor de processos) | `flows.manage`: administra os [fluxos de processo](flows.md#autorização) como o administrador, e nada fora deles ([#407](https://github.com/alaorneto/skeleton/issues/407)) |
| `member` | nenhuma |

`has_capability(user, cap)` e `require_capability(user, cap)` leem os papéis efetivos (diretos e via grupo) do tenant atual a cada chamada, sem cache: revogar `MemberRole` vale na requisição seguinte, e uma capacidade em Alfa não vale em Beta. `organization_admin` continua o único administrador pleno: só ele tem a exceção de visibilidade em `authorized_queryset`, a proteção de último administrador e a função SQL `skeleton.assert_user_deactivation`.

Cada recurso administrativo exige uma capacidade: vínculos pedem `members.view` para ler e `members.manage` para alterar; convites, `invitations.manage`; papéis, grupos, associações, permissões de tabela e concessões, `access.manage`. A configuração de apps continua restrita ao administrador. Quem tem outra capacidade recebe **404** num recurso fora da sua delegação, como se ele não existisse; quem não tem capacidade nenhuma mantém o 403 histórico. Qualquer capacidade exige MFA quando `SKELETON_REQUIRE_ADMIN_MFA` está ligado, também nas rotas de apps, cujo guarda aplica o mesmo portão antes da view (#322), e a [área de configuração da organização](ui-design.md#navegação) (`/o/<org>/admin/`, #621) mostra só as seções da capacidade do ator: a engrenagem do menu lateral aparece só para quem vê algum item dela, e o hub responde 404 a quem não vê nenhum. Mudar um link de lugar não muda autorização: cada tela continua revalidando no servidor.

**Regra contra escalada:** quem não tem `access.manage` só convida com papéis sem capacidade e não desativa nem remove quem tem papel com capacidade (403, sem escrita). O seletor de papéis do convite segue a mesma regra. `reconcile_builtin_roles` (também `manage.py reconcile_builtin_roles`) cria os papéis internos que faltam em cada organização ativa, numa transação por organização, é idempotente e reporta o código de um papel customizado que colide com um interno; renomeie esse papel e repita.

### Contas de serviço

A capacidade `integrations.manage` também governa os [webhooks](webhooks.md) da organização.

Uma **conta de serviço** é um membro sem pessoa, para integrações (webhooks, ERPs, scripts). É um `User` com `account_type=service`, para reutilizar owner, políticas e ator de auditoria, mas presa a uma organização (`service_organization`). `create_service_account(*, actor, name)` e `deactivate_service_account(*, actor, user)`, em `skeleton.accounts.services`, exigem `integrations.manage` sob `organization_lock`; só o administrador tem essa capacidade, e o gestor de pessoas não cria, desativa nem altera o vínculo de uma conta de serviço (403).

- A conta recebe email sintético `svc-<uuid>@service.invalid`, senha inutilizável e `local_password_allowed=False`. Um CHECK no banco impede `is_staff`, `is_superuser` ou senha local numa conta de serviço, e `account_type`/`service_organization` são imutáveis (trigger da infraestrutura do `User`).
- Tem exatamente um vínculo, o da organização que a criou: `Membership.clean` recusa outra organização (400).
- Nunca recebe papel com capacidade, nem direto (`MemberRole`) nem por grupo (`GroupMember` num grupo com papel com capacidade, ou `GroupRole` com capacidade num grupo que a contém): 400 `service_account_role`. Papéis sem capacidade, como `member`, são permitidos.
- Desativar revoga papéis, grupos e concessões do vínculo, desativa o vínculo e a identidade e incrementa `auth_version`. Os webhooks cujo principal é a conta são desativados na mesma transação (`principal_deactivated`, #313). Conta de outra organização ou uma pessoa responde como indisponível.
- Aparece marcada nas listas: `(conta de serviço)` no HTML e `account_type` na API de vínculos. Fica fora das listas e contagens da plataforma (`/platform/users/`, seletores de administrador), que responde 404 no detalhe.

## Administração de acesso

Os serviços em `skeleton.access.services` administram vínculos, papéis, grupos, associações, permissões de tabela e concessões de registro, cada um com a capacidade correspondente (seção anterior). As páginas e a REST escolhem o serviço de cada recurso pelo mesmo despacho, `skeleton.access.resources` (`create_resource`, `update_resource`, `delete_resource`); revogar convite é o serviço `skeleton.tenancy.invitations.revoke_invitation`, que confere a capacidade e a organização do convite (#265). `TablePermission` só aponta a tabela de negócio exposta e ação suportada, nunca identidade global ou tabela técnica. `RecordGrant` resolve um alvo `SecuredRecord` vivo, da mesma organização, com destinatário ativo; o UUID enviado pelo cliente não basta.

Mudanças que podem remover o último administrador bloqueiam `Organization` dentro de `atomic`: desativação/remoção de `Membership`, `MemberRole`, `GroupRole` e relações de grupo. Assim, operações concorrentes não aprovam duas remoções com base no mesmo estado antigo. Conflito deixa tudo inalterado. Remover `Membership` revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo antes da marca de exclusão. Papéis/grupos com referências vivas exigem revogação explícita prévia; papéis internos são protegidos.

**Saída voluntária.** Qualquer pessoa sai de uma organização quando quiser, por `skeleton.access.services.leave_organization(user, *, expected_revision=None)`, em `/o/<org>/leave/` (GET confirma sem alterar; POST com CSRF e a revisão do vínculo) ou em `POST /api/v1/o/<org>/membership/leave/` (`If-Match` da revisão do vínculo). Sair não exige capacidade: o serviço toma `organization_lock()` sem ator, bloqueia só o vínculo do próprio ator (Organization → vínculo, como as demais mudanças administrativas), revoga `MemberRole`, `GroupMember` e `RecordGrant`, desativa e exclui logicamente o vínculo e termina com `ensure_last_admin()`. O último administrador recebe 409 e nada muda; designe outro antes. O ator precisa ser o do contexto; o acesso cai na requisição seguinte (`/o/<org>/` responde 404). Voltar exige convite, aprovação ou designação: a confirmação de email do JIT de SSO não readmite quem saiu, e os fluxos de domínio e de link (#26, #28) também não podem readmitir automaticamente.

POST de `Membership` existente usa a mesma transição de desativação que PATCH: revoga `MemberRole`/`GroupMember`/`RecordGrant` e conserva a proteção do último administrador. Reativar não ressuscita essas associações. Restore persiste `is_active` escolhido pelo serviço no único CAS de restauração. Um vínculo inativo, mas não excluído, pode ser removido pelo ciclo de vida normal; organização, ACL e último administrador continuam validados.

Desativar ou excluir logicamente o `User` global verifica **todas** as organizações onde ele é membro ativo, não apenas a atual. No PostgreSQL, `skeleton.assert_user_deactivation(uuid)` é uma função `SECURITY DEFINER` restrita: pertence ao migrator (credencial de migrations), executa com privilégios do proprietário e só verifica administradores/mantém bloqueios nas organizações. Não escreve negócio nem concede contexto organizacional geral.

O privilégio público `EXECUTE` é revogado. A conexão runtime exige o plano de plataforma autenticado; operações offline usam owner. `system_context(reason=...)` numa conexão runtime comum não transforma a credencial em owner. Violação de integridade vira `LastAdministrator`; a tela `/platform/users/<id>/` responde 409 com rollback, não erro SQL/500.

### Designação de administrador pela plataforma

Ao criar uma organização em `/platform/organizations/new/`, o administrador inicial é informado pelo email de uma identidade humana ativa; o formulário não lista as identidades da instalação (#311). Email desconhecido, de identidade inativa ou de conta de serviço é recusado no campo.

Quando o administrador de uma organização sai ou fica indisponível, o administrador de plataforma designa outro em `/platform/organizations/<id>/` (card "Administradores"). O serviço é `skeleton.access.administration.designate_organization_admin(*, actor, organization, user, replace=None, expected_version=None)`:

- roda em `system_context(reason="organization:designate-admin", organization_id=...)`, só dessa organização, e toma `organization_lock()` antes de qualquer vínculo (mesma ordem das demais mudanças administrativas);
- cria ou reativa o vínculo com `organization_admin` por `provision_membership`. Com `replace`, retira só o papel `organization_admin` **direto** da pessoa substituída (o vínculo dela continua; papel via grupo não é tocado);
- termina com `ensure_last_admin()`: o que deixaria a organização sem administrador ativo responde 409 sem escrita parcial;
- compara `expected_version`, a versão do conjunto de administradores que o formulário mostrou, depois do lock; conjunto diferente responde 409 (`ConcurrentUpdate`);
- recusa designar o próprio administrador de plataforma (403) e substituir alguém por ele mesmo (400). Para entrar numa organização, o administrador de plataforma precisa de convite ou do `bootstrap_identity` no CLI.

A auditoria vem dos triggers, na mesma transação: os eventos de `Membership` e `MemberRole` têm `source` `system:organization:designate-admin` e o administrador de plataforma como ator. O formulário exige autenticação recente (sem ela, redireciona para reautenticar), CSRF e POST.

**Decisão: a leitura restrita da plataforma.** *Contexto:* para designar, a plataforma precisa saber quem administra a organização, mas administração de plataforma não concede acesso tenant. *Decisão:* a única leitura tenant da plataforma é `organization_overview(*, actor, organization)`, em `system_context(reason="platform:organization-inspect")`, que devolve um DTO imutável com o número de vínculos ativos e os emails dos administradores ativos (marcando quem é administrador só via grupo), mais a versão desse conjunto. Um wrapper de execução recusa qualquer instrução que não seja `SELECT` sem `FOR UPDATE`/`FOR SHARE` enquanto o contexto está aberto. *Consequências:* a plataforma não lista membros comuns, papéis, grupos, convites nem dados de negócio, e continua recebendo 404 em `/o/<org>/` sem vínculo próprio. Uma tela que precise de mais que isso exige outra decisão, não uma razão nova reaproveitada.

Os serviços de ciclo de vida da plataforma (`update_platform_user`, `update_organization`, `deactivate_user` e `restore_user`) recebem `actor` obrigatório e recusam quem não é administrador de plataforma (`PermissionDenied`) ou não é o ator do contexto (`InvalidContext`), mesmo chamados fora de `/platform/`; `actor=None` só vale em `system_context` (#226). Criar, editar ou remover identidade e criar ou editar organização exigem autenticação recente no POST. A confirmação de remoção em `/platform/users/<id>/delete/` descreve somente a identidade, envia a revisão que mostrou (revisão antiga responde 409) e chama `deactivate_user(user, actor=..., delete=True, expected_revision=...)`: exclusão lógica, sem o collector de exclusão física do Django. Perfis, Memberships e identificadores de identidade externa (`subjects`) permanecem históricos; o ciclo de vida invalida login/sessões. Não habilite ações de exclusão física da identidade nem de seus dependentes.

## Papéis limitados de plataforma

O administrador da plataforma continua definido só pelas flags (`is_platform_admin`). Além dele, `sk_accounts.PlatformRoleAssignment` (#34) dá a uma identidade um papel limitado, com capacidades em código (`skeleton/accounts/platform.py`):

| Papel | Capacidades | Telas |
|---|---|---|
| `auditor` | `audit.read`, `inventory.read` | `/platform/audit/` e o catálogo de tabelas, só leitura |
| `operator` | `updates.operate` | `/platform/updates/` (planos, envio, aprovação e operações) |
| `billing` | `billing.operate` | `/platform/billing/`: produtos, preços, vouchers e contas de provedor ([cobrança](billing.md#operação-da-plataforma)); a conta de uma organização continua com o administrador |

`platform_page(capability=..., writes=...)` libera a tela ao portador da capacidade: só para leitura, a menos que a capacidade seja de escrita (`updates.operate`, `billing.operate`); o resto de `/platform/` continua exigindo o administrador. `require_release_operator` aceita o administrador ou o operador nos serviços de deployment. Conceder e revogar, na página da identidade, exigem o administrador, CSRF e autenticação recente, e incrementam o `auth_version` da pessoa; a capacidade é recalculada a cada requisição.

A leitura global da auditoria pelo auditor passa pela RLS: a política `skeleton_audit_select` aceita eventos sem organização com `skeleton.platform_admin='on'` **ou** `skeleton.platform_auditor='on'`. Este último só é ligado por `platform_audit_scope(user)`, aberto pela página de auditoria depois de conferir `audit.read`, numa requisição de sessão sem tenant; fora dele o auditor não vê eventos globais. Inserção continua só com o escritor de auditoria, e UPDATE/DELETE seguem impossíveis. A migration `sk_audit.0004_platform_auditor` reinstala a política, e `check_deployed_schema` recusa a versão antiga.

## Contrato de contextos

O contexto informa à execução **quem está agindo, em qual organização e em qual transação**. Ele não é uma permissão adicional. Use as entradas abaixo em vez de preencher variáveis de sessão SQL manualmente:

| Entrada | Uso e contrato |
|---|---|
| `request_context(*, actor, request_id, source, using='default')` | Abre a transação externa da requisição |
| `tenant_context(*, organization_id, actor, source, using='default')` | Valida vínculo e organização ativos |
| `system_context(*, reason, organization_id=None, using='default')` | Exige motivo; somente infraestrutura/CLI, nunca opção de body/header HTTP, salvo as razões fechadas abaixo |
| `external_context(*, organization_id, actor, source='http:portal', using='default')` | O portal de uma pessoa externa ([acesso externo](#acesso-externo-portal)): valida `ExternalAccess` em vez de vínculo e marca `external=True` |

Cada entrada grava os oito GUCs `skeleton.*` (`skeleton.external` é `on` só no portal, #531; `skeleton.system` é `on` só em contexto de sistema e é lido pelo guard das [tabelas globais de apps](table-catalog.md#tabelas-globais-de-apps), #576) com uma instrução (`set_config(..., true)`, local à transação). Ao sair de um contexto aninhado com exceção, o rollback do savepoint devolve os GUCs anteriores e a mesma exceção sai do `with` (um erro de banco não vira outro nem fecha a conexão); na saída normal, os GUCs do contexto anterior são regravados. Um `system_context` de topo guarda e devolve o tenant escolhido, então dois blocos de sistema seguidos no mesmo processo podem usar organizações diferentes (#237).

Dentro de uma requisição, `system_context` só aceita as razões de `HTTP_SYSTEM_REASONS` (`skeleton/tenancy/context.py`), sempre escolhidas pelo código do servidor depois da própria validação, nunca por body ou header. `invitation:validated`, `sso:validated` e `external:validated` (`VALIDATED_IDENTITY_REASONS`) servem a convite, SSO e convite do portal já validados. `PLATFORM_ADMIN_REASONS` exigem que o ator da requisição seja administrador de plataforma: `organization:create`, `organization:bootstrap`, `builtin-roles`, `organization:designate-admin`, `platform:organization-inspect`, `platform:billing` (ações da plataforma na cobrança de uma organização, #405) e `platform:apps` (a plataforma atribui ou retira uma app de uma organização, #699) e `platform:setting-lock` (a plataforma trava uma configuração de uma organização, grava o valor ou um campo de app gerido por ela, #701/#702; também os jobs que aplicam a política das organizações novas, #703). `PLATFORM_CLIENT_REASONS` (`platform:api`, #711) só é admitida numa requisição com um token de plataforma que tem `platform.billing:read` ou `platform.billing_contacts:read`, para ler a cobrança de uma organização somente leitura ([API de gestão](platform-api.md#rotas)). `JOIN_REASONS` são abertas só pelos serviços de admissão, para uma organização: `join:request` (#26) exige um ator autenticado e age em nome do próprio solicitante; `join:link` (#27) também valida um link antes do login, com o resumo do token como prova; `join:domain` (#28) roda quando um email é confirmado, possivelmente antes do login, com o email confirmado como prova. A reverificação de domínios (`domain:recheck`), a sincronização de diretório (`directory:sync`) e a importação de provedores (`idp:import`) são só de CLI. Qualquer outra razão, ou uma dessas pedida por quem não é administrador de plataforma, levanta `InvalidContext`. Acrescentar uma razão exige mudar a constante e o teste que fixa a lista (`test_system_reasons_open_to_http_are_a_closed_list`).

Uma requisição global pode entrar em uma organização validada e voltar ao contexto global. Não pode trocar de Alfa para Beta na mesma transação. Blocos aninhados reutilizam identidade/organização usando savepoints (pontos de retorno dentro da transação); o ator só muda no hook de login/logout depois dos estágios allauth completos. Trabalho com várias organizações abre transações independentes por organização. O contexto é restaurado em `finally`; usa somente `SET LOCAL` e não sobrevive a COMMIT/ROLLBACK.

```python
for org_id in materialized_organization_ids:
    with system_context(reason="job:reconcile-permissions", organization_id=org_id):
        # Uma organização por transação, APIs internas apropriadas.
        reconcile_one(org_id)
```

O exemplo acima ilustra o contexto, não concede permissão HTTP nem `BYPASSRLS`. Não envolva todo o laço em uma `atomic` externa. `on_commit` recebe dados já materializados; consultas posteriores precisam de novo contexto. Um queryset Django é avaliado sob demanda: converta-o em dados dentro do bloco, não o devolva para consultar depois.

### Acesso externo (portal)

Uma pessoa de fora (um cliente que vê os próprios documentos, por exemplo) acessa a organização **sem ser membro** (#531, epic #515). A identidade é um `User` comum; "externo" é propriedade do vínculo `ExternalAccess` (organização, pessoa, app que a admitiu, ativo), separado de `Membership`. Listas e seletores de membros, papéis, assentos (`members`) e o último administrador nunca o veem. A organização liga o portal em `ExternalAccessPolicy` (desligado sem linha). O que a pessoa vê é decidido pela app que a admitiu, por serviços marcados para uso externo: o núcleo só sabe que ela tem acesso.

`external_context` valida um `ExternalAccess` vivo e ativo de pessoa e organização ativas, e o portal ligado. Qualquer falta responde `PermissionDenied("Organization is unavailable.")`, a mesma mensagem do vínculo ausente. Ele não aninha com `tenant_context` nem o contrário (`InvalidContext`), não promove a `system_context`, e um token Bearer nunca o abre. Por isso a pessoa que é membro **e** externa da mesma organização não usa, no portal, nenhum poder de membro.

**Negações explícitas** em contexto externo, antes de qualquer consulta de vínculo:

| Onde | Regra |
|---|---|
| `skeleton/access/policies.py` | `active_membership`, `authorized_queryset`, `require_record_action` e `authorized_audit_queryset` levantam `PermissionDenied`; `has_table_permission` e `is_organization_admin` devolvem falso; `table_permissions`, `effective_roles` e `group_ids`, vazio |
| `skeleton/access/capabilities.py` | `capabilities_for` devolve vazio |
| `skeleton/records` | toda entrada do gateway (especificação exposta, tabelas legíveis, queryset, criação, edição, acesso, exclusão, restauração, seletor de referência; exportação e importação passam por elas) recusa |
| `service_method` | sem `allow_external=True`, o método responde `Forbidden(code="external_not_allowed")`; nenhum serviço do núcleo tem a marca nesta entrega |

A capacidade `external.manage` (do administrador) gere as pessoas externas, e a métrica de plano `external_people` conta os acessos ativos. A auditoria de uma escrita no portal registra a pessoa como ator e `source="http:portal"`. Convite, aceite e o serviço `ExternalAccessService` estão no [portal](portal.md) (#532); o aceite abre `system_context(reason="external:validated")`, que entra em `VALIDATED_IDENTITY_REASONS`. Nos testes, `skeleton.testing.external` traz `external_scenario` (organizações A e B com o portal ligado, uma pessoa externa e uma administradora que também é externa) e `acting_external`.

### Direitos da assinatura

Na plataforma, o papel limitado **Operador de cobrança** (`billing`, capacidade `billing.operate`) opera o catálogo da cobrança, mas não a conta de uma organização. As capacidades `billing.view` e `billing.manage` (só do administrador da organização) controlam a [área de cobrança](billing.md#superfícies). Com a [cobrança](billing.md#ganchos-de-direito) ligada, a situação de pagamento da organização é mais um limite, aplicado depois da validação do vínculo e antes de qualquer autorização de registro: uma app paga sem direito fica indisponível como uma app retirada pela plataforma, uma organização `restricted` fica só leitura para pessoas e uma `suspended` só deixa passar as rotas de cobrança para quem pode regularizá-la. Quem não é membro continua recebendo 404, sem saber o estado. O estado vem da política registrada no servidor, nunca de body ou header, e não concede nada: ele só fecha.

### Host de organização

Num endereço `<slug>.<domínio base>` ([#482](https://github.com/alaorneto/skeleton/issues/482)) o host **restringe, nunca concede**: vínculo, RLS e autorização são os do domínio principal, e quem não é membro continua sem acesso. O escopo de site vem só de `request.get_host()` já validado por `ALLOWED_HOSTS`, nunca de header, body ou query; `X-Forwarded-Host` só vale pela configuração de proxy confiável existente (`DJANGO_TRUST_PROXY`). No host de uma organização, qualquer caminho de outra organização (`/o/<outra>/…`, `/api/v1/o/<outra>/…`) responde 404, mesmo para quem é membro das duas, e nenhum `system_context` é aberto por causa do host.

Rotas globais alcançáveis no host de organização (lista de permissão em `skeleton/tenancy/site_middleware.py`; o resto é o mesmo 404 neutro):

| Rota | Decisão | Por quê |
|---|---|---|
| `/accounts/…` (login, logout, cadastro, senha, MFA, SSO) | permitida | identidade |
| `/profile/…`, `/me/…` | permitida | páginas da própria pessoa |
| `/api/v1/…` (exceto `/api/v1/o/<outra>/…`) | permitida | mesma API; só a organização do host |
| `/site/…`, `/favicon.ico`, `/sw.js`, `/health/…`, `STATIC_URL` | permitida | marca pública, push e sondas |
| `/brand/…` (tema e imagens da instância, #597) | permitida | a organização carrega o tema da instância por baixo da marca dela |
| `/invite/<uuid do site>/…`, `/join/<uuid do site>/…` | permitida | convite e entrada na própria organização |
| `/`, `/notifications/`, `/flows/tasks/` | viram a rota da organização | a rota da organização vence |
| `/platform/…`, `/admin/…`, `/join/` (diretório), `/join/domain/…`, `/join/requests/…`, `/billing/webhooks/…`, `/invite/<outra>/…` | recusada | plataforma ou várias organizações |

**O que é público antes do login (#623).** No host da organização, sem login, só aparecem a marca publicada (nome, cor, logo, ícone, idioma; índice `OrganizationSite`) e, quando o administrador a publicou, a página pública (espelho `OrganizationSitePage`), lida sem contexto de tenant e sem `system_context`. Qualquer outro caminho de organização leva ao login, e `/o/<uuid>/` no domínio principal nunca é público. O host decide só qual organização é a do endereço; ele não abre nenhum dado de membro nem de registro.

No host de uma organização, as listagens de vínculos são **estreitadas** para a organização do host (#483): `memberships_for_user` (seletor, contadores, caixa de entrada e tarefas globais, `OrganizationService.my_organizations`, `GET /api/v1/organizations/`) e `for_each_organization` (Minha agenda, feed do calendário, OpenAPI agregado). Estreitar é **apresentação**, nunca autorização: nenhuma decisão de acesso depende do escopo de site. Ciclo de vida, segurança e privacidade da conta passam `all_organizations=True` e veem todas as organizações em qualquer host: a exportação dos dados pessoais, as sugestões e o diretório de admissão; as consultas diretas de `Membership` de último administrador, revogação, `auth_version` e eliminação não usam essas funções e não mudam. Uma pessoa autenticada sem vínculo recebe, no host da organização, a página "Você não tem acesso" (status 404), igual para qualquer caminho e com só o nome público da organização; na API continua o JSON 404.

Depois do login, um `next` absoluto só é seguido quando aponta para o endereço de uma organização com endereço ativo, no mesmo esquema e porta da URL pública, e a requisição está num host que compartilha a sessão ([login no endereço da organização](authentication-sso.md#login-no-endereço-da-organização)); qualquer outro host abaixo do domínio base cai no destino padrão.

**Domínios próprios (#488).** Só o administrador da organização cadastra, verifica e remove um domínio próprio, sob o lock da organização. A posse (`SiteDomainClaim`) é global e única por domínio, porque é ela que autoriza um host externo a representar uma organização; é escrita só na transação de verificação, depois da consulta DNS (feita fora de lock), e um domínio já comprovado por outra organização falha com a mesma resposta neutra, sem dizer quem o tem. A plataforma lista e revoga posses em `/platform/domains/`. Domínio igual ao domínio base ou abaixo dele, host da instalação, IP, `localhost` e curingas são recusados no cadastro. Só um domínio com posse viva, de organização ativa, entra nos hosts permitidos (`SiteAllowedHosts`, #489), e a parte dinâmica nunca tem curinga nem padrão com ponto inicial; um host da plataforma listado explicitamente continua plataforma mesmo que alguém o cadastre.

Um projeto derivado libera uma rota global própria acrescentando o prefixo a `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES` (lista, padrão vazia), como `["/relatorios/"]`; só faça isso para rotas da própria pessoa, nunca para rotas que leiam várias organizações.

### Leitura agregada entre organizações

Uma requisição comum seleciona um tenant só. Para mostrar os itens de uma pessoa em todas as organizações dela (a visão "Minha agenda", o feed da agenda), o núcleo oferece `skeleton.tenancy.aggregate.for_each_organization(user, fn, *, source, app_label=None, limit=50)` (#74):

- A view é marcada com `@aggregate_route`, e o `TenantContextMiddleware` não abre contexto nem transação para ela. `for_each_organization` recusa rodar dentro de um contexto (`InvalidContext`).
- Os vínculos ativos da pessoa são listados numa transação curta, em ordem de nome, até `limit` (no máximo 50) organizações.
- Para cada organização, `fn(organization_id)` roda num `request_context` + `tenant_context` próprios: TablePermission, visibilidade, ativação de apps e RLS valem como numa requisição comum. No PostgreSQL a transação é `READ ONLY`, então uma escrita dentro de `fn` falha.
- Uma organização é pulada quando o vínculo ficou inativo, quando `app_label` não está ativa nela ou quando `fn` é negado: `PermissionDenied` (por exemplo, sem TablePermission) ou `Forbidden`/`Unavailable` de uma API de serviços.
- `fn` devolve valores materializados: queryset, iterador ou gerador levantam `LazyAggregateResult`, porque nada pode ler o banco depois da transação da organização.
- Com `organization_id=...`, só essa organização roda, e só enquanto a pessoa tiver vínculo ativo nela; sem vínculo, o resultado é vazio. O link de assinatura de um calendário usa esse filtro (#669).

Não há escrita entre organizações: uma escrita sempre acontece numa organização escolhida pela pessoa.

### Ordem dos bloqueios administrativos

Um bloqueio de linha (`SELECT ... FOR UPDATE`) impede outra transação de modificar aquela linha até o fim da transação atual. Para alterações administrativas, adquira primeiro o bloqueio de `Organization` com `skeleton.access.services.organization_lock(user, capability=...)` (sem `capability`, exige o administrador) e só depois carregue/bloqueie o vínculo, papel, grupo ou link a alterar. HTTP sempre passa o ator autenticado; a ausência de ator é reservada ao provisionamento interno.

O bloqueio da organização é `SELECT ... FOR NO KEY UPDATE` (#239): dois bloqueios da mesma organização continuam se excluindo, mas ele não conflita com o `FOR KEY SHARE` que as chaves estrangeiras pedem na linha da organização ao inserir ou confirmar registros dela; assim um commit de outra transação não espera a operação administrativa, e duas transações que já inseriram não entram em deadlock ao pedir o bloqueio. A API dedicada e os POSTs administrativos HTML seguem essa ordem. Os serviços reutilizam o mesmo bloqueio, e a revisão é comparada **depois** de adquirir os locks. Inverter a ordem — uma requisição segurando o vínculo e esperando a organização, outra segurando a organização e esperando o vínculo — causa deadlock. O banco abortar uma transação não é uma solução de concorrência; não há retry automático para esconder esse erro.


## RLS PostgreSQL

**Row-Level Security (RLS), ou segurança por linha**, é uma proteção do banco que restringe quais linhas a conexão pode ler ou escrever. Aqui ela verifica a organização; os serviços Django continuam responsáveis por papéis, visibilidade, ações e exclusão lógica.

O contexto instala parâmetros locais da sessão PostgreSQL, chamados **GUCs**: `skeleton.user_id`, `skeleton.tenant_id`, `skeleton.request_id`, `skeleton.source`, `skeleton.platform_admin`. O ator vem da sessão autenticada e o UUID da requisição é gerado pelo servidor. A organização vem da URL validada, não de header/body. Valores vazios não veem linhas; UUID inválido gera erro, não um valor alternativo permissivo.

Tabelas organizacionais usam `ENABLE` + `FORCE ROW LEVEL SECURITY` e policy da conexão runtime (processo da aplicação) `USING T / WITH CHECK T`, onde T significa “organização da linha igual ao tenant da GUC”. `USING` limita linhas existentes; `WITH CHECK` verifica a organização da linha escrita. RLS **não** filtra `deleted_at`, papéis ou visibilidade: uma nota privada de Alfa não se torna legível a Bob apenas porque ambos estão em Alfa.

**Autodescoberta deliberada:** uma tabela organizacional pode declarar, no spec congelado de `InstallRecordInfrastructure`, a chave `self_discovery_column` (a coluna do usuário dono da linha), e o modelo declara o mesmo campo em `self_discovery_field`. Para ela, SELECT permite `(T OR U)`, onde U significa “coluna igual ao usuário da GUC”; INSERT/UPDATE/DELETE só permitem T. As políticas se chamam `skeleton_self_select`, `skeleton_self_insert`, `skeleton_self_update` e `skeleton_self_delete`, e `check_skeleton_schema` reprova política faltante, extra ou alargada, e divergência entre o modelo e o spec. A leitura cruzada só acontece pelo helper explícito `own_rows(model, user)` de `skeleton.tenancy.context`, que filtra o usuário também em Python (defesa em profundidade sobre a RLS) e exige que ele seja o ator do contexto; o manager normal permanece restrito ao tenant. `Membership` usa a chave desde `sk_tenancy.0003_membership_self_discovery`, com a mesma política efetiva de antes: `memberships_for_user(user)` projeta IDs/status/nome das organizações correspondentes. Alice pode assim descobrir seus vínculos em Alfa e Beta antes de escolher uma organização, sem receber escrita entre organizações nem usar uma policy recursiva. `FlowTask` declara a chave pelo responsável (`assignee_id`, `sk_flows.0003_task_self_discovery`, [#409](https://github.com/alaorneto/skeleton/issues/409)): a caixa global de tarefas lê só as tarefas atribuídas à própria pessoa, sem cruzar com a instância (que a RLS de outro tenant esconde).

SELECT de AuditEvent pela conexão runtime limita-se ao tenant; eventos globais só quando `platform_admin=on`. INSERT é da role SQL do trigger; UPDATE/DELETE são proibidos. User/Profile/Organization/catálogo globais não têm RLS tenant e nunca são listas genéricas para membros. Uma role SQL é uma credencial/privilégio do banco; não é o modelo empresarial `Role`.

**Limite da proteção:** `set_config` é permitido a quem possui a credencial runtime. RLS protege contra filtro organizacional esquecido e fluxos não autorizados da aplicação; não contra credencial runtime maliciosa ou injeção SQL arbitrária que altere GUCs. DBA/superuser SQL/`BYPASSRLS` ignora FORCE. `is_superuser` Django não transforma a conexão em owner do banco. Proteja DSNs (endereços/credenciais de conexão), use SQL parametrizado e proteja as superfícies HTTP.

## Auditoria autorizada

Administradores organizacionais veem auditoria da própria organização. Outros precisam de `table.audit` e leitura autorizada do registro atual; uma marca de exclusão exige autorização para `restore`. Filtre **antes** de paginar/contar. Se o registro foi removido offline e não há objeto atual, só administrador organizacional/plataforma pode ver os eventos: nunca reconstrua a ACL a partir do conteúdo histórico. Inventário técnico e eventos globais pertencem somente à plataforma: ao administrador e, para leitura, ao auditor de plataforma (#34).

A origem de cada evento vem do GUC `skeleton.source` da transação e diz qual credencial escreveu: `http` para a sessão, `http:token:<prefixo>` para um [token de API](api.md#autenticação-por-token), `http:oidc:<client_id>` para o access token de um [cliente nativo](api.md#access-tokens-oidc-de-clientes-nativos) (#604) e `system:<motivo>` para um contexto de sistema, como `system:native-client:register`. Nenhuma delas leva o segredo do token.
