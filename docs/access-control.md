# Controle de acesso e contexto organizacional

Uma organização é a fronteira de isolamento de dados que o código chama de **tenant**. Autenticar responde "quem é a pessoa?"; autorizar responde "ela pode executar esta ação neste registro desta organização?". Alice pode ter acesso em Alfa e em Beta sem que uma permissão concedida em Alfa tenha efeito em Beta. Consulte [autorização e contexto](concepts.md#autorização-e-contexto) e [identidade e organizações](concepts.md#identidade-e-organizações) para a visão geral.

## Papel, grupo, permissão e concessão

O controle de acesso combina controle baseado em papéis (RBAC) com regras de visibilidade e concessões por registro. O acesso efetivo depende de todas as camadas em conjunto, nunca da simples existência de um papel ou de um UUID:

- **Papel (`Role`)**: conjunto de permissões organizacionais, como leitor ou administrador. A associação direta a uma pessoa é `MemberRole`.
- **Grupo (`AccessGroup`)**: reúne vínculos de pessoas (`GroupMember`), como a equipe Financeiro. Pode receber papéis via `GroupRole`; não tem relação com `auth.Group` do Django.
- **Permissão de tabela (`TablePermission`)**: autoriza uma ação — como `read` ou `update` — sobre um tipo de registro. Sem ela, a ação é negada inclusive ao proprietário.
- **Concessão de registro (`RecordGrant`, ou grant)**: compartilha uma ação sobre uma linha específica com um vínculo, grupo ou papel. Não dispensa a permissão de tabela, o vínculo ativo nem o isolamento organizacional. Apps consultam quais registros têm concessão para o ator via `AccessService.granted_record_ids`, nunca pelas funções internas de `skeleton.access.policies`, e concedem ou revogam via `AccessService.grant_record_access` e `revoke_record_access`, sob a mesma autorização das telas (`access.manage`), nunca gravando `RecordGrant` diretamente.
- **Visibilidade**: define quem pode ler uma linha — apenas o proprietário (`private`), os membros do grupo indicado (`group`) ou todos os membros da organização (`organization`). Visibilidade nunca concede permissão de edição.

## Entradas públicas

O código da aplicação deve sempre entrar pelos helpers públicos de contexto e autorização:

```python
from skeleton.access.policies import authorized_queryset, require_record_action
from skeleton.tenancy.context import tenant_context, system_context

with tenant_context(organization_id=org_uuid, actor=user, source="job:export"):
    rows = authorized_queryset(Note, user=user, action="read")
    # Avalie/materialize antes de sair do contexto.
    result = list(rows.values("sysid", "title"))
```

`authorized_queryset(model, *, user, action='read', include_deleted=False)` aplica o filtro de autorização antes de paginação ou contagem. `require_record_action(record, *, user, action)` revalida um registro específico. Nunca exponha resultados de `_base_manager`, `all_objects` ou relações reversas como alternativa.

Para decidir acesso de muitas pessoas a um único registro — como no fan-out de notificações —, use `users_who_can_read(record, user_ids)`. A função aplica as mesmas regras e a mesma ordem de `require_record_action` (vínculo ativo, papéis efetivos, `TablePermission`, administrador, proprietário, visibilidade e concessões) com um número fixo de consultas, validada por teste de equivalência pessoa a pessoa. Endpoints continuam usando `authorized_queryset`.

A listagem de tabelas legíveis no painel, no catálogo e no seletor (`skeleton.records.services.readable_specs`) avalia o acesso por tabela com uma única consulta de `table_permissions` e verifica a ativação de cada app uma vez por app, em vez de executar uma autorização completa por tabela. Sua equivalência com `authorized_queryset` é garantida por teste, e nada fica em cache entre chamadas.

## Algoritmo

As verificações ocorrem rigorosamente nesta ordem; falhar em uma camada nunca é compensado por outra:

1. Organização ativa, usuário ativo e não excluído, e `Membership` ativa e viva nessa organização.
2. Papéis efetivos: `MemberRole` vivos somados aos `GroupRole` vivos dos grupos em que há `GroupMember` vivo. Cada decisão resolve o vínculo do ator uma única vez e não guarda cache entre decisões — nem entre requisições nem na mesma transação —, de modo que qualquer revogação vale imediatamente na decisão seguinte.
3. `TablePermission` obrigatória para a ação. Uma concessão de registro não a substitui. O padrão é negar (`default deny`); o papel `member` não recebe permissões automaticamente.
4. Leitura: o ator é o proprietário, **ou** `visibility=organization`, **ou** `visibility=group` e o ator integra o `owning_group`, **ou** existe `RecordGrant read` para seu vínculo, grupo ou papel efetivo.
5. Atualização e exclusão: o ator é o proprietário **ou** existe `RecordGrant` específico para a ação. Compartilhar leitura com a organização ou com um grupo nunca concede escrita.
6. Restauração: exige permissão `restore` e condição de proprietário ou administrador. Ler uma marca de exclusão (tombstone) também exige autorização de restauração: chamar `authorized_queryset(..., include_deleted=True)` ou `require_record_action` sobre um registro excluído com outra ação levanta `PermissionDenied`.
7. O papel interno `organization_admin` possui concessões reconciliadas para tabelas elegíveis e exceção explícita à visibilidade **apenas dentro da própria organização**. A flag global `is_superuser` não dispensa vínculo nem papel nas rotas organizacionais.
8. Administrador da plataforma é definido exclusivamente por `is_platform_admin` (`skeleton/accounts/platform.py`): usuário autenticado, ativo, não excluído, com `is_staff` e `is_superuser` simultaneamente (apenas um dos dois não basta). Esse predicado liga `Context.platform_admin` e a GUC `skeleton.platform_admin`, libera a auditoria global (`authorized_audit_queryset`), responde `has_perm` e controla menus e selos nos templates (context processor `platform_admin` e filtros `platform_admin`).
   - Os serviços globais da plataforma (disponibilidade de apps, operações de release e telas de plataforma de apps) compartilham o guard único `require_platform_context(user, capability=None)`, que exige o predicado (ou um papel limitado portador da `capability`), MFA quando exigido pela política, `user` igual ao ator do contexto e contexto global sem organização.
   - `update_platform_user` e `deactivate_user` bloqueiam os administradores ativos em ordem de `sysid` (`FOR UPDATE`) antes da identidade alvo e recusam com `LastPlatformAdministrator` (409, sem escrita parcial) qualquer alteração que deixaria a plataforma sem administrador ativo; dois rebaixamentos concorrentes serializam e um deles recebe 409.

### Exemplo com Alfa e Beta

Suponha que Alice e Bob tenham vínculo ativo e `table.read` em Alfa. Alice cria uma nota privada: ela consegue lê-la, mas Bob não. Quando Alice muda a visibilidade para `organization`, Bob passa a ler a nota, mas ainda não pode editá-la. Mesmo um `RecordGrant update` para Bob não libera a edição se ele não tiver `table.update`.

Em Beta, Alice pode exercer outro papel e Bob pode nem ter vínculo. Nada concedido em Alfa abre a nota em Beta, e um grupo chamado Financeiro em cada organização são dois grupos distintos. Revogar `MemberRole`, `GroupMember` ou um `RecordGrant` surte efeito já na requisição seguinte.

Criar um registro exige `table.create`; o proprietário inicial é o ator e a visibilidade nasce `private`. Alterar `owner`, `visibility`, `owning_group` ou a lista de concessões (ACL) exige `manage_access` **e** ser proprietário ou administrador da organização. Esses campos nunca são aceitos pelo `PATCH` comum, e o servidor valida sempre que o proprietário está ativo e que o grupo está vivo na mesma organização.

## Tabelas derivadas

Quando uma app possui um contêiner editado pela equipe — como uma área de trabalho e seus itens, ou um documento e suas linhas —, as tabelas filhas não precisam de controle de acesso próprio. O filho é um `OrganizationRecord` não exposto que declara no `TableSpec` a chave estrangeira para o registro que decide o acesso:

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

Uma tabela que declara `access_parent` é uma **tabela derivada**. A **âncora** é o `SecuredRecord` exposto no fim da cadeia de `access_parent`, a no máximo 3 chaves estrangeiras de distância (por exemplo, `WorkItemLine` com `access_parent="item"` chega à área de trabalho passando pelo item). A app usa `authorized_queryset` e `require_record_action` no filho normalmente, e a decisão é delegada à âncora com a ação mapeada:

| Ação pedida no filho | Ação exigida na âncora |
|---|---|
| `read` | `read` |
| `create`, `update`, `delete`, `restore` | `update` |
| `manage_access` | sempre negada: o acesso é gerido na âncora |
| `audit` | `audit` na tabela âncora e `read` no registro âncora ([histórico](#auditoria-autorizada)) |

Regras estruturais (verificadas pelo check [`skeleton.E042`](checks.md)):

1. Apenas um `OrganizationRecord` que não seja `SecuredRecord` nem `OperationalRecord`, com `expose_api=False` e escopo de organização, pode declarar `access_parent`.
2. `access_parent` deve apontar para uma `ForeignKey` concreta do próprio modelo, com `null=False`, `on_delete=PROTECT` e listada em `immutable_fields`: um filho nunca troca de pai.
3. O destino pertence à mesma app e deve ser a âncora (um `SecuredRecord` com `expose_api=True`) ou outra tabela derivada, sem ciclos e com no máximo 3 saltos.
4. `TablePermission`, escopo de token e concessões são avaliados na **tabela âncora**, usando a ação mapeada. A tabela derivada não recebe `TablePermission` nem `RecordGrant`, sendo recusada pela gestão de acesso.
5. A app da tabela derivada precisa estar ativa na organização, como em qualquer outra tabela.
6. Se a âncora ou qualquer elo intermediário estiver excluído logicamente (tombstone), o filho fica invisível, inclusive para `restore`: restaure o pai primeiro.
7. A derivação ocorre exclusivamente em Python: nada muda no banco, na RLS, nos triggers ou na especificação congelada da infraestrutura. O isolamento entre organizações continua garantido pela RLS e pelo contexto.

Como criar um filho ainda não possui instância para revalidar, use `require_parent_action(WorkItem, workspace, user=..., action="create")` (de `skeleton.access.policies`), que confirma se `workspace` é o alvo de `access_parent` e se o ator pode alterá-lo (resolvendo até a âncora quando o pai também é derivado). `users_who_can_read(filho, ids)` espelha a âncora viva e devolve vazio se a âncora ou algum elo tiver tombstone. `skeleton.access.derivation.is_authorizable(model)` e `is_derived(model)` informam se uma tabela aceita autorização e se é derivada; todo serviço do núcleo que autoriza por tabela consulta essas funções.

Permanecem fechados para tabelas derivadas: o gateway genérico de registros, a API REST genérica e o OpenAPI, o seletor de referências, as operações de registro, o catálogo e a concessão de `TablePermission` e `RecordGrant`, a exportação e importação CSV, a interface declarativa e o sujeito de fluxo. Cabe à própria app expor os filhos em suas páginas e serviços.

## Capacidades e papéis internos

Uma **capacidade** é um direito administrativo da organização, como gerir pessoas ou apps. Capacidades provêm exclusivamente de **papéis internos** declarados em código (`skeleton/access/capabilities.py`, registro `BUILTIN_ROLES`). Um papel customizado nunca recebe capacidade, mesmo que reutilize o código de um papel interno — editar papéis jamais escala privilégios.

| Papel interno | Capacidades |
|---|---|
| `organization_admin` | todas: `members.view`, `members.manage`, `invitations.manage`, `admission.decide`, `admission.configure`, `apps.manage`, `access.manage`, `identity.configure`, `integrations.manage`, `flows.manage`, `notifications.manage` (os avisos da organização, [#785](https://github.com/alaorneto/skeleton/issues/785)) |
| `people_manager` (gestor de pessoas) | `members.view`, `members.manage`, `invitations.manage`, `admission.decide` |
| `app_manager` (gestor de apps) | `apps.manage` |
| `process_manager` (gestor de processos) | `flows.manage`: administra os [fluxos de processo](flows.md#autorização) como o administrador, e nada fora deles ([#407](https://github.com/alaorneto/skeleton/issues/407)) |
| `member` | nenhuma |

`has_capability(user, cap)` e `require_capability(user, cap)` consultam os papéis efetivos (diretos e via grupo) do tenant atual a cada chamada, sem cache: revogar `MemberRole` vale na requisição seguinte, e uma capacidade em Alfa não vale em Beta. `organization_admin` permanece como o único administrador pleno, sendo o único a ter exceção de visibilidade em `authorized_queryset`, proteção de último administrador e verificação na função SQL `skeleton.assert_user_deactivation`.

Cada recurso administrativo exige sua capacidade correspondente: vínculos exigem `members.view` para leitura e `members.manage` para alteração; convites exigem `invitations.manage`; papéis, grupos, associações, permissões de tabela e concessões exigem `access.manage`. A configuração de apps continua restrita ao administrador. Quem possui outra capacidade recebe **404** em recursos fora da sua delegação, como se não existissem; quem não possui capacidade alguma mantém o 403 histórico.

Qualquer capacidade exige MFA quando `SKELETON_REQUIRE_ADMIN_MFA` está ligado, inclusive nas rotas de apps, cujo guarda aplica a mesma barreira antes da view. A organização pode endurecer a exigência para seus administradores ou para todos os membros, mas nunca afrouxar a política da plataforma ([quem precisa cadastrar o MFA](authentication-sso.md#mfa-cifrado-e-obrigatório)). A [área de configuração da organização](ui-design.md#navegação) (`/o/<org>/admin/`) exibe apenas as seções correspondentes às capacidades do ator: o ícone de engrenagem no menu lateral aparece somente para quem enxerga algum item, e o hub responde 404 a quem não vê nenhum. Reorganizar links na interface não altera autorização — cada tela continua revalidando no servidor.

**Regra contra escalada:** quem não tem `access.manage` só pode convidar usando papéis sem capacidade e não pode desativar nem remover pessoas que possuam papel com capacidade (403, sem escrita); o seletor de papéis do convite aplica a mesma restrição. A rotina `reconcile_builtin_roles` (também disponível em `manage.py reconcile_builtin_roles`) cria os papéis internos ausentes em cada organização ativa, em uma transação por organização, de forma idempotente; se houver um papel customizado colidindo com um código interno, o comando reporta o conflito para que o papel seja renomeado antes de repetir a execução.

### Contas de serviço

Uma **conta de serviço** é um membro não humano destinado a integrações (webhooks, ERPs e scripts). Ela é modelada como um `User` com `account_type=service` — reutilizando proprietário, políticas e ator de auditoria —, mas fica vinculada permanentemente a uma única organização (`service_organization`). A capacidade `integrations.manage` governa tanto as contas de serviço quanto os [webhooks](webhooks.md) da organização. `create_service_account(*, actor, name)` e `deactivate_service_account(*, actor, user)`, em `skeleton.accounts.services`, exigem `integrations.manage` sob `organization_lock`; apenas o administrador possui essa capacidade, e o gestor de pessoas não cria, desativa nem altera o vínculo de uma conta de serviço (403).

- A conta recebe email sintético `svc-<uuid>@service.invalid`, senha inutilizável e `local_password_allowed=False`. Um `CHECK` no banco proíbe `is_staff`, `is_superuser` ou senha local em contas de serviço, e o trigger de infraestrutura de `User` torna `account_type` e `service_organization` imutáveis.
- Possui exatamente um vínculo, pertencente à organização que a criou: `Membership.clean` recusa qualquer outra organização (400).
- Nunca recebe papel com capacidade, seja diretamente (`MemberRole`) ou por grupo (`GroupMember` em grupo com papel privilegiado, ou `GroupRole` privilegiado em grupo que a contenha), retornando `400 service_account_role`. Papéis sem capacidade, como `member`, são permitidos.
- Desativar a conta revoga seus papéis, grupos e concessões, desativa o vínculo e a identidade, incrementa `auth_version` e desativa na mesma transação os webhooks cujo principal é a conta (`principal_deactivated`). Tentar operar sobre conta de outra organização ou sobre uma pessoa humana responde como indisponível.
- Aparece identificada nas listas como `(conta de serviço)` no HTML e pelo campo `account_type` na API de vínculos. Fica de fora das listas e contagens da plataforma (`/platform/users/` e seletores de administrador), que respondem 404 para o detalhe.

## Administração de acesso

Os serviços em `skeleton.access.services` administram vínculos, papéis, grupos, associações, permissões de tabela e concessões de registro, sempre exigindo a capacidade correspondente. As páginas HTML e a API REST encaminham essas operações pelo mesmo despachante, `skeleton.access.resources` (`create_resource`, `update_resource` e `delete_resource`). A revogação de convites passa por `skeleton.tenancy.invitations.revoke_invitation`, que valida a capacidade e a organização do convite. `TablePermission` aceita apenas tabelas de negócio expostas e ações suportadas, nunca identidades globais nem tabelas técnicas. `RecordGrant` valida um alvo `SecuredRecord` vivo na mesma organização e um destinatário ativo — o UUID enviado pelo cliente não é suficiente.

Qualquer mudança capaz de remover o último administrador bloqueia `Organization` dentro de `atomic`, abrangendo desativação ou remoção de `Membership`, `MemberRole`, `GroupRole` e relações de grupo. Isso evita que requisições concorrentes aprovem duas remoções com base no mesmo estado anterior; em caso de conflito, nada é gravado. Remover uma `Membership` revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo antes de marcar a exclusão. Papéis e grupos com referências vivas exigem revogação prévia explícita, e papéis internos são protegidos.

**Saída voluntária.** Qualquer pessoa pode sair de uma organização quando desejar por meio de `skeleton.access.services.leave_organization(user, *, expected_revision=None)`, na página `/o/<org>/leave/` (`GET` confirma sem alterar; `POST` exige CSRF e a revisão do vínculo) ou via `POST /api/v1/o/<org>/membership/leave/` (com `If-Match` trazendo a revisão do vínculo). Sair não exige capacidade: o serviço adquire `organization_lock()` sem ator, bloqueia apenas o vínculo do próprio ator (seguindo a ordem Organization → vínculo), revoga `MemberRole`, `GroupMember` e `RecordGrant`, desativa e exclui logicamente o vínculo e encerra com `ensure_last_admin()`.

O último administrador recebe 409 sem alterações e precisa designar outro administrador antes de sair. O ator deve ser o do contexto atual, e o acesso cai já na requisição seguinte (`/o/<org>/` passa a responder 404). Retornar à organização exige novo convite, aprovação ou designação: nem a confirmação de email do JIT de SSO nem os fluxos de domínio verificado ou de link de ingresso podem readmitir automaticamente quem saiu.

Um `POST` sobre `Membership` existente executa a mesma transição de desativação que um `PATCH`, revogando `MemberRole`, `GroupMember` e `RecordGrant` e preservando a proteção do último administrador; reativar não restaura essas associações. O `restore` persiste o valor de `is_active` escolhido pelo serviço no mesmo CAS de restauração. Um vínculo inativo, porém não excluído, segue o ciclo de vida normal de remoção, mantendo todas as validações de organização, ACL e último administrador.

Desativar ou excluir logicamente o `User` global verifica **todas** as organizações em que ele é membro ativo, não apenas a atual. No PostgreSQL, `skeleton.assert_user_deactivation(uuid)` é uma função `SECURITY DEFINER` restrita pertencente ao migrator (credencial de migrations): executa com os privilégios do proprietário apenas para manter bloqueios e verificar administradores nas organizações, sem escrever dados de negócio nem abrir contexto organizacional amplo.

O privilégio público `EXECUTE` dessa função é revogado. A conexão runtime exige o plano de plataforma autenticado, enquanto operações offline usam a credencial owner; chamar `system_context(reason=...)` numa conexão runtime comum não a transforma em owner. Violações de integridade tornam-se `LastAdministrator`, e a tela `/platform/users/<id>/` responde 409 com rollback, nunca erro SQL ou 500.

### Designação de administrador pela plataforma

Ao criar uma organização em `/platform/organizations/new/`, o administrador inicial é indicado pelo email de uma identidade humana ativa; o formulário não lista as identidades da instalação. Emails desconhecidos, de identidades inativas ou de contas de serviço são recusados no próprio campo.

Se o administrador de uma organização sair ou ficar indisponível, o administrador da plataforma pode designar outro em `/platform/organizations/<id>/` (card "Administradores") pelo serviço `skeleton.access.administration.designate_organization_admin(*, actor, organization, user, replace=None, expected_version=None)`:

- executa em `system_context(reason="organization:designate-admin", organization_id=...)`, restrito àquela organização, e adquire `organization_lock()` antes de qualquer vínculo (preservando a ordem das travas);
- cria ou reativa o vínculo com o papel `organization_admin` via `provision_membership`. Quando `replace` é informado, remove apenas o papel `organization_admin` **direto** da pessoa substituída, mantendo o vínculo dela e sem tocar em papéis atribuídos via grupo;
- valida `ensure_last_admin()` ao final: qualquer alteração que deixaria a organização sem administrador ativo responde 409 sem escrita parcial;
- compara `expected_version` (a versão do conjunto de administradores exibido no formulário) após o lock, retornando `409 ConcurrentUpdate` em caso de divergência;
- recusa designar o próprio administrador da plataforma (403) e substituir alguém por si mesmo (400). Para entrar em uma organização, o administrador da plataforma precisa de convite ou do comando `bootstrap_identity` no CLI.

A auditoria é gravada pelos triggers na mesma transação: os eventos de `Membership` e `MemberRole` registram `source` igual a `system:organization:designate-admin` e o administrador da plataforma como ator. O formulário exige autenticação recente (redirecionando para reautenticação quando ausente), CSRF e `POST`.

**Decisão: leitura restrita da plataforma.** *Contexto:* para designar um administrador, a plataforma precisa saber quem administra a organização, mas ser administrador da plataforma não concede acesso ao tenant. *Decisão:* a única leitura tenant disponível à plataforma é `organization_overview(*, actor, organization)`, executada em `system_context(reason="platform:organization-inspect")`, que retorna um DTO imutável contendo a contagem de vínculos ativos, os emails dos administradores ativos (sinalizando os que o são apenas via grupo) e a versão desse conjunto. Um wrapper de execução bloqueia qualquer comando que não seja `SELECT` sem `FOR UPDATE` nem `FOR SHARE` enquanto o contexto está ativo. *Consequências:* a plataforma não acessa listas de membros comuns, papéis, grupos, convites nem dados de negócio, e continua recebendo 404 em `/o/<org>/` sem vínculo próprio. Qualquer nova necessidade exige decisão dedicada, nunca o reaproveitamento dessa razão.

Os serviços de ciclo de vida da plataforma (`update_platform_user`, `update_organization`, `deactivate_user` e `restore_user`) exigem `actor` obrigatório e recusam quem não é administrador da plataforma (`PermissionDenied`) ou não coincide com o ator do contexto (`InvalidContext`), mesmo quando chamados fora de `/platform/`; `actor=None` só é aceito em `system_context`. Criar, editar ou remover identidades e criar ou editar organizações exigem autenticação recente no `POST`.

A confirmação de remoção em `/platform/users/<id>/delete/` exibe apenas os dados da identidade, envia a revisão mostrada (retornando 409 se estiver obsoleta) e chama `deactivate_user(user, actor=..., delete=True, expected_revision=...)` — realizando exclusão lógica sem acionar o collector de exclusão física do Django. Perfis, `Membership`s e identificadores externos (`subjects`) são preservados como histórico, enquanto o ciclo de vida invalida logins e sessões. Nunca habilite exclusão física da identidade ou de seus dependentes.

## Papéis limitados de plataforma

O administrador pleno da plataforma continua definido exclusivamente pelas flags (`is_platform_admin`). De forma complementar, `sk_accounts.PlatformRoleAssignment` atribui a uma identidade um papel limitado com capacidades declaradas em código (`skeleton/accounts/platform.py`):

| Papel | Capacidades | Telas |
|---|---|---|
| `auditor` | `audit.read`, `inventory.read` | `/platform/audit/` e catálogo de tabelas, somente leitura |
| `operator` | `updates.operate` | `/platform/updates/` (planos, envio, aprovação e operações) |
| `billing` | `billing.operate` | `/platform/billing/`: produtos, preços, vouchers e contas de provedor ([cobrança](billing.md#operação-da-plataforma)); a conta de uma organização continua restrita ao administrador |

O decorador `platform_page(capability=..., writes=...)` libera a tela ao portador da capacidade: somente para leitura, exceto quando a capacidade permite escrita (`updates.operate`, `billing.operate`). Todo o restante de `/platform/` permanece exclusivo do administrador pleno. `require_release_operator` aceita tanto o administrador quanto o operador nos serviços de deployment. Conceder ou revogar papéis limitados na página da identidade exige administrador pleno, CSRF e autenticação recente, e incrementa `auth_version` da pessoa; as capacidades são recalculadas a cada requisição.

A leitura da auditoria global pelo auditor respeita a RLS: a política `skeleton_audit_select` libera eventos sem organização quando `skeleton.platform_admin='on'` **ou** `skeleton.platform_auditor='on'`. Esta última GUC é ativada exclusivamente por `platform_audit_scope(user)`, aberta pela página de auditoria após validar `audit.read` em uma requisição de sessão sem tenant; fora desse escopo, o auditor não enxerga eventos globais. A inserção continua restrita ao escritor de auditoria, e `UPDATE` e `DELETE` permanecem bloqueados. A migration `sk_audit.0004_platform_auditor` reinstala a política, e `check_deployed_schema` recusa a versão anterior.

## Contrato de contextos

O contexto informa à execução **quem está agindo, em qual organização e em qual transação**; ele nunca representa uma permissão extra. Utilize sempre as entradas abaixo em vez de manipular variáveis de sessão SQL manualmente:

| Entrada | Uso e contrato |
|---|---|
| `request_context(*, actor, request_id, source, using='default')` | Abre a transação externa da requisição |
| `tenant_context(*, organization_id, actor, source, using='default')` | Valida vínculo e organização ativos |
| `system_context(*, reason, organization_id=None, using='default')` | Exige motivo; restrito a infraestrutura e CLI, nunca derivado de body ou header HTTP, salvo as razões fechadas abaixo |
| `external_context(*, organization_id, actor, source='http:portal', using='default')` | Usado no portal de pessoas externas ([acesso externo](#acesso-externo-portal)): valida `ExternalAccess` em vez de vínculo e marca `external=True` |

Cada entrada grava as oito GUCs `skeleton.*` em uma única instrução (`set_config(..., true)`, local à transação) — incluindo `skeleton.external`, ligada apenas no portal, e `skeleton.system`, ligada apenas em contexto de sistema e verificada pelo guard das [tabelas globais de apps](table-catalog.md#tabelas-globais-de-apps). Se um contexto aninhado terminar com exceção, o rollback do savepoint restaura automaticamente as GUCs anteriores e propaga a exceção original (sem mascarar erros de banco nem fechar a conexão); na saída normal, as GUCs do contexto anterior são regravadas. Um `system_context` de topo salva e restaura o tenant selecionado, permitindo que dois blocos de sistema consecutivos no mesmo processo operem em organizações distintas.

Em requisições HTTP, `system_context` aceita exclusivamente as razões listadas em `HTTP_SYSTEM_REASONS` (`skeleton/tenancy/context.py`), sempre definidas pelo código do servidor após validação própria e nunca recebidas do cliente:

- **`VALIDATED_IDENTITY_REASONS`**: `invitation:validated`, `sso:validated` e `external:validated`, usados após validação de convite, SSO e convite do portal.
- **`PLATFORM_ADMIN_REASONS`** (exigem ator administrador da plataforma): `organization:create`, `organization:bootstrap`, `builtin-roles`, `organization:designate-admin`, `platform:organization-inspect`, `platform:billing` (ações da plataforma na cobrança de uma organização), `platform:apps` (atribuição ou retirada de apps pela plataforma) e `platform:setting-lock` (travas e valores de configuração ou campos de app geridos pela plataforma, além dos jobs da política de organizações novas).
- **`SIGNUP_REASONS`**: `organization:signup`, aberta apenas por `skeleton.tenancy.signup` para um usuário autenticado criar a própria organização após validação, nunca em requisição anônima.
- **`PLATFORM_CLIENT_REASONS`**: `platform:api`, admitida apenas em requisições com token de plataforma que possua `platform.billing:read` ou `platform.billing_contacts:read`, para leitura da cobrança de uma organização ([API de gestão](platform-api.md#rotas)).
- **`JOIN_REASONS`** (abertas apenas pelos serviços de admissão, para uma organização): `join:request` (exige ator autenticado e age em nome do solicitante), `join:link` (valida links também antes do login, usando o resumo do token como prova) e `join:domain` (roda na confirmação de email, mesmo antes do login, usando o email confirmado como prova).
- **Exclusivas de CLI**: `domain:recheck` (reverificação de domínios), `directory:sync` (sincronização de diretório) e `idp:import` (importação de provedores).

Qualquer outra razão — ou uma razão de plataforma solicitada por quem não é administrador da plataforma — levanta `InvalidContext`. Adicionar uma nova razão requer alterar a constante e atualizar o teste que fixa a lista (`test_system_reasons_open_to_http_are_a_closed_list`).

Uma requisição global pode entrar em uma organização validada e retornar ao contexto global, mas nunca alternar de Alfa para Beta na mesma transação. Blocos aninhados reutilizam identidade e organização via savepoints; o ator só muda no hook de login ou logout após a conclusão dos estágios do allauth. Operações sobre múltiplas organizações devem abrir transações independentes por organização. O contexto é sempre restaurado no `finally`, utiliza somente `SET LOCAL` e não sobrevive a `COMMIT` nem `ROLLBACK`.

```python
for org_id in materialized_organization_ids:
    with system_context(reason="job:reconcile-permissions", organization_id=org_id):
        # Uma organização por transação, APIs internas apropriadas.
        reconcile_one(org_id)
```

Esse exemplo ilustra apenas o gerenciamento de contexto — ele não concede permissão HTTP nem `BYPASSRLS`. Nunca envolva o laço inteiro em um `atomic` externo. Callbacks de `on_commit` devem receber dados já materializados, e consultas posteriores exigem um novo contexto. Como querysets do Django são avaliados sob demanda, materializa-se o resultado ainda dentro do bloco em vez de retornar o queryset para avaliação posterior.

### Acesso externo (portal)

Uma pessoa externa — como um cliente que consulta seus próprios documentos — acessa a organização **sem ser membro**. A identidade continua sendo um `User` comum; o caráter externo pertence ao vínculo `ExternalAccess` (organização, pessoa, app que a admitiu e status ativo), separado de `Membership`. Listas e seletores de membros, papéis, a métrica de assentos (`members`) e a regra de último administrador ignoram acessos externos. A organização habilita o portal via `ExternalAccessPolicy` (desligado quando não há registro). O que a pessoa externa visualiza é definido pela app que a admitiu por meio de serviços marcados para uso externo; o núcleo apenas controla a existência do acesso.

`external_context` exige um `ExternalAccess` vivo e ativo, pessoa e organização ativas, e portal ligado. Qualquer falha levanta `PermissionDenied("Organization is unavailable.")` — a mesma mensagem de vínculo ausente. Esse contexto não se aninha com `tenant_context` (em nenhuma ordem, levantando `InvalidContext`), não promove para `system_context` e nunca é aberto por token Bearer. Por isso, mesmo quem é membro **e** externo na mesma organização não possui nenhum poder de membro dentro do portal.

**Negações explícitas** em contexto externo, aplicadas antes de qualquer consulta de vínculo:

| Onde | Regra |
|---|---|
| `skeleton/access/policies.py` | `active_membership`, `authorized_queryset`, `require_record_action` e `authorized_audit_queryset` levantam `PermissionDenied`; `has_table_permission` e `is_organization_admin` retornam falso; `table_permissions`, `effective_roles` e `group_ids` retornam vazio |
| `skeleton/access/capabilities.py` | `capabilities_for` retorna vazio |
| `skeleton/records` | Toda entrada do gateway (especificação exposta, tabelas legíveis, queryset, criação, edição, acesso, exclusão, restauração, seletor de referência, além de exportação e importação) recusa |
| `service_method` | Sem `allow_external=True`, o método responde `Forbidden(code="external_not_allowed")`. No núcleo, possuem essa marca `ExternalAccessService.me` e `FileService.external_download_url`; além disso, quando chamados a partir de um método `allow_external` da app dona, também são aceitos `SequenceService.next_number`, `EventService.publish` e os eventos gerenciados da agenda. Qualquer outra chamada no portal continua retornando `external_not_allowed` ([portal](portal.md#superfícies-do-núcleo-para-os-recursos-da-própria-app-741)) |

A capacidade `external.manage` (do administrador) gere pessoas externas, e a métrica de plano `external_people` contabiliza os acessos ativos. Escritas realizadas no portal registram a pessoa como ator e `source="http:portal"` na auditoria. Detalhes sobre convite, aceite e `ExternalAccessService` estão em [portal](portal.md); o aceite executa em `system_context(reason="external:validated")`, parte de `VALIDATED_IDENTITY_REASONS`. Em testes, `skeleton.testing.external` disponibiliza `external_scenario` (organizações A e B com portal ligado, uma pessoa externa e uma administradora que também é externa) e `acting_external`.

### Direitos da assinatura

Na plataforma, o papel limitado **Operador de cobrança** (`billing`, capacidade `billing.operate`) administra o catálogo de cobrança, mas não a conta de uma organização. Dentro da organização, as capacidades `billing.view` e `billing.manage` (restritas ao administrador) controlam a [área de cobrança](billing.md#superfícies).

Com a [cobrança](billing.md#ganchos-de-direito) habilitada, a situação financeira da organização atua como um limite adicional, verificado após o vínculo e antes da autorização de registros: uma app paga sem direito vigente fica indisponível como uma app retirada pela plataforma; uma organização `restricted` fica em modo somente leitura para pessoas; e uma organização `suspended` libera apenas as rotas de cobrança para quem pode regularizá-la. Quem não é membro continua recebendo 404, sem descobrir o estado da conta. Essa situação é obtida apenas da política registrada no servidor, nunca de body ou header, e só restringe acesso — nunca concede.

### Host de organização

Em um endereço próprio `<slug>.<domínio base>` ([#482](https://github.com/alaorneto/skeleton/issues/482)), o host **apenas restringe, nunca concede**: vínculo, RLS e autorização funcionam exatamente como no domínio principal, e não membros continuam sem acesso. O escopo de site deriva exclusivamente de `request.get_host()`, já validado por `ALLOWED_HOSTS`, nunca de header, body ou query; `X-Forwarded-Host` só é considerado segundo a configuração existente de proxy confiável (`DJANGO_TRUST_PROXY`). No host de uma organização, qualquer caminho de outra organização (`/o/<outra>/…` ou `/api/v1/o/<outra>/…`) responde 404, mesmo para quem pertence a ambas, e o host nunca abre `system_context`.

Rotas globais permitidas em um host de organização (lista explícita em `skeleton/tenancy/site_middleware.py`; qualquer outra rota retorna o mesmo 404 neutro):

| Rota | Decisão | Por quê |
|---|---|---|
| `/accounts/…` (login, logout, cadastro, senha, MFA, SSO) | permitida | identidade |
| `/profile/…`, `/me/…` | permitida | páginas da própria pessoa |
| `/api/v1/…` (exceto `/api/v1/o/<outra>/…`) | permitida | mesma API, restrita à organização do host |
| `/site/…`, `/favicon.ico`, `/sw.js`, `/health/…`, `STATIC_URL` | permitida | marca pública, push e sondas |
| `/brand/…` (tema e imagens da instância) | permitida | a organização carrega o tema da instância sob a própria marca |
| `/invite/<uuid do site>/…`, `/join/<uuid do site>/…` | permitida | convite e ingresso na própria organização |
| `/`, `/notifications/`, `/flows/tasks/` | viram a rota da organização | a rota da organização prevalece |
| `/platform/…`, `/admin/…`, `/join/` (diretório), `/join/domain/…`, `/join/requests/…`, `/billing/webhooks/…`, `/invite/<outra>/…` | recusada | plataforma ou múltiplas organizações |

**O que é público antes do login.** Sem autenticação, o host da organização expõe somente a marca publicada (nome, cor, logo, ícone e idioma, lidos do índice `OrganizationSite`) e, quando publicada pelo administrador, a página pública (espelho `OrganizationSitePage`), ambos lidos sem contexto de tenant e sem `system_context`. Qualquer outro caminho organizacional redireciona para o login, e `/o/<uuid>/` no domínio principal nunca é público. O host identifica apenas qual organização corresponde ao endereço, sem expor membros nem registros.

No host de uma organização, as listagens de vínculos são **estreitadas** para aquela organização: isso se aplica a `memberships_for_user` (seletor, contadores, caixa de entrada, tarefas globais, `OrganizationService.my_organizations` e `GET /api/v1/organizations/`) e a `for_each_organization` (Minha agenda, feed do calendário e OpenAPI agregado). O estreitamento afeta apenas a **apresentação**, nunca a autorização — nenhuma decisão de acesso depende do escopo de site.

Operações de ciclo de vida, segurança e privacidade da conta passam `all_organizations=True` e consideram todas as organizações em qualquer host (exportação de dados pessoais, sugestões e diretório de admissão). As consultas diretas de `Membership` para último administrador, revogação, `auth_version` e eliminação não utilizam essas funções e não são afetadas. Uma pessoa autenticada sem vínculo com a organização do host recebe a página "Você não tem acesso" (status 404), idêntica para qualquer caminho e exibindo apenas o nome público da organização; na API, retorna o JSON 404 padrão.

Após o login, um parâmetro `next` absoluto só é aceito quando aponta para o endereço de uma organização com site ativo, no mesmo esquema e porta da URL pública, e desde que a requisição ocorra em um host que compartilhe a sessão ([login no endereço da organização](authentication-sso.md#login-no-endereço-da-organização)); qualquer outro host sob o domínio base cai no destino padrão.

**Domínios próprios.** Somente o administrador da organização cadastra, verifica e remove um domínio próprio, sob o lock da organização. A posse (`SiteDomainClaim`) é global e única por domínio, pois autoriza um host externo a representar a organização: ela é gravada apenas na transação de verificação, após a consulta DNS (feita fora de lock), e um domínio já comprovado por outra organização falha com a mesma resposta neutra, sem revelar o titular. A plataforma lista e revoga posses em `/platform/domains/`.

O cadastro recusa domínios iguais ou inferiores ao domínio base, o host da instalação, IPs, `localhost` e curingas. Apenas domínios com posse viva de organizações ativas entram nos hosts permitidos (`SiteAllowedHosts`), e a lista dinâmica nunca aceita curinga nem padrão com ponto inicial; um host da plataforma listado explicitamente mantém seu papel de plataforma mesmo se cadastrado.

Um projeto derivado pode liberar rotas globais próprias no host de organização adicionando seus prefixos em `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES` (lista vazia por padrão, como `["/relatorios/"]`). Use isso apenas para rotas da própria pessoa, nunca para rotas que leiam várias organizações.

### Leitura agregada entre organizações

Enquanto uma requisição comum opera em um único tenant, visões como "Minha agenda" ou o feed do calendário precisam consolidar itens de todas as organizações da pessoa. Para isso, o núcleo oferece `skeleton.tenancy.aggregate.for_each_organization(user, fn, *, source, app_label=None, limit=50)`:

- A view deve ser decorada com `@aggregate_route`, fazendo o `TenantContextMiddleware` não abrir contexto nem transação; `for_each_organization` recusa executar se já houver um contexto aberto (`InvalidContext`).
- Os vínculos ativos da pessoa são listados em uma transação curta, ordenados por nome, até o teto de `limit` (no máximo 50 organizações).
- Para cada organização, `fn(organization_id)` executa dentro de um par próprio `request_context` + `tenant_context`, aplicando `TablePermission`, visibilidade, ativação de apps e RLS exatamente como em uma requisição comum. No PostgreSQL, a transação é `READ ONLY`, impedindo qualquer escrita dentro de `fn`.
- Uma organização é ignorada quando o vínculo foi desativado, quando `app_label` não está ativa nela ou quando `fn` é recusado com `PermissionDenied` (por exemplo, falta de `TablePermission`) ou com `Forbidden` ou `Unavailable` de uma API de serviços.
- `fn` deve retornar valores materializados: devolver um queryset, iterador ou gerador levanta `LazyAggregateResult`, pois nenhuma consulta ao banco pode ocorrer após o fechamento da transação da organização.
- Quando `organization_id=...` é informado, apenas essa organização é processada — desde que o vínculo esteja ativo; sem vínculo, o resultado é vazio (mecanismo usado pelo link de assinatura de calendários).

Não existe escrita agregada entre organizações: toda operação de escrita ocorre sempre dentro de uma organização específica escolhida pela pessoa.

### Ordem dos bloqueios administrativos

Um bloqueio de linha (`SELECT ... FOR UPDATE`) impede que outras transações modifiquem aquela linha até o término da transação atual. Em alterações administrativas, adquira sempre o bloqueio de `Organization` primeiro via `skeleton.access.services.organization_lock(user, capability=...)` (sem `capability`, exige o administrador) e só depois carregue ou bloqueie o vínculo, papel, grupo ou link a ser modificado. Requisições HTTP sempre informam o ator autenticado; omitir o ator é reservado ao provisionamento interno.

O bloqueio da organização utiliza `SELECT ... FOR NO KEY UPDATE`: dois bloqueios sobre a mesma organização continuam se excluindo mutuamente, mas não conflitam com o `FOR KEY SHARE` adquirido por chaves estrangeiras ao inserir ou confirmar registros da organização. Assim, commits de outras transações não ficam esperando operações administrativas, e transações que já inseriram registros não entram em deadlock ao solicitar o bloqueio. A API dedicada e os `POST`s administrativos HTML seguem rigorosamente essa ordem, os serviços reutilizam o mesmo bloqueio e a revisão é verificada **depois** da aquisição dos locks. Inverter a ordem — uma transação travando o vínculo à espera da organização e outra travando a organização à espera do vínculo — provoca deadlock. Abortar transações no banco não é estratégia de concorrência, e não há retry automático para mascarar esse erro.

## RLS PostgreSQL

**Row-Level Security (RLS)**, ou segurança por linha, é uma proteção nativa do PostgreSQL que restringe quais linhas a conexão pode ler ou gravar. No núcleo, a RLS garante o isolamento por organização, enquanto os serviços Django continuam responsáveis por papéis, visibilidade, ações e exclusão lógica.

O contexto grava parâmetros locais na sessão do PostgreSQL chamados **GUCs**: `skeleton.user_id`, `skeleton.tenant_id`, `skeleton.request_id`, `skeleton.source` e `skeleton.platform_admin`. O ator provém da sessão autenticada e o UUID da requisição é gerado pelo servidor; a organização vem da URL validada, nunca de headers ou do corpo da requisição. Valores vazios não retornam nenhuma linha, e UUIDs inválidos geram erro em vez de assumir algum padrão permissivo.

Tabelas organizacionais aplicam `ENABLE` e `FORCE ROW LEVEL SECURITY`, com política para a conexão runtime definida como `USING T / WITH CHECK T`, em que `T` exige que a organização da linha seja igual ao tenant na GUC. `USING` filtra linhas existentes; `WITH CHECK` valida a organização das linhas gravadas. A RLS **não** filtra `deleted_at`, papéis nem visibilidade: uma nota privada de Alfa não se torna legível para Bob apenas por ambos estarem em Alfa.

**Autodescoberta deliberada.** Uma tabela organizacional pode declarar na especificação congelada de `InstallRecordInfrastructure` a chave `self_discovery_column` (a coluna do usuário dono da linha), refletida no modelo pelo atributo `self_discovery_field`. Nesses casos, `SELECT` permite `(T OR U)`, em que `U` compara a coluna ao usuário da GUC, enquanto `INSERT`, `UPDATE` e `DELETE` continuam exigindo `T`. As políticas chamam-se `skeleton_self_select`, `skeleton_self_insert`, `skeleton_self_update` e `skeleton_self_delete`; `check_skeleton_schema` reprova políticas ausentes, extras ou alargadas, bem como divergências entre o modelo e a especificação.

A leitura entre organizações só ocorre pelo helper explícito `own_rows(model, user)` de `skeleton.tenancy.context`, que filtra o usuário também em Python (como defesa em profundidade sobre a RLS) e exige que ele seja o ator do contexto; o manager padrão continua restrito ao tenant. `Membership` utiliza esse recurso desde `sk_tenancy.0003_membership_self_discovery` (com `memberships_for_user(user)` projetando IDs, status e nomes das organizações), permitindo que Alice descubra seus vínculos em Alfa e Beta antes de selecionar um tenant, sem escrita cruzada nem política recursiva. Da mesma forma, `FlowTask` declara a chave sobre `assignee_id` (`sk_flows.0003_task_self_discovery`, [#409](https://github.com/alaorneto/skeleton/issues/409)), para que a caixa global de tarefas liste apenas as tarefas atribuídas à própria pessoa sem cruzar com a instância (que a RLS de outro tenant mantém oculta).

Na tabela `AuditEvent`, o `SELECT` da conexão runtime restringe-se ao tenant, liberando eventos globais apenas quando `platform_admin=on`. O `INSERT` pertence exclusivamente à role SQL do trigger, e `UPDATE` e `DELETE` são proibidos. Tabelas globais como `User`, `UserProfile`, `Organization` e o catálogo não usam RLS tenant e nunca são expostas como listagens genéricas a membros. Note que uma role SQL é uma credencial do banco de dados, sem relação com o modelo empresarial `Role`.

**Limite da proteção:** `set_config` está acessível a quem detém a credencial runtime. Portanto, a RLS protege contra filtros organizacionais esquecidos e fluxos não autorizados na aplicação, mas não contra uma credencial runtime comprometida ou injeção SQL arbitrária capaz de alterar GUCs. Da mesma forma, DBA, superuser SQL e roles com `BYPASSRLS` ignoram `FORCE`, e a flag `is_superuser` do Django não transforma a conexão em owner do banco. Proteja os DSNs (endereços e credenciais de conexão), utilize SQL parametrizado e blinde todas as superfícies HTTP.

## Auditoria autorizada

Administradores organizacionais visualizam a auditoria da própria organização. Os demais membros precisam de `table.audit` e leitura autorizada do registro atual — ou autorização de `restore`, caso o registro esteja marcado como excluído. O filtro é sempre aplicado **antes** de paginar ou contar. Se o registro foi removido offline e não existe mais, apenas administradores organizacionais ou da plataforma podem consultar seus eventos: nunca reconstrua a ACL a partir do conteúdo histórico. O inventário técnico e os eventos globais são exclusivos da plataforma (acessíveis ao administrador e, em somente leitura, ao auditor de plataforma).

A origem de cada evento vem da GUC `skeleton.source` da transação e indica qual credencial executou a escrita: `http` para sessão web, `http:token:<prefixo>` para [token de API](api.md#autenticação-por-token), `http:oidc:<client_id>` para access token de [cliente nativo](api.md#access-tokens-oidc-de-clientes-nativos) e `system:<motivo>` para contextos de sistema (como `system:native-client:register`). Nenhuma origem registra o segredo do token.
