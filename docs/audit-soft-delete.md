# Auditoria, concorrência e exclusão lógica

Três mecanismos diferentes trabalham juntos: **revisão** evita sobrescrever uma edição concorrente; **exclusão lógica** retira o objeto das consultas comuns sem apagar a linha; **auditoria** registra o que efetivamente mudou. Nenhum substitui autorização, backup ou política de retenção. Veja [auditoria e concorrência](concepts.md#auditoria-e-concorrência) e [autorização e contexto](concepts.md#autorização-e-contexto).

## Uma fonte de eventos

Um **trigger** é uma rotina executada pelo banco quando uma linha muda. A auditoria de entidades próprias usa triggers `AFTER ROW INSERT/UPDATE/DELETE`, isto é, após a operação e para cada linha, não signals de `save`. Um evento corresponde a uma mudança efetiva. Isso cobre `save`, `queryset.update`, `bulk_create`, associações com UUID, SQL direto, exclusão lógica e restauração. UPDATE sem alteração efetiva não gera evento. AuditEvent não audita a si mesmo.

O evento e a mudança pertencem à mesma transação: se a inserção na auditoria falhar, a escrita de negócio também falha. Desfazer a transação (`rollback`) ou um savepoint também remove os eventos correspondentes. Resposta de handler >=400 marca rollback. Logs de segurança enviados a stdout podem permanecer após rollback, mas sem cópias de segredos.

`operation` é `create`, `update`, `soft_delete`, `restore` ou `hard_delete`, conforme a transição de `deleted_at`. Exclusão física (`hard_delete`) é bloqueada para a conexão da aplicação (runtime); não existe endpoint de expurgo (`purge`). Somente manutenção offline revisada que remova a proteção no banco pode produzi-la.

## Payload e redação

O **payload** é o conteúdo do evento. Em `changes`, cada campo tem `{old, new, redacted}`: valor anterior, novo valor e indicação de segredo ocultado. Create tem `old=null`; exclusão física tem `new=null`; update contém só campos realmente alterados. Identidade, datas e revisão têm metadados próprios e não precisam repetir alterações mecânicas no conteúdo. UUIDs na API usam hífens.

**Redação**, aqui, significa ocultar valores sensíveis, não escrever uma descrição. Password, token/token_digest, secret/private_key, códigos MFA e campos marcados `sensitive` viram `[REDACTED]` **antes de persistir**. O banco compara os valores reais antes de ocultá-los: mesmo que old/new apareçam igualmente redigidos, uma mudança de senha continua registrada. JSON/claims sensíveis são ocultados integralmente. Um novo campo sensível exige atualização da spec e da migration; não confie apenas em heurística de nome.

No PostgreSQL, actor/request/source vêm dos parâmetros locais do contexto (GUCs); `db_principal` é `session_user`, a identidade SQL da conexão. Tabela, organização e registro vêm de OLD/NEW do banco, nunca da entrada do cliente. Sem source, registra-se `sql:unattributed`, sem inventar usuário.

O escritor de auditoria é um trigger `SECURITY DEFINER`: executa com os privilégios de seu proprietário, tem `search_path` fixo e não recebe escrita em negócio. INSERT de auditoria organizacional deve satisfazer a policy; o audit writer não tem `BYPASSRLS`. Isso não é o papel empresarial de administrador.

No SQLite, `connection_created` registra as funções de conexão (UDFs) `skeleton_actor`, `skeleton_request_id`, `skeleton_source`, `skeleton_uuid`, `skeleton_now`; UUID retornado respeita o armazenamento hexadecimal. Os triggers usam JSON SQLite. Um cliente externo sem essas funções falha ao escrever, em vez de pular a auditoria. Não há alteração de schema (DDL) no startup nem variável global mutável para controlar recursão.

## Metadados e CAS

**Compare-and-swap (CAS)** significa “grave somente se a revisão ainda for aquela que eu li”. Alice e Bob abrem uma nota de Alfa na revisão 4. Alice salva e a revisão vira 5; Bob tenta salvar a revisão 4 e recebe `ConcurrentUpdate`, em vez de apagar a edição de Alice. Ele precisa reler e decidir como conciliar. Uma nota de Beta com revisão 4 é outra linha: a comparação usa identificador e revisão juntos. Não retente silenciosamente com a revisão nova.

`SystemRecord.save` valida invariantes, a imutabilidade de `sysid`/`organization` e compara chave primária/revisão original. Ao atualizar, incrementa revisão e preenche datas/ator. `_state.adding=True` não sobrescreve uma linha já existente. `update_fields` grava apenas os campos pedidos mais os metadados; vazio não escreve. Revisão desatualizada lança `ConcurrentUpdate`.

`QuerySet.update` preenche `updated_at`/`updated_by` e usa `F('revision')+1`; mudanças de `sysid`, organização e autoria de criação são proibidas. Esse incremento por lote não é, sozinho, uma comparação com a revisão que um cliente leu: endpoints continuam exigindo os serviços e o contrato de concorrência. `bulk_create` prepara metadados e valida cada objeto. `ignore_conflicts`/`update_conflicts` são rejeitados; `bulk_update` lança `UnsafeBulkMutation`. Não use operações em lote para contornar serviços/autorização.

SQL direto também precisa fornecer `revision=OLD.revision+1`, datas e valores coerentes. Proteções no banco (guards), independentes do modelo Python, bloqueiam revisão incorreta, mudança de `sysid`/organização e DELETE físico. Chaves estrangeiras compostas no PostgreSQL e guards no SQLite bloqueiam relações entre organizações em INSERT/UPDATE.

## Soft delete e restore

**Soft delete**, ou exclusão lógica, preenche `deleted_at` em vez de apagar a linha. A marca é chamada **tombstone**: `deleted_at IS NOT NULL` é sua única definição, sem `is_deleted` redundante. O manager padrão exclui essas linhas das consultas e aplica a organização. Sem contexto organizacional, lança `MissingTenantContext`. `with_deleted()` remove **apenas** o filtro da marca de exclusão: não abre Beta para quem está em Alfa nem dispensa autorização. O base manager interno não filtrado serve só à infraestrutura, nunca a serializer, seletor de referências ou template.

Delete de instância/queryset é lógico e não usa coleta/cascata de exclusão física do Django. O lote usa um único `atomic`, com triggers por linha. Restore revalida referências ativas e unicidade, preservando `sysid`; um conflito não restaura parcialmente nem sobrescreve outra linha.

Por exemplo, Alice exclui em Alfa a nota de código `ATA-01`. Se outra nota viva ocupar esse código em Alfa, restaurar a antiga falha; a antiga conserva sua marca e UUID. Uma nota `ATA-01` de Beta não conflita quando a unicidade é por organização. Restaurar tampouco reativa automaticamente todos os vínculos de acesso revogados.

As políticas de dependência são explícitas:

- **`protect` (padrão):** dependente próprio vivo causa `LiveReferencesExist`; revogue/remova os dependentes explicitamente antes.
- **`retain` em User/Organization:** conserva identidade, afiliação histórica e dados empresariais. O serviço de ciclo de vida desativa acesso antes da marca de exclusão, sem cascata. Restore de User deixa `is_active=False` até reativação de plataforma.
- **Membership:** o serviço revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo no mesmo `atomic`, protege o último administrador e só então remove o vínculo.
- **Role/grupo:** referências vivas exigem revogação prévia; papéis internos (`builtins`) são protegidos.
- **Sessões:** desativar/excluir User ou revogar globalmente aumenta `auth_version`; sessões antigas são encerradas. Email e identificadores de identidade externa (`subjects`) não são reutilizados.

Bibliotecas técnicas mantêm seu ciclo de vida: sessões expiram ou são apagadas tecnicamente; allauth gerencia dados de protocolo. Não aplique exclusão lógica genérica nem auditoria do conteúdo nessas tabelas; o manifesto explica as exceções.

## Leitura e retenção

A auditoria é somente leitura. Administradores organizacionais só veem sua organização; outros usuários precisam de `table.audit` **e** leitura autorizada do registro atual (se excluído, restauração autorizada). Filtre antes de contar/paginar. Eventos globais e inventário técnico são exclusivos da plataforma; registros apagados fisicamente offline só são visíveis a administrador organizacional/plataforma.

Na API, HTML e admin, a apresentação da auditoria de negócio segue `TableSpec.read_fields`, inclusive nomes físicos de FKs. Um campo omitido da leitura comum não reaparece no histórico; a captura persistida continua íntegra e redigida. Eventos administrativos do núcleo são uma superfície privilegiada separada, com segredos ocultados. Uma spec desconhecida não apresenta `changes`. Ter audit-sem-read em uma tabela exclui apenas essa tabela inelegível, sem abortar os eventos permitidos de outras tabelas.

**Append-only** significa que a conexão runtime só acrescenta eventos, não os altera ou exclui. Não significa **WORM** (armazenamento que impede reescrita): DBA/migration owner pode alterar o schema; o dono do arquivo SQLite pode editá-lo ou removê-lo. Retenção, backup, criptografia e credenciais privilegiadas exigem proteção operacional. Não prometa imutabilidade contra esses atores.
