# Controle de acesso e contexto organizacional

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

1. Organização ativa + usuário ativo/não deletado + Membership ativa/viva.
2. Roles efetivos: MemberRole vivos e GroupRole vivos dos grupos com GroupMember vivo. Sem cache de autorização entre requests.
3. TablePermission da ação é obrigatória. Grant não a substitui. Default deny; `member` não ganha permissões automaticamente.
4. Para leitura: owner=ator **ou** visibility=organization **ou** visibility=group e membro do owning_group **ou** RecordGrant read para membership/grupo/role efetivo.
5. Para update/delete: owner=ator **ou** RecordGrant específico da ação. Visibilidade pública na org/grupo não dá escrita.
6. Restore: permissão restore e ownership/admin; leitura de tombstone somente conforme autorização de restore.
7. Organization_admin builtin tem concessões reconciliadas para tabelas elegíveis e override explícito de visibilidade **só na própria organização**. Superuser global não pula Membership/role nas rotas tenant.

Exemplos: Alice com table.read vê sua nota privada; Bob com table.read não a vê. `visibility=organization` permite Bob ler, não editar. Um grant update de Bob sem table.update continua negado. Membro de grupo com role não ganha poderes em outro tenant. Revogar MemberRole/GroupMember/grant perde efeito na próxima request.

Criar requer table.create; owner inicial é ator e visibility private. Owner/visibility/owning_group e ACL exigem manage_access; nunca aceitos pelo PATCH comum. Gestão do acesso exige ainda ownership ou org admin. O servidor valida owner ativo e grupo vivo da mesma organização.

## Administração de acesso

Serviços em `skeleton.access.services` administram Membership, roles, grupos, links, table permissions e record grants exclusivamente por org admin. TablePermission só aponta a tabela empresarial exposta/ação suportada, não identidade global/técnica. RecordGrant resolve alvo SecuredRecord vivo, mesma organização e sujeito ativo; UUID do cliente não basta.

Mudanças que podem remover último administrador travam Organization dentro de atomic: desativação/remoção de Membership, MemberRole, GroupRole e relações de grupo. Conflito deixa tudo inalterado. Remover Membership revoga MemberRole, GroupMember e RecordGrant do vínculo antes do tombstone. Papéis/grupos com referências vivas exigem revogação explícita prévia; os builtins são protegidos.

POST de Membership existente usa a mesma transição de desativação que PATCH: revoga MemberRole/GroupMember/RecordGrant e conserva o guard de último admin. Reativar não ressuscita esses links. Restore persiste is_active escolhido pelo serviço no único CAS de restauração. Um vínculo inativo vivo pode ser removido pelo lifecycle normal; tenant/ACL/último admin continuam validados.


Desativar ou excluir logicamente User global também verifica todas as organizações em que ele é membro ativo, não apenas o tenant atual. No PostgreSQL, `skeleton.assert_user_deactivation(uuid)` é uma função SECURITY DEFINER restrita, de ownership do migrator: só verifica administradores e mantém locks nas organizações; não escreve negócio nem concede um contexto tenant geral. EXECUTE público é revogado; runtime exige o plano de plataforma autenticado, e operações offline usam owner. `system_context(reason=...)` com uma conexão runtime comum não transforma essa credencial em owner. A violação de integridade é traduzida para `LastAdministrator`; o admin global responde 409 com rollback, não erro SQL/500.

A confirmação de exclusão de User no Django Admin descreve somente a identidade: não usa o collector físico do Django. Perfis, Memberships e subjects permanecem históricos, e o lifecycle invalida login/sessões. Não habilite ações de hard delete ou exclusão física de dependentes.

## Contrato de contextos

`request_context(*, actor, request_id, source, using='default')` abre transação externa. `tenant_context(*, organization_id, actor, source, using='default')` valida vínculo/tenant ativos. `system_context(*, reason, organization_id=None, using='default')` exige motivo e é só infraestrutura/CLI.

Um request global pode entrar num tenant validado e voltar a global. Não pode selecionar tenant diferente na mesma transação. Blocos aninhados reutilizam identidade/tenant por savepoints; ator só muda no hook de login/logout após estágios allauth completos. Trabalho multitenant abre transações independentes por org. Reset em finally; somente `SET LOCAL`; contexto não sobrevive COMMIT/ROLLBACK.

```python
for org_id in materialized_organization_ids:
    with system_context(reason="job:reconcile-permissions", organization_id=org_id):
        # Uma organização por transação, APIs internas apropriadas.
        reconcile_one(org_id)
```

O exemplo acima ilustra o padrão de contexto, não concede permissão HTTP nem BYPASSRLS. Não envolva todo o loop numa atomic externa. `on_commit` recebe dados materializados; consultas posteriores precisam de novo contexto. Não retorne queryset lazy para avaliação fora do bloco.

## RLS PostgreSQL

GUCs locais: `skeleton.user_id`, `skeleton.tenant_id`, `skeleton.request_id`, `skeleton.source`, `skeleton.platform_admin`. Ator vem da sessão e request UUID do servidor. Tenant vem da URL validada, não header/body. Valores vazios não veem linhas; UUID inválido gera erro, não fallback.

Tabelas organizacionais usam ENABLE + FORCE ROW LEVEL SECURITY e policy runtime `USING T / WITH CHECK T`, onde T é organização igual ao tenant da GUC. RLS não filtra deleted_at nem papéis/visibilidade.

**Membership discovery deliberada:** SELECT permite `(T OR U)` sendo U=user_id da GUC. INSERT/UPDATE/DELETE só T. `memberships_for_user(user)` filtra explicitamente usuário e projeta IDs/status/nome das organizações correspondentes; manager normal segue tenant-scoped. Isso permite listar os próprios vínculos antes de selecionar org sem abrir escrita cross-tenant nem policy recursiva.

AuditEvent SELECT runtime limita ao tenant; eventos globais só quando platform_admin=on. INSERT é da role de trigger; UPDATE/DELETE são proibidos. Global User/Profile/Organization/catálogo não têm RLS tenant e nunca são listas genéricas para membros.

**Threat boundary:** `set_config` é permitido a uma conexão que conhece a credencial runtime. RLS protege filtro tenant esquecido e fluxos não autorizados da aplicação; não protege contra credencial runtime maliciosa ou SQL injection arbitrária que altere GUCs. DBA/superuser/BYPASSRLS ignora FORCE. `is_superuser` Django não transforma conexão em owner. Proteja DSNs, SQL parametrizado e superfícies HTTP. SQLite não tem RLS.

## Auditoria autorizada

Org admin vê audit do próprio tenant. Outros precisam table.audit e leitura autorizada do registro atual; tombstone exige autorização para restore. Filtre **antes** de paginar/contar. Registro removido offline sem objeto atual só é visível a org admin/plataforma, nunca reconstruindo ACL pelo payload histórico. Inventário técnico e eventos globais são só da plataforma.
