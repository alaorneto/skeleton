# Auditoria, concorrência e exclusão lógica

Três mecanismos complementares protegem a integridade dos dados: a **revisão** impede sobrescrever edições concorrentes; a **exclusão lógica** remove o objeto das consultas comuns sem apagar a linha do banco; e a **auditoria** registra o que efetivamente mudou. Nenhum deles substitui autorização, backup ou política de retenção. Para o contexto conceitual, consulte [auditoria e concorrência](concepts.md#auditoria-e-concorrência) e [autorização e contexto](concepts.md#autorização-e-contexto).

## Uma fonte de eventos

Um **trigger** é uma rotina executada diretamente pelo banco de dados quando uma linha sofre alteração. A auditoria das entidades próprias utiliza triggers `AFTER ROW INSERT`, `UPDATE` e `DELETE` — disparados após a operação e para cada linha — em vez de signals de `save`. Cada evento corresponde a uma mudança efetiva, cobrindo `save`, `queryset.update`, `bulk_create`, associações com UUID, SQL direto, exclusão lógica e restauração. Um `UPDATE` que não altera valores não gera evento, e `AuditEvent` não audita a si mesmo.

O evento de auditoria e a alteração de negócio pertencem à mesma transação: se a gravação da auditoria falhar, a escrita de negócio também falha. Desfazer a transação (`rollback`) ou um savepoint descarta os eventos correspondentes, e qualquer resposta de handler com status `>= 400` marca rollback. Logs de segurança enviados a `stdout` podem permanecer após um rollback, mas nunca contêm cópias de segredos.

O campo `operation` assume `create`, `update`, `soft_delete`, `restore` ou `hard_delete`, de acordo com a transição de `deleted_at`. A exclusão física (`hard_delete`) é bloqueada para a conexão da aplicação (runtime), e não existe endpoint de expurgo (`purge`). Apenas uma manutenção offline revisada que remova o guard do banco pode gerar um evento `hard_delete`.

## Payload e redação

O **payload** representa o conteúdo registrado pelo evento. Em `changes`, cada campo modificado aparece como `{old, new, redacted}`, indicando o valor anterior, o novo valor e se houve ocultação de segredo. Em `create`, `old` é `null`; em `hard_delete`, `new` é `null`; em `update`, entram apenas os campos realmente alterados. Além dos `sensitive_fields` declarados no `TableSpec`, a auditoria redige automaticamente campos com nomes de segredo (`password`, `token`, `token_digest`, `secret`, `private_key` e `recovery_codes`), e o check `E008` exige que esses campos, quando não relacionais, estejam declarados como sensíveis. Identidade, datas e revisão já constam nos metadados do evento e não repetem alterações mecânicas em `changes`. Na API, UUIDs são representados com hífens.

Aqui, **redação** significa ocultar valores sensíveis, substituindo-os por `[REDACTED]` **antes da gravação** — isso vale para senhas, `token`, `token_digest`, `secret`, `private_key`, códigos MFA e campos marcados como `sensitive`. O banco compara os valores reais antes de redigi-los: assim, mesmo que `old` e `new` apareçam ambos redigidos, uma troca de senha continua sendo registrada. Campos JSON e claims sensíveis são ocultados integralmente. Todo novo campo sensível exige atualização do `TableSpec` e da migration; nunca confie apenas em heurística de nomes.

### Dados pessoais

`TableSpec.personal_fields` identifica dados pessoais — como o email e os nomes em `User` ou o `display_name` em `UserProfile` ([#189](https://github.com/alaorneto/skeleton/issues/189)). Esses campos continuam acessíveis via `read_fields`, mas os triggers os gravam na auditoria como `[REDACTED]` com `redacted: true`. Dessa forma, a auditoria nunca armazena PII, referencia a pessoa apenas pelo `sysid` e não impede a anonimização futura da identidade. A declaração é congelada na especificação da infraestrutura; divergências entre o spec e a migration falham no check `E032`.

Eventos gravados antes dessa declaração mantêm os valores antigos até que se execute `manage.py redact_audit_personal_data --confirm`, que os redige em uma única passagem e constitui a única exceção à regra append-only:

- executa exclusivamente com o owner da tabela de auditoria (`MIGRATION_DATABASE_URL` ou `--database-url`), recusando a role runtime;
- em uma única transação, desliga o guard, reescreve apenas `old`, `new` e `redacted` das colunas pessoais e religa o guard;
- registra o evento de segurança `audit.personal_data_redacted` com a contagem de linhas atualizadas.

Faça backup antes de rodar o comando, pois a reescrita é irreversível.

No PostgreSQL, ator, requisição e origem vêm dos parâmetros locais do contexto (GUCs), enquanto `db_principal` registra `session_user` (a identidade SQL da conexão). Tabela, organização e registro vêm diretamente de `OLD` e `NEW` no banco, nunca da entrada do cliente. Na ausência de `source`, registra-se `sql:unattributed`, sem fabricar um usuário.

O escritor de auditoria é um trigger `SECURITY DEFINER`: roda com os privilégios de seu proprietário, usa `search_path` fixo e não possui permissão de escrita em tabelas de negócio. Todo `INSERT` de auditoria organizacional precisa satisfazer a política de RLS, pois o audit writer não tem `BYPASSRLS` — e tampouco equivale ao papel empresarial de administrador.

## Metadados e CAS

**Compare-and-swap (CAS)** significa "grave somente se a revisão ainda corresponder à que foi lida". Imagine Alice e Bob abrindo a mesma nota em Alfa na revisão 4: Alice salva e a revisão avança para 5; quando Bob tenta salvar a partir da revisão 4, recebe `ConcurrentUpdate` em vez de sobrescrever o trabalho de Alice, precisando reler o registro para conciliar as mudanças. Uma nota de Beta na revisão 4 é outra linha, já que a verificação combina identificador e revisão. Nunca retente a gravação silenciosamente com a revisão nova.

`SystemRecord.save` valida invariantes, garante a imutabilidade de `sysid` e `organization` e compara chave primária e revisão original. Na atualização, incrementa a revisão e preenche datas e ator. `_state.adding=True` impede sobrescrever uma linha existente. `update_fields` grava apenas os campos solicitados junto aos metadados; se vazio, nada é escrito. Revisão obsoleta levanta `ConcurrentUpdate`. Quando `save()`, `delete()` ou `restore()` falham, a instância em memória é revertida ao estado anterior à chamada (os metadados no `save()`, e todos os campos no `delete()` e `restore()`): assim, corrigir um campo inválido e tentar salvar novamente não gera conflito falso, e uma exclusão abortada não deixa o objeto aparentando estar excluído.

`QuerySet.update` atualiza `updated_at` e `updated_by` com `F('revision') + 1` e bloqueia mudanças em `sysid`, organização e autoria de criação. Esse incremento em lote não substitui a verificação de revisão lida pelo cliente: endpoints continuam exigindo os serviços e o contrato de CAS. `bulk_create` prepara metadados e valida cada instância; `ignore_conflicts` e `update_conflicts` são recusados, e `bulk_update` levanta `UnsafeBulkMutation`. `QuerySet.update` valida e grava linha a linha (bloqueando cada linha e executando `full_clean()`), o que garante consistência mas gera consultas proporcionais ao volume de linhas; ao validar referências a registros próprios, consulta apenas `deleted_at` e `organization_id` do alvo. Nunca use operações em lote para contornar serviços ou autorização.

SQL direto também precisa informar `revision = OLD.revision + 1`, datas e valores coerentes. Os guards do banco, independentes da camada Python, bloqueiam revisões incorretas, alterações de `sysid` ou organização e `DELETE` físico. Chaves estrangeiras compostas no PostgreSQL impedem relações entre organizações em `INSERT` e `UPDATE`.

## Soft delete e restore

**Soft delete** (exclusão lógica) preenche `deleted_at` em vez de apagar fisicamente a linha. Essa marca chama-se **tombstone**: `deleted_at IS NOT NULL` é sua única definição, sem uma coluna redundante `is_deleted`. O manager padrão omite tombstones das consultas e filtra pela organização atual; sem contexto organizacional, levanta `MissingTenantContext`.

O método `with_deleted()` remove **apenas** o filtro de exclusão lógica inserido pelo manager — em qualquer profundidade e inclusive em querysets combinados com `|` e `&` (o filtro usa um lookup próprio, `LiveRows`, identificado pelo tipo, mantendo qualquer `deleted_at__isnull=True` escrito explicitamente pelo chamador). Ele nunca expõe dados de Beta a quem está em Alfa nem dispensa autorização. O base manager interno sem filtros existe apenas para infraestrutura, nunca para serializers, seletores de referência ou templates.

Registros selados ([modelo de dados](data-model.md#registros-selados-526)) recusam exclusão lógica e restauração — tanto no modelo (`RecordSealed`) quanto no guard do banco —, exceto quando o modelo declara `seal_allows_delete = True`. O selo em si é auditado como um `update` comum com `seal_digest` legível.

A exclusão de instância ou queryset é sempre lógica e não dispara a coleta nem a cascata de exclusão física do Django. Exclusões em lote executam dentro de um único `atomic`, com triggers por linha. A restauração (`restore`) revalida referências vivas e restrições de unicidade, preservando `sysid`; em caso de conflito, não restaura parcialmente nem sobrescreve outro registro. As superfícies (páginas e API REST) só permitem restaurar tombstones: tentar restaurar um registro vivo retorna 404, igual a um inexistente, sem invocar o serviço de restauração.

Por exemplo, se Alice excluir em Alfa a nota de código `ATA-01` e outra nota viva passar a usar esse código em Alfa, a restauração da nota antiga falhará, mantendo seu tombstone e seu UUID originais. Já uma nota `ATA-01` em Beta não conflita quando a unicidade é por organização. Restaurar um registro tampouco reativa automaticamente vínculos de acesso previamente revogados.

As políticas de dependência são explícitas:

- **`protect` (padrão):** existir dependente próprio vivo levanta `LiveReferencesExist`; revogue ou remova os dependentes antes.
- **`retain` em `User` e `Organization`:** preserva a identidade, o histórico de vínculos e os dados de negócio. O serviço de ciclo de vida desativa o acesso antes de gravar o tombstone, sem cascata. Restaurar um `User` mantém `is_active=False` até reativação explícita pela plataforma.
- **`Membership`:** o serviço revoga `MemberRole`, `GroupMember` e `RecordGrant` do vínculo no mesmo `atomic`, verifica a proteção do último administrador e só então marca a exclusão do vínculo. A saída voluntária (`leave_organization`) executa a mesma exclusão lógica de uma remoção administrativa: preserva UUID, revisão e histórico, atribui os eventos a quem saiu e permite que um convite aceito depois restaure o mesmo vínculo.
- **`Role` e `AccessGroup`:** referências vivas precisam ser revogadas previamente; papéis internos (`builtins`) são protegidos.
- **Sessões:** desativar ou excluir um `User` (ou revogar globalmente) incrementa `auth_version`, encerrando sessões antigas. Emails e identificadores externos (`subjects`) não são reutilizados.

Tabelas de bibliotecas técnicas mantêm seu ciclo de vida próprio: sessões expiram ou são removidas tecnicamente, e o allauth gerencia seus dados de protocolo. Não aplique exclusão lógica genérica nem auditoria de conteúdo nessas tabelas; o manifesto documenta cada exceção.

## Leitura e retenção

A auditoria é sempre somente leitura. Administradores organizacionais veem os eventos da própria organização; outros usuários precisam de `table.audit` **e** leitura autorizada do registro atual (ou autorização de restauração, se o registro estiver excluído). Para linhas de [tabelas derivadas](access-control.md#tabelas-derivadas), o histórico exige `audit` na tabela âncora **e** leitura do registro âncora vivo, esteja o filho vivo ou excluído — tabelas derivadas não recebem `TablePermission` própria. A redação de `sensitive_fields` e `personal_fields` permanece ativa, e os campos exibidos limitam-se aos `read_fields` do filho. A filtragem sempre ocorre antes de contar ou paginar. Eventos globais e o inventário técnico são exclusivos da plataforma, e eventos de registros apagados fisicamente offline só ficam visíveis para administradores organizacionais ou da plataforma.

Na API, no HTML e na administração, a exibição de eventos de negócio segue `TableSpec.read_fields`, incluindo nomes físicos de FKs: um campo omitido da leitura comum não reaparece no histórico, embora a captura persistida permaneça íntegra e redigida. Eventos administrativos do núcleo compõem uma superfície privilegiada separada, mantendo segredos ocultados. Uma especificação desconhecida não exibe `changes`. Ter `audit` sem `read` em uma tabela exclui apenas essa tabela inelegível, sem abortar a listagem de eventos permitidos de outras tabelas.

**Append-only** significa que a conexão runtime só consegue acrescentar eventos, nunca alterá-los ou removê-los. Isso **não** equivale a **WORM** (armazenamento imutável contra reescrita): o DBA ou o owner de migrations pode alterar o schema, e quem controla o servidor PostgreSQL pode modificar ou apagar dados. Retenção, backup, criptografia e proteção de credenciais privilegiadas exigem controles operacionais; nunca prometa imutabilidade contra esses atores.

### Tabela de retenções

Todo apagamento físico é executado por um job declarado em código — por organização quando a tabela é organizacional — com prazo definido em um setting do catálogo da instância. Registros de negócio nunca são apagados fisicamente: o soft delete os mantém como tombstones.

| O quê | Job | Setting (padrão) |
|---|---|---|
| Eventos de domínio já entregues | `events.retention` | `SKELETON_EVENT_RETENTION_DAYS` (30) |
| Jobs concluídos ou cancelados / esgotados | `jobs.retention` | `SKELETON_JOB_SUCCEEDED_RETENTION_DAYS` (7) / `SKELETON_JOB_DEAD_RETENTION_DAYS` (30) |
| Entregas de webhook | `webhooks.retention` | `SKELETON_WEBHOOK_RETENTION_DAYS` (30) |
| Notificações lidas, não lidas e entregas | `notifications.retention` | `SKELETON_NOTIFICATION_*_RETENTION_DAYS` |
| Arquivos de exportação e importação CSV | `transfer.retention` | `SKELETON_TRANSFER_RETENTION_DAYS` (7) |
| Objetos sem linha, deixados por rollback | `files.orphans` | `SKELETON_FILES_ORPHAN_GRACE_SECONDS` (1 hora) |
| Imagens da marca da instância sem referência | `appearance.orphans` | `SKELETON_FILES_ORPHAN_GRACE_SECONDS` (1 hora) |
| Bytes de anexos excluídos | `files.purge` | `SKELETON_FILES_DELETED_RETENTION_DAYS` (90) |
| Valores de formulário de instâncias de fluxo encerradas | `flows.purge_form_data` | `FlowSpec.retention_days` ou `SKELETON_FLOWS_FORM_RETENTION_DAYS` (0: mantém) |
| Auditoria | nenhum: append-only; apenas dados pessoais antigos são redigidos uma vez (`redact_audit_personal_data`) | — |

### Filtros de investigação

As três superfícies de consulta aceitam os mesmos filtros ([#184](https://github.com/alaorneto/skeleton/issues/184)): a auditoria da organização (`/o/<org>/audit/`), a da plataforma (`/platform/audit/`) e a API REST (`/api/v1/o/<org>/audit/`):

- `from` e `to`: uma data (que cobre o dia inteiro no fuso da pessoa) ou data e hora em ISO 8601;
- `actor` (UUID), `operation` (`create`, `update`, `soft_delete`, `restore`, `hard_delete`) e `request_id`;
- `model_label` e `record_sysid`.

O parser compartilhado é `skeleton.audit.filters.parse_audit_filters`. Filtros desconhecidos ou inválidos respondem 400. Os filtros apenas restringem o conjunto já permitido por `authorized_audit_queryset` — informar um `request_id` de outra organização não revela nada. As buscas apoiam-se nos índices `sk_audit_org_actor` (organização, ator, data) e `sk_audit_request`. Quando filtra por `request_id`, o administrador da organização vê também os eventos de domínio da mesma requisição ainda retidos, exibindo apenas identificadores e nunca o campo `data`.

Nas duas páginas HTML, as alterações de cada evento aparecem em uma tabela **Campo / Antes / Depois**, com uma linha por coluna visível em ordem alfabética: valores ausentes aparecem como "—", valores redigidos como "[redigido]", e números, booleanos e estruturas são exibidos em JSON. A API REST mantém `changes` como objeto `{old, new, redacted}` por campo.

O nome físico da tabela (`table_name`) e a role do banco que efetuou a escrita (`db_principal`) interessam apenas à administração: no log, ambos chegam vazios para membros que leem eventos via `TablePermission audit` sem serem administradores da organização, embora a captura persistida permaneça completa.

### Exportação CSV

O administrador da organização pode exportar o resultado filtrado da auditoria em `/o/<org>/audit/` pelo botão **Exportar CSV**, que preserva os filtros ativos na tela ([#186](https://github.com/alaorneto/skeleton/issues/186)).

- **Execução:** usa o pipeline de [exportação e importação](data-transfer.md). O job consulta `AuditLogService.events` em nome de quem solicitou, garantindo as mesmas linhas e a mesma redação da tela; a coluna `changes` sai em JSON com valores sensíveis substituídos por `[REDACTED]`.
- **Limite:** `SKELETON_EXPORT_MAX_ROWS`.
- **Download:** restrito ao próprio administrador que pediu a exportação.
- **Escopo:** não inclui a auditoria global da plataforma, pois toda transferência pertence a uma organização.
