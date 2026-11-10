# Controle de acesso e contexto organizacional

Uma organização é a fronteira de dados que o código chama de **tenant**. Autenticar responde “quem é a pessoa?”; autorizar responde “ela pode executar esta ação neste registro desta organização?”. Alice pode ter acesso em Alfa e Beta sem que uma permissão de Alfa valha em Beta. Veja [autorização e contexto](concepts.md#autorização-e-contexto) e [identidade e organizações](concepts.md#identidade-e-organizações).

## Papel, grupo, permissão e concessão

- **Papel (`Role`)**: conjunto de capacidades organizacionais, como leitor ou administrador. A associação direta é `MemberRole`.
- **Grupo (`AccessGroup`)**: reúne vínculos de pessoas (`GroupMember`), por exemplo a equipe Financeiro. Pode receber papéis por `GroupRole`; não é `auth.Group` do Django.
- **Permissão de tabela (`TablePermission`)**: permite uma ação, como `read` ou `update`, sobre um tipo de registro. Sem ela, a ação é negada, mesmo ao proprietário.
- **Concessão de registro (`RecordGrant`, ou grant)**: compartilha uma ação sobre uma linha com um vínculo, grupo ou papel. Não substitui a permissão de tabela, o vínculo ativo nem o isolamento organizacional.
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

`authorized_queryset(model, *, user, action='read', include_deleted=False)` filtra antes de paginação/contagem. `require_record_action(record, *, user, action)` revalida um registro. Nunca exponha resultados de `_base_manager`/`all_objects` ou relações reversas como alternativa. Em SQLite essas entradas são uma fronteira indispensável; RLS no PostgreSQL não substitui autorização por registro.

## Algoritmo

As verificações seguem esta ordem; falhar em uma camada não pode ser compensado por outra:

1. Organização ativa, usuário ativo/não excluído e `Membership` ativa/viva nessa organização.
2. Papéis efetivos: `MemberRole` vivos e `GroupRole` vivos dos grupos com `GroupMember` vivo. Não há cache de autorização entre requisições.
3. `TablePermission` da ação obrigatória. Uma concessão de registro não a substitui. O padrão é negar (`default deny`); `member` não recebe permissões automaticamente.
4. Leitura: proprietário é o ator **ou** `visibility=organization` **ou** `visibility=group` e ator pertence ao `owning_group` **ou** existe `RecordGrant read` para seu vínculo/grupo/papel efetivo.
5. Atualização/exclusão: proprietário é o ator **ou** existe `RecordGrant` específico da ação. Compartilhar leitura com organização/grupo não compartilha escrita.
6. Restauração: permissão `restore` e condição de proprietário/administrador. Ler uma marca de exclusão (tombstone) também exige autorização de restauração.
7. O papel interno `organization_admin` tem concessões reconciliadas para tabelas elegíveis e exceção explícita à visibilidade **só na própria organização**. `is_superuser` global não pula vínculo/papel nas rotas organizacionais.
8. Administrador da plataforma é definido só por `is_platform_admin` (`skeleton/accounts/platform.py`): autenticado, ativo, não excluído, `is_staff` e `is_superuser`. O mesmo predicado liga `Context.platform_admin` e a GUC `skeleton.platform_admin`, abre a auditoria global (`authorized_audit_queryset`), responde `has_perm` e decide menus e selos nos templates (context processor `platform_admin` e filtros `platform_admin`). Conta só superuser ou só staff não é administrador. `update_platform_user` e `deactivate_user` bloqueiam os administradores ativos em ordem de `sysid` (`FOR UPDATE`) antes da identidade alvo e recusam com `LastPlatformAdministrator` (409, sem escrita parcial) a mudança que deixaria a plataforma sem administrador ativo; dois rebaixamentos concorrentes serializam e um deles recebe 409.

### Exemplo com Alfa e Beta

Alice e Bob têm vínculo ativo e `table.read` em Alfa. Alice cria uma nota privada: ela consegue ler; Bob não. Ao mudar a visibilidade para `organization`, Bob passa a ler, mas não a editar. Mesmo um `RecordGrant update` para Bob não permite editar sem `table.update`.

Em Beta, Alice pode ter outro papel e Bob pode nem ter vínculo. Nada do que foi concedido em Alfa abre a nota em Beta. Um grupo Financeiro em cada organização também são dois grupos distintos. Revogar `MemberRole`, `GroupMember` ou uma concessão retira seu efeito na próxima requisição.

Criar exige `table.create`; o proprietário inicial é o ator e a visibilidade é `private`. Alterar `owner`, `visibility`, `owning_group` ou a lista de concessões (ACL, lista de controle de acesso) exige `manage_access` **e** condição de proprietário ou administrador da organização. Esses campos nunca são aceitos pelo PATCH comum. O servidor valida proprietário ativo e grupo vivo da mesma organização.

## Capacidades e papéis internos

Uma **capacidade** é um direito administrativo da organização, como gerir pessoas ou apps. Capacidades vêm só de **papéis internos** declarados em código (`skeleton/access/capabilities.py`, registry `BUILTIN_ROLES`). Um papel customizado nunca carrega capacidade, mesmo que reutilize um código interno: editar papéis não vira escalada de privilégio.

| Papel interno | Capacidades |
|---|---|
| `organization_admin` | todas: `members.view`, `members.manage`, `invitations.manage`, `admission.decide`, `admission.configure`, `apps.manage`, `access.manage`, `identity.configure` |
| `people_manager` (gestor de pessoas) | `members.view`, `members.manage`, `invitations.manage`, `admission.decide` |
| `app_manager` (gestor de apps) | `apps.manage` |
| `member` | nenhuma |

`has_capability(user, cap)` e `require_capability(user, cap)` leem os papéis efetivos (diretos e via grupo) do tenant atual a cada chamada, sem cache: revogar `MemberRole` vale na requisição seguinte, e uma capacidade em Alfa não vale em Beta. `organization_admin` continua o único administrador pleno: só ele tem a exceção de visibilidade em `authorized_queryset`, a proteção de último administrador e a função SQL `skeleton.assert_user_deactivation`.

Cada recurso administrativo exige uma capacidade: vínculos pedem `members.view` para ler e `members.manage` para alterar; convites, `invitations.manage`; papéis, grupos, associações, permissões de tabela e concessões, `access.manage`. A configuração de apps continua restrita ao administrador. Quem tem outra capacidade recebe **404** num recurso fora da sua delegação, como se ele não existisse; quem não tem capacidade nenhuma mantém o 403 histórico. Qualquer capacidade exige MFA quando `SKELETON_REQUIRE_ADMIN_MFA` está ligado, e o menu "Administração" mostra só as seções da capacidade do ator.

**Regra contra escalada:** quem não tem `access.manage` só convida com papéis sem capacidade e não desativa nem remove quem tem papel com capacidade (403, sem escrita). O seletor de papéis do convite segue a mesma regra. `reconcile_builtin_roles` (também `manage.py reconcile_builtin_roles`) cria os papéis internos que faltam em cada organização ativa, numa transação por organização, é idempotente e reporta o código de um papel customizado que colide com um interno; renomeie esse papel e repita.

## Administração de acesso

Os serviços em `skeleton.access.services` administram vínculos, papéis, grupos, associações, permissões de tabela e concessões de registro, cada um com a capacidade correspondente (seção anterior). `TablePermission` só aponta a tabela de negócio exposta e ação suportada, nunca identidade global ou tabela técnica. `RecordGrant` resolve um alvo `SecuredRecord` vivo, da mesma organização, com destinatário ativo; o UUID enviado pelo cliente não basta.

Mudanças que podem remover o último administrador bloqueiam `Organization` dentro de `atomic`: desativação/remoção de `Membership`, `MemberRole`, `GroupRole` e relações de grupo. Assim, operações concorrentes não aprovam duas remoções com base no mesmo estado antigo. Conflito deixa tudo inalterado. Remover `Membership` revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo antes da marca de exclusão. Papéis/grupos com referências vivas exigem revogação explícita prévia; papéis internos são protegidos.

**Saída voluntária.** Qualquer pessoa sai de uma organização quando quiser, por `skeleton.access.services.leave_organization(user, *, expected_revision=None)`, em `/o/<org>/leave/` (GET confirma sem alterar; POST com CSRF e a revisão do vínculo) ou em `POST /api/v1/o/<org>/membership/leave/` (`If-Match` da revisão do vínculo). Sair não exige capacidade: o serviço toma `organization_lock()` sem ator, bloqueia só o vínculo do próprio ator (Organization → vínculo, como as demais mudanças administrativas), revoga `MemberRole`, `GroupMember` e `RecordGrant`, desativa e exclui logicamente o vínculo e termina com `ensure_last_admin()`. O último administrador recebe 409 e nada muda; designe outro antes. O ator precisa ser o do contexto; o acesso cai na requisição seguinte (`/o/<org>/` responde 404). Voltar exige convite, aprovação ou designação: a confirmação de email do JIT de SSO não readmite quem saiu, e os fluxos de domínio e de link (#26, #28) também não podem readmitir automaticamente.

POST de `Membership` existente usa a mesma transição de desativação que PATCH: revoga `MemberRole`/`GroupMember`/`RecordGrant` e conserva a proteção do último administrador. Reativar não ressuscita essas associações. Restore persiste `is_active` escolhido pelo serviço no único CAS de restauração. Um vínculo inativo, mas não excluído, pode ser removido pelo ciclo de vida normal; organização, ACL e último administrador continuam validados.

Desativar ou excluir logicamente o `User` global verifica **todas** as organizações onde ele é membro ativo, não apenas a atual. No PostgreSQL, `skeleton.assert_user_deactivation(uuid)` é uma função `SECURITY DEFINER` restrita: pertence ao migrator (credencial de migrations), executa com privilégios do proprietário e só verifica administradores/mantém bloqueios nas organizações. Não escreve negócio nem concede contexto organizacional geral.

O privilégio público `EXECUTE` é revogado. A conexão runtime exige o plano de plataforma autenticado; operações offline usam owner. `system_context(reason=...)` numa conexão runtime comum não transforma a credencial em owner. Violação de integridade vira `LastAdministrator`; o admin global responde 409 com rollback, não erro SQL/500.

A confirmação de exclusão de User no Django Admin descreve somente a identidade: não usa o collector de exclusão física do Django. Perfis, Memberships e identificadores de identidade externa (`subjects`) permanecem históricos; o ciclo de vida invalida login/sessões. Não habilite ações de exclusão física da identidade nem de seus dependentes.

## Contrato de contextos

O contexto informa à execução **quem está agindo, em qual organização e em qual transação**. Ele não é uma permissão adicional. Use as entradas abaixo em vez de preencher variáveis de sessão SQL manualmente:

| Entrada | Uso e contrato |
|---|---|
| `request_context(*, actor, request_id, source, using='default')` | Abre a transação externa da requisição |
| `tenant_context(*, organization_id, actor, source, using='default')` | Valida vínculo e organização ativos |
| `system_context(*, reason, organization_id=None, using='default')` | Exige motivo; somente infraestrutura/CLI, nunca opção de body/header HTTP |

Uma requisição global pode entrar em uma organização validada e voltar ao contexto global. Não pode trocar de Alfa para Beta na mesma transação. Blocos aninhados reutilizam identidade/organização usando savepoints (pontos de retorno dentro da transação); o ator só muda no hook de login/logout depois dos estágios allauth completos. Trabalho com várias organizações abre transações independentes por organização. O contexto é restaurado em `finally`; usa somente `SET LOCAL` e não sobrevive a COMMIT/ROLLBACK.

```python
for org_id in materialized_organization_ids:
    with system_context(reason="job:reconcile-permissions", organization_id=org_id):
        # Uma organização por transação, APIs internas apropriadas.
        reconcile_one(org_id)
```

O exemplo acima ilustra o contexto, não concede permissão HTTP nem `BYPASSRLS`. Não envolva todo o laço em uma `atomic` externa. `on_commit` recebe dados já materializados; consultas posteriores precisam de novo contexto. Um queryset Django é avaliado sob demanda: converta-o em dados dentro do bloco, não o devolva para consultar depois.

### Ordem dos bloqueios administrativos

Um bloqueio de linha (`SELECT ... FOR UPDATE`) impede outra transação de modificar aquela linha até o fim da transação atual. Para alterações administrativas, adquira primeiro o bloqueio de `Organization` com `skeleton.access.services.organization_lock(user, capability=...)` (sem `capability`, exige o administrador) e só depois carregue/bloqueie o vínculo, papel, grupo ou link a alterar. HTTP sempre passa o ator autenticado; a ausência de ator é reservada ao provisionamento interno.

A API dedicada e os POSTs administrativos HTML seguem essa ordem. Os serviços reutilizam o mesmo bloqueio, e a revisão é comparada **depois** de adquirir os locks. Inverter a ordem — uma requisição segurando o vínculo e esperando a organização, outra segurando a organização e esperando o vínculo — causa deadlock. O banco abortar uma transação não é uma solução de concorrência; não há retry automático para esconder esse erro.


## RLS PostgreSQL

**Row-Level Security (RLS), ou segurança por linha**, é uma proteção do banco que restringe quais linhas a conexão pode ler ou escrever. Aqui ela verifica a organização; os serviços Django continuam responsáveis por papéis, visibilidade, ações e exclusão lógica.

O contexto instala parâmetros locais da sessão PostgreSQL, chamados **GUCs**: `skeleton.user_id`, `skeleton.tenant_id`, `skeleton.request_id`, `skeleton.source`, `skeleton.platform_admin`. O ator vem da sessão autenticada e o UUID da requisição é gerado pelo servidor. A organização vem da URL validada, não de header/body. Valores vazios não veem linhas; UUID inválido gera erro, não um valor alternativo permissivo.

Tabelas organizacionais usam `ENABLE` + `FORCE ROW LEVEL SECURITY` e policy da conexão runtime (processo da aplicação) `USING T / WITH CHECK T`, onde T significa “organização da linha igual ao tenant da GUC”. `USING` limita linhas existentes; `WITH CHECK` verifica a organização da linha escrita. RLS **não** filtra `deleted_at`, papéis ou visibilidade: uma nota privada de Alfa não se torna legível a Bob apenas porque ambos estão em Alfa.

**Autodescoberta deliberada:** uma tabela organizacional pode declarar, no spec congelado de `InstallRecordInfrastructure`, a chave `self_discovery_column` (a coluna do usuário dono da linha), e o modelo declara o mesmo campo em `self_discovery_field`. Para ela, SELECT permite `(T OR U)`, onde U significa “coluna igual ao usuário da GUC”; INSERT/UPDATE/DELETE só permitem T. As políticas se chamam `skeleton_self_select`, `skeleton_self_insert`, `skeleton_self_update` e `skeleton_self_delete`, e `check_skeleton_schema` reprova política faltante, extra ou alargada, e divergência entre o modelo e o spec. A leitura cruzada só acontece pelo helper explícito `own_rows(model, user)` de `skeleton.tenancy.context`, que filtra o usuário em Python (o SQLite não tem RLS) e exige que ele seja o ator do contexto; o manager normal permanece restrito ao tenant. `Membership` usa a chave desde `sk_tenancy.0003_membership_self_discovery`, com a mesma política efetiva de antes: `memberships_for_user(user)` projeta IDs/status/nome das organizações correspondentes. Alice pode assim descobrir seus vínculos em Alfa e Beta antes de escolher uma organização, sem receber escrita entre organizações nem usar uma policy recursiva.

SELECT de AuditEvent pela conexão runtime limita-se ao tenant; eventos globais só quando `platform_admin=on`. INSERT é da role SQL do trigger; UPDATE/DELETE são proibidos. User/Profile/Organization/catálogo globais não têm RLS tenant e nunca são listas genéricas para membros. Uma role SQL é uma credencial/privilégio do banco; não é o modelo empresarial `Role`.

**Limite da proteção:** `set_config` é permitido a quem possui a credencial runtime. RLS protege contra filtro organizacional esquecido e fluxos não autorizados da aplicação; não contra credencial runtime maliciosa ou injeção SQL arbitrária que altere GUCs. DBA/superuser SQL/`BYPASSRLS` ignora FORCE. `is_superuser` Django não transforma a conexão em owner do banco. Proteja DSNs (endereços/credenciais de conexão), use SQL parametrizado e proteja as superfícies HTTP. SQLite não tem RLS.

## Auditoria autorizada

Administradores organizacionais veem auditoria da própria organização. Outros precisam de `table.audit` e leitura autorizada do registro atual; uma marca de exclusão exige autorização para `restore`. Filtre **antes** de paginar/contar. Se o registro foi removido offline e não há objeto atual, só administrador organizacional/plataforma pode ver os eventos: nunca reconstrua a ACL a partir do conteúdo histórico. Inventário técnico e eventos globais pertencem somente à plataforma.
