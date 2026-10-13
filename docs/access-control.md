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

### Exemplo com Alfa e Beta

Alice e Bob têm vínculo ativo e `table.read` em Alfa. Alice cria uma nota privada: ela consegue ler; Bob não. Ao mudar a visibilidade para `organization`, Bob passa a ler, mas não a editar. Mesmo um `RecordGrant update` para Bob não permite editar sem `table.update`.

Em Beta, Alice pode ter outro papel e Bob pode nem ter vínculo. Nada do que foi concedido em Alfa abre a nota em Beta. Um grupo Financeiro em cada organização também são dois grupos distintos. Revogar `MemberRole`, `GroupMember` ou uma concessão retira seu efeito na próxima requisição.

Criar exige `table.create`; o proprietário inicial é o ator e a visibilidade é `private`. Alterar `owner`, `visibility`, `owning_group` ou a lista de concessões (ACL, lista de controle de acesso) exige `manage_access` **e** condição de proprietário ou administrador da organização. Esses campos nunca são aceitos pelo PATCH comum. O servidor valida proprietário ativo e grupo vivo da mesma organização.

## Administração de acesso

Os serviços em `skeleton.access.services` administram vínculos, papéis, grupos, associações, permissões de tabela e concessões de registro exclusivamente por administrador organizacional. `TablePermission` só aponta a tabela de negócio exposta e ação suportada, nunca identidade global ou tabela técnica. `RecordGrant` resolve um alvo `SecuredRecord` vivo, da mesma organização, com destinatário ativo; o UUID enviado pelo cliente não basta.

Mudanças que podem remover o último administrador bloqueiam `Organization` dentro de `atomic`: desativação/remoção de `Membership`, `MemberRole`, `GroupRole` e relações de grupo. Assim, operações concorrentes não aprovam duas remoções com base no mesmo estado antigo. Conflito deixa tudo inalterado. Remover `Membership` revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo antes da marca de exclusão. Papéis/grupos com referências vivas exigem revogação explícita prévia; papéis internos são protegidos.

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

Um bloqueio de linha (`SELECT ... FOR UPDATE`) impede outra transação de modificar aquela linha até o fim da transação atual. Para alterações administrativas, adquira primeiro o bloqueio de `Organization` com `skeleton.access.services.organization_lock(user)` e só depois carregue/bloqueie o vínculo, papel, grupo ou link a alterar. HTTP sempre passa o ator autenticado; a ausência de ator é reservada ao provisionamento interno.

A API dedicada e os POSTs administrativos HTML seguem essa ordem. Os serviços reutilizam o mesmo bloqueio, e a revisão é comparada **depois** de adquirir os locks. Inverter a ordem — uma requisição segurando o vínculo e esperando a organização, outra segurando a organização e esperando o vínculo — causa deadlock. O banco abortar uma transação não é uma solução de concorrência; não há retry automático para esconder esse erro.


## RLS PostgreSQL

**Row-Level Security (RLS), ou segurança por linha**, é uma proteção do banco que restringe quais linhas a conexão pode ler ou escrever. Aqui ela verifica a organização; os serviços Django continuam responsáveis por papéis, visibilidade, ações e exclusão lógica.

O contexto instala parâmetros locais da sessão PostgreSQL, chamados **GUCs**: `skeleton.user_id`, `skeleton.tenant_id`, `skeleton.request_id`, `skeleton.source`, `skeleton.platform_admin`. O ator vem da sessão autenticada e o UUID da requisição é gerado pelo servidor. A organização vem da URL validada, não de header/body. Valores vazios não veem linhas; UUID inválido gera erro, não um valor alternativo permissivo.

Tabelas organizacionais usam `ENABLE` + `FORCE ROW LEVEL SECURITY` e policy da conexão runtime (processo da aplicação) `USING T / WITH CHECK T`, onde T significa “organização da linha igual ao tenant da GUC”. `USING` limita linhas existentes; `WITH CHECK` verifica a organização da linha escrita. RLS **não** filtra `deleted_at`, papéis ou visibilidade: uma nota privada de Alfa não se torna legível a Bob apenas porque ambos estão em Alfa.

**Descoberta deliberada de Membership:** SELECT permite `(T OR U)`, onde U significa “user_id igual ao usuário da GUC”. INSERT/UPDATE/DELETE só permitem T. `memberships_for_user(user)` filtra explicitamente o usuário e projeta IDs/status/nome das organizações correspondentes; o manager normal permanece restrito ao tenant. Alice pode assim descobrir seus vínculos em Alfa e Beta antes de escolher uma organização, sem receber escrita entre organizações nem usar uma policy recursiva.

SELECT de AuditEvent pela conexão runtime limita-se ao tenant; eventos globais só quando `platform_admin=on`. INSERT é da role SQL do trigger; UPDATE/DELETE são proibidos. User/Profile/Organization/catálogo globais não têm RLS tenant e nunca são listas genéricas para membros. Uma role SQL é uma credencial/privilégio do banco; não é o modelo empresarial `Role`.

**Limite da proteção:** `set_config` é permitido a quem possui a credencial runtime. RLS protege contra filtro organizacional esquecido e fluxos não autorizados da aplicação; não contra credencial runtime maliciosa ou injeção SQL arbitrária que altere GUCs. DBA/superuser SQL/`BYPASSRLS` ignora FORCE. `is_superuser` Django não transforma a conexão em owner do banco. Proteja DSNs (endereços/credenciais de conexão), use SQL parametrizado e proteja as superfícies HTTP. SQLite não tem RLS.

## Auditoria autorizada

Administradores organizacionais veem auditoria da própria organização. Outros precisam de `table.audit` e leitura autorizada do registro atual; uma marca de exclusão exige autorização para `restore`. Filtre **antes** de paginar/contar. Se o registro foi removido offline e não há objeto atual, só administrador organizacional/plataforma pode ver os eventos: nunca reconstrua a ACL a partir do conteúdo histórico. Inventário técnico e eventos globais pertencem somente à plataforma.
