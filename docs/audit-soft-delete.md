# Auditoria, concorrência e exclusão lógica

Três mecanismos diferentes trabalham juntos: **revisão** evita sobrescrever uma edição concorrente; **exclusão lógica** retira o objeto das consultas comuns sem apagar a linha; **auditoria** registra o que efetivamente mudou. Nenhum substitui autorização, backup ou política de retenção. Veja [auditoria e concorrência](concepts.md#auditoria-e-concorrência) e [autorização e contexto](concepts.md#autorização-e-contexto).

## Uma fonte de eventos

Um **trigger** é uma rotina executada pelo banco quando uma linha muda. A auditoria de entidades próprias usa triggers `AFTER ROW INSERT/UPDATE/DELETE`, isto é, após a operação e para cada linha, não signals de `save`. Um evento corresponde a uma mudança efetiva. Isso cobre `save`, `queryset.update`, `bulk_create`, associações com UUID, SQL direto, exclusão lógica e restauração. UPDATE sem alteração efetiva não gera evento. AuditEvent não audita a si mesmo.

O evento e a mudança pertencem à mesma transação: se a inserção na auditoria falhar, a escrita de negócio também falha. Desfazer a transação (`rollback`) ou um savepoint também remove os eventos correspondentes. Resposta de handler >=400 marca rollback. Logs de segurança enviados a stdout podem permanecer após rollback, mas sem cópias de segredos.

`operation` é `create`, `update`, `soft_delete`, `restore` ou `hard_delete`, conforme a transição de `deleted_at`. Exclusão física (`hard_delete`) é bloqueada para a conexão da aplicação (runtime); não existe endpoint de expurgo (`purge`). Somente manutenção offline revisada que remova a proteção no banco pode produzi-la.

## Payload e redação

O **payload** é o conteúdo do evento. Em `changes`, cada campo tem `{old, new, redacted}`: valor anterior, novo valor e indicação de segredo ocultado. Create tem `old=null`; exclusão física tem `new=null`; update contém só campos realmente alterados. Identidade, datas e revisão têm metadados próprios e não precisam repetir alterações mecânicas no conteúdo. UUIDs na API usam hífens.

**Redação**, aqui, significa ocultar valores sensíveis, não escrever uma descrição. Password, token/token_digest, secret/private_key, códigos MFA e campos marcados `sensitive` viram `[REDACTED]` **antes de persistir**. O banco compara os valores reais antes de ocultá-los: mesmo que old/new apareçam igualmente redigidos, uma mudança de senha continua registrada. JSON/claims sensíveis são ocultados integralmente. Um novo campo sensível exige atualização da spec e da migration; não confie apenas em heurística de nome.

### Dados pessoais

`TableSpec.personal_fields` marca dados pessoais, como o email e os nomes de `User` e o `display_name` de `UserProfile` ([#189](https://github.com/alaorneto/skeleton/issues/189)). Esses campos continuam em `read_fields`, mas os triggers os gravam como `[REDACTED]`, com `redacted: true`. Assim a auditoria nunca guarda a PII, cita a pessoa só pelo sysid e não impede a anonimização da identidade. A declaração é congelada na especificação da infraestrutura, e uma divergência entre o spec e a migration é E032.

Os eventos gravados antes da declaração continuam com os valores. `manage.py redact_audit_personal_data --confirm` os redige uma vez, e é a única exceção à regra append-only:

- roda só com o owner da tabela de auditoria (`MIGRATION_DATABASE_URL` ou `--database-url`); a role runtime é recusada;
- numa transação, desliga o guard, reescreve só `old`/`new`/`redacted` das colunas pessoais e religa o guard;
- grava o evento de segurança `audit.personal_data_redacted` com a contagem.

Faça backup antes: a reescrita não tem volta.

No PostgreSQL, actor/request/source vêm dos parâmetros locais do contexto (GUCs); `db_principal` é `session_user`, a identidade SQL da conexão. Tabela, organização e registro vêm de OLD/NEW do banco, nunca da entrada do cliente. Sem source, registra-se `sql:unattributed`, sem inventar usuário.

O escritor de auditoria é um trigger `SECURITY DEFINER`: executa com os privilégios de seu proprietário, tem `search_path` fixo e não recebe escrita em negócio. INSERT de auditoria organizacional deve satisfazer a policy; o audit writer não tem `BYPASSRLS`. Isso não é o papel empresarial de administrador.

## Metadados e CAS

**Compare-and-swap (CAS)** significa “grave somente se a revisão ainda for aquela que eu li”. Alice e Bob abrem uma nota de Alfa na revisão 4. Alice salva e a revisão vira 5; Bob tenta salvar a revisão 4 e recebe `ConcurrentUpdate`, em vez de apagar a edição de Alice. Ele precisa reler e decidir como conciliar. Uma nota de Beta com revisão 4 é outra linha: a comparação usa identificador e revisão juntos. Não retente silenciosamente com a revisão nova.

`SystemRecord.save` valida invariantes, a imutabilidade de `sysid`/`organization` e compara chave primária/revisão original. Ao atualizar, incrementa revisão e preenche datas/ator. `_state.adding=True` não sobrescreve uma linha já existente. `update_fields` grava apenas os campos pedidos mais os metadados; vazio não escreve. Revisão desatualizada lança `ConcurrentUpdate`. Quando `save()`, `delete()` ou `restore()` falham, a instância em memória volta ao estado de antes da chamada (metadados no `save()`, todos os campos no `delete()`/`restore()`): corrigir o campo inválido e salvar de novo não vira um conflito falso, e uma exclusão que falhou não deixa a instância parecendo excluída (#273).

`QuerySet.update` preenche `updated_at`/`updated_by` e usa `F('revision')+1`; mudanças de `sysid`, organização e autoria de criação são proibidas. Esse incremento por lote não é, sozinho, uma comparação com a revisão que um cliente leu: endpoints continuam exigindo os serviços e o contrato de concorrência. `bulk_create` prepara metadados e valida cada objeto. `ignore_conflicts`/`update_conflicts` são rejeitados; `bulk_update` lança `UnsafeBulkMutation`. Não use operações em lote para contornar serviços/autorização.

SQL direto também precisa fornecer `revision=OLD.revision+1`, datas e valores coerentes. Proteções no banco (guards), independentes do modelo Python, bloqueiam revisão incorreta, mudança de `sysid`/organização e DELETE físico. Chaves estrangeiras compostas no PostgreSQL bloqueiam relações entre organizações em INSERT/UPDATE.

## Soft delete e restore

**Soft delete**, ou exclusão lógica, preenche `deleted_at` em vez de apagar a linha. A marca é chamada **tombstone**: `deleted_at IS NOT NULL` é sua única definição, sem `is_deleted` redundante. O manager padrão exclui essas linhas das consultas e aplica a organização. Sem contexto organizacional, lança `MissingTenantContext`. `with_deleted()` remove **apenas** o filtro da marca de exclusão posto pelo manager, em qualquer profundidade e também em querysets combinados com `|` e `&` (o filtro é um lookup próprio, `LiveRows`, reconhecido pelo tipo; um `deleted_at__isnull=True` escrito por quem chama fica): não abre Beta para quem está em Alfa nem dispensa autorização (#274). O base manager interno não filtrado serve só à infraestrutura, nunca a serializer, seletor de referências ou template.

Delete de instância/queryset é lógico e não usa coleta/cascata de exclusão física do Django. O lote usa um único `atomic`, com triggers por linha. Restore revalida referências ativas e unicidade, preservando `sysid`; um conflito não restaura parcialmente nem sobrescreve outra linha.

Por exemplo, Alice exclui em Alfa a nota de código `ATA-01`. Se outra nota viva ocupar esse código em Alfa, restaurar a antiga falha; a antiga conserva sua marca e UUID. Uma nota `ATA-01` de Beta não conflita quando a unicidade é por organização. Restaurar tampouco reativa automaticamente todos os vínculos de acesso revogados.

As políticas de dependência são explícitas:

- **`protect` (padrão):** dependente próprio vivo causa `LiveReferencesExist`; revogue/remova os dependentes explicitamente antes.
- **`retain` em User/Organization:** conserva identidade, afiliação histórica e dados empresariais. O serviço de ciclo de vida desativa acesso antes da marca de exclusão, sem cascata. Restore de User deixa `is_active=False` até reativação de plataforma.
- **Membership:** o serviço revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo no mesmo `atomic`, protege o último administrador e só então remove o vínculo. A saída voluntária (`leave_organization`) é a mesma exclusão lógica da remoção pelo administrador: o UUID, a revisão e o histórico ficam, os eventos são de quem saiu e um convite aceito depois restaura o mesmo vínculo.
- **Role/grupo:** referências vivas exigem revogação prévia; papéis internos (`builtins`) são protegidos.
- **Sessões:** desativar/excluir User ou revogar globalmente aumenta `auth_version`; sessões antigas são encerradas. Email e identificadores de identidade externa (`subjects`) não são reutilizados.

Bibliotecas técnicas mantêm seu ciclo de vida: sessões expiram ou são apagadas tecnicamente; allauth gerencia dados de protocolo. Não aplique exclusão lógica genérica nem auditoria do conteúdo nessas tabelas; o manifesto explica as exceções.

## Leitura e retenção

### Tabela de retenções

Todo apagamento físico é um job declarado em código, por organização quando a tabela é organizacional, com o prazo num setting do catálogo da instância. Os registros de negócio nunca são apagados: o soft delete os mantém como tombstone.

| O quê | Job | Setting (padrão) |
|---|---|---|
| Eventos de domínio já entregues | `events.retention` | `SKELETON_EVENT_RETENTION_DAYS` (30) |
| Jobs concluídos ou cancelados / esgotados | `jobs.retention` | `SKELETON_JOB_SUCCEEDED_RETENTION_DAYS` (7) / `SKELETON_JOB_DEAD_RETENTION_DAYS` (30) |
| Entregas de webhook | `webhooks.retention` | `SKELETON_WEBHOOK_RETENTION_DAYS` (30) |
| Notificações lidas, não lidas e entregas | `notifications.retention` | `SKELETON_NOTIFICATION_*_RETENTION_DAYS` |
| Arquivos de exportação e importação CSV | `transfer.retention` | `SKELETON_TRANSFER_RETENTION_DAYS` (7) |
| Objetos sem linha, deixados por rollback | `files.orphans` | `SKELETON_FILES_ORPHAN_GRACE_SECONDS` (1 hora) |
| Bytes de anexos excluídos | `files.purge` | `SKELETON_FILES_DELETED_RETENTION_DAYS` (90) |
| Auditoria | nenhum: append-only; só os dados pessoais antigos são redigidos uma vez (`redact_audit_personal_data`) | — |

A auditoria é somente leitura. Administradores organizacionais só veem sua organização; outros usuários precisam de `table.audit` **e** leitura autorizada do registro atual (se excluído, restauração autorizada). Filtre antes de contar/paginar. Eventos globais e inventário técnico são exclusivos da plataforma; registros apagados fisicamente offline só são visíveis a administrador organizacional/plataforma.

Na API, HTML e admin, a apresentação da auditoria de negócio segue `TableSpec.read_fields`, inclusive nomes físicos de FKs. Um campo omitido da leitura comum não reaparece no histórico; a captura persistida continua íntegra e redigida. Eventos administrativos do núcleo são uma superfície privilegiada separada, com segredos ocultados. Uma spec desconhecida não apresenta `changes`. Ter audit-sem-read em uma tabela exclui apenas essa tabela inelegível, sem abortar os eventos permitidos de outras tabelas.

**Append-only** significa que a conexão runtime só acrescenta eventos, não os altera ou exclui. Não significa **WORM** (armazenamento que impede reescrita): DBA/migration owner pode alterar o schema; quem controla o servidor PostgreSQL pode editar ou remover dados. Retenção, backup, criptografia e credenciais privilegiadas exigem proteção operacional. Não prometa imutabilidade contra esses atores.

### Filtros de investigação

As três superfícies aceitam os mesmos filtros ([#184](https://github.com/alaorneto/skeleton/issues/184)): a auditoria da organização (`/o/<org>/audit/`), a da plataforma (`/platform/audit/`) e a REST (`/api/v1/o/<org>/audit/`).

- `from` e `to`: uma data, que vale o dia inteiro no fuso da pessoa, ou data e hora ISO 8601.
- `actor` (UUID), `operation` (`create`, `update`, `soft_delete`, `restore`, `hard_delete`) e `request_id`.
- Os filtros que já existiam, `model_label` e `record_sysid`.

O parser comum é `skeleton.audit.filters.parse_audit_filters`. Um filtro desconhecido ou inválido responde 400. Os filtros só estreitam o que `authorized_audit_queryset` já permite: um `request_id` de outra organização não revela nada. Os índices `sk_audit_org_actor` (organização, ator, data) e `sk_audit_request` sustentam as buscas.

Com `request_id`, o administrador da organização vê também os eventos de domínio da mesma requisição que ainda estão retidos, com os identificadores e nunca o `data`.

### Exportação CSV

O administrador da organização exporta o resultado filtrado da auditoria em `/o/<org>/audit/` com **Exportar CSV**, que leva os filtros da tela ([#186](https://github.com/alaorneto/skeleton/issues/186)).

- **Como roda:** pelo pipeline de [exportação e importação](data-transfer.md). O job lê pelo `AuditLogService.events` como quem pediu, então as linhas e a redação são as mesmas da tela, e `changes` sai como JSON com os valores sensíveis em `[REDACTED]`.
- **Limite:** `SKELETON_EXPORT_MAX_ROWS`.
- **Download:** só o próprio administrador baixa o arquivo.
- **Fora:** a auditoria global da plataforma, porque toda transferência pertence a uma organização.
