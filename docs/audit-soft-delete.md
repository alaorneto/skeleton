# Auditoria, concorrência e exclusão lógica

## Uma fonte de eventos

Auditoria de entidades próprias é feita por triggers AFTER ROW INSERT/UPDATE/DELETE, não signals de save. Um evento corresponde a uma mudança efetiva. Ela cobre save, queryset.update, bulk_create, links UUID, SQL direto, soft delete e restore. UPDATE sem alteração efetiva não gera evento. AuditEvent não audita a si mesmo.

Evento participa da mesma transação: se inserir no audit falhar, a escrita de negócio falha; rollback/savepoint também remove evento. Resposta de handler >=400 marca rollback. Logs de segurança stdout podem permanecer após rollback, sem snapshots secretos.

`operation` é create/update/soft_delete/restore/hard_delete conforme a transição de deleted_at. Hard delete é bloqueado para runtime e não existe como endpoint/purge; somente maintenance offline revisada que remova guard pode produzi-lo.

## Payload e redação

`changes` é objeto por campo `{old, new, redacted}`. Create tem old=null, delete new=null; update só campos realmente alterados. Identidade, timestamps e revisão têm metadata própria e não precisam duplicar alterações mecânicas no payload. UUIDs na API usam hífens.

Password, token/token_digest, secret/private_key, códigos MFA e campos marcados `sensitive` são `[REDACTED]` **antes de persistir**. Comparação usa valores reais antes da redação: old/new redigidos iguais não apagam evidência de alteração de senha. JSON/claims sensíveis são redigidos integralmente. Um campo novo sensível precisa de spec/migration atualizados; não confie em heurística de nome como única defesa.

PostgreSQL obtém actor/request/source das GUCs; db_principal é session_user; tabela/tenant/registro de OLD/NEW, nunca input. Sem source: `sql:unattributed`, sem inventar usuário. Trigger writer SECURITY DEFINER tem search_path fixo e não recebe escrita em negócio. Audit INSERT tenant deve satisfazer policy; audit writer não tem BYPASSRLS.

SQLite registra UDFs `skeleton_actor`, `skeleton_request_id`, `skeleton_source`, `skeleton_uuid`, `skeleton_now` em connection_created; UUID retornado respeita armazenamento hex. Triggers usam JSON SQLite. Cliente externo sem funções falha ao escrever, não pula auditoria. Não existe DDL de startup ou recursion guard global mutável.

## Metadados e CAS

`SystemRecord.save` valida invariantes, imutabilidade de sysid/organization e usa compare-and-swap por PK/revisão original. Em update incrementa revisão e timestamps/ator. `_state.adding=True` não sobrescreve linha já existente. `update_fields` preserva só campos pedidos + metadata; vazio não escreve. Stale revision lança `ConcurrentUpdate`.

`QuerySet.update` preenche updated_at/updated_by e usa `F('revision')+1`; sysid/organização/autoria de criação são proibidos. `bulk_create` prepara metadata e valida cada objeto. `ignore_conflicts`/`update_conflicts` são rejeitados; `bulk_update` lança `UnsafeBulkMutation`. Não use bulk APIs para contornar serviços/autorização.

SQL direto também precisa fornecer revision=OLD.revision+1, timestamps e valores coerentes. Guards independentes do modelo bloqueiam revisão incorreta, mudança sysid/tenant e DELETE físico. FK composta PostgreSQL/guards SQLite bloqueiam relação entre organizações em INSERT/UPDATE.

## Soft delete e restore

`deleted_at IS NOT NULL` é a única definição de tombstone; não há is_deleted redundante. Manager padrão remove tombstones e aplica tenant. Sem contexto organizacional: `MissingTenantContext`. `with_deleted()` remove apenas tombstone; não abre outras organizações. Base manager interno não filtrado é só infraestrutura, nunca serializer/picker/template.

Delete de instância/queryset é lógico, sem collector/cascade físico. Lote usa atomic único e triggers por linha. Restore revalida referências ativas e unicidade, preservando sysid; conflito não ressuscita nem sobrescreve outra linha.

- Política padrão `protect`: dependente próprio vivo causa `LiveReferencesExist`; revogue/remova dependentes explicitamente.
- User/Organization usam `retain`: preservam identidade, afiliação histórica e dados empresariais. Lifecycle desativa acesso antes de tombstone, sem cascata. Restore de User deixa is_active=False até reativação de plataforma.
- Membership: serviço revoga MemberRole, GroupMember e RecordGrant do vínculo no mesmo atomic, preserva último admin e então remove vínculo.
- Role/grupo: referências vivas exigem revogação prévia; builtins são protegidos.
- User desativado/deletado ou revogação global aumenta auth_version; sessões antigas são encerradas. Email e subjects não são reutilizados.

Bibliotecas técnicas mantêm lifecycle próprio: sessão expira/delete técnico, allauth gerencia dados de protocolo. Não aplique generic soft delete/audit de payload nessas tabelas; manifesto explica exceções.

## Leitura e retenção

Audit é read-only. Org admin só vê sua org; demais usuários precisam audit de tabela + leitura do registro atual (tombstone com restore autorizado). Não conte/pagine antes de filtrar. Eventos globais e inventário técnico só plataforma; registro hard-deleted offline só org admin/plataforma.

A representação de audit de negócio (API, HTML e admin) segue `TableSpec.read_fields`, inclusive nomes físicos de FKs. Um campo omitido da leitura comum não reaparece no histórico; a captura persistida continua íntegra e redigida. Eventos administrativos do núcleo permanecem uma superfície privilegiada com campos sensíveis redigidos. Spec desconhecida não apresenta changes. Permissões mistas audit-sem-read excluem apenas a tabela inelegível, sem abortar os eventos permitidos de outras tabelas.


Append-only do runtime **não significa WORM**. DBA/migration owner pode alterar schema; dono do arquivo SQLite pode removê-lo/editá-lo. Retenção, backup, criptografia e credenciais privilegiadas precisam de proteção operacional. Não ofereça promessa de imutabilidade contra esses atores.
