# Catálogo de tabelas e colunas

## Duas fontes com responsabilidades diferentes

**Registry no código:** `TableSpec` imutável, registro único por `_meta.label_lower`; campos de leitura/escrita/sensibilidade explícitos; expose_api=False por padrão. Só spec de SecuredRecord exposta pode usar gateway. Cliente nunca fornece classe Python, nome físico/SQL ou `__all__`.

**Catálogo no banco:** inventário das tabelas reais e das colunas/constraints introspectadas. Cada TableDefinition/FieldDefinition tem UUID próprio, mesmo quando descreve uma tabela técnica com PK inteira. Operador só edita label/description. Catálogo não habilita gateway nem executa DDL. Não crie engine de runtime schema a partir dele.

## Sincronização

```sh
.venv/bin/python manage.py sync_table_catalog --database default
.venv/bin/python manage.py check_skeleton_schema --database default
```

O serviço público `sync_table_catalog(*, using='default', apps_registry=None)` cruza apps/modelos (incluindo auto-created) e introspecção física: schema `skeleton` no PostgreSQL, `main` no SQLite. Deduplica proxy/swapped/mesma tabela. Não inventaria sequências/catálogos internos do engine como entidades empresariais.

A reconciliação é data-only e atomic: chave `(database_alias,schema_name,db_table)` e `(table,column)`; preserva sysids/textos humanos, atualiza físicos e marca `available=False` quando sumirem. Não deleta entradas. Sem diferenças não aumenta revisão nem gera audit extra. Receiver post_migrate do app system usa registry histórico/alias e só age se tabelas existem e router permite; ready não consulta DB. O comando usa o mesmo serviço.

Depois do commit global, concessões builtin organization_admin são reconciliadas em transações system_context independentes por organização, com IDs materializados; bootstrap reutiliza rotina. Role member não recebe grants automáticos.

`origin`: core/application/framework/third_party/unclassified. `scope`: global/organization/technical. Tabela física desconhecida fica unclassified e faz deployment check falhar; nunca é isenta por prefixo. Remoção de tabela preserva eventos e deixa catálogo indisponível.

## Campos físicos versus humanos

TableDefinition: alias/schema/db_table/model_label; base_class; uuid_managed; soft_delete; audit_mode; api_exposed; available são derivados da implementação/estrutura. `label`, `description` são humanos. FieldDefinition: name/column/type_name/nullable/primary_key/relation_target/sensitive/available são físicos/de spec; label/description são humanos. Nome Django de FK difere da coluna `<field>_id`; relation_target identifica destino, não uma permissão de leitura desse destino.

Catálogo tenant mostra somente tabelas que o usuário pode ler. Inventário técnico completo e edição de textos são da plataforma. Expor metadata não autoriza abrir um registro negado.

## Exceções técnicas nominais

Fonte versionada: `skeleton/system/technical_tables.json`. A lista descreve tabelas possíveis dos releases fixados; entrada que não existe fisicamente não é inventada como tabela disponível. Cada exceção mantém PK/lifecycle oficial, não recebe base própria, gateway empresarial, generic soft delete ou snapshots de payload.

| Tabela | Motivo específico |
|---|---|
| django_migrations | Histórico do executor de migrations; chave inteira e lifecycle Django |
| django_content_type | Registro técnico de tipos de modelo; chave inteira oficial |
| auth_permission | Permissões técnicas Django, não RBAC empresarial; chave inteira oficial |
| auth_group | Grupo técnico Django sem relação de groups do User próprio; não grupo empresarial |
| auth_group_permissions | Through oficial Group/Permission; não link empresarial próprio |
| django_admin_log | Log de admin; evitar auditoria genérica duplicada e snapshots de segredos |
| django_session | Sessão opaca; expiração/invalidação oficial, nunca snapshot de payload |
| django_site | Site técnico utilizado por bibliotecas quando instalado; chave inteira oficial |
| account_emailaddress | Estado de endereço/verificação allauth, lifecycle de identidade; sem soft delete genérico |
| account_emailconfirmation | Prova técnica de confirmação; tokens não entram em auditoria genérica |
| socialaccount_socialaccount | Subject/vínculo social allauth; claims não recebem snapshots genéricos |
| socialaccount_socialapp | Configuração técnica allauth; segredos fora do catálogo empresarial |
| socialaccount_socialapp_sites | Through técnico aplicação/site, não associação empresarial |
| socialaccount_socialtoken | Token allauth; armazenamento desabilitado e sem auditoria genérica de segredo |
| mfa_authenticator | Authenticator cifrado allauth; lifecycle próprio, sem snapshot de segredo MFA |

Não existe isenção blanket `django_*`, `account_*` ou por app third-party. Dependência nova exige revisão de cada tabela concreta/through e motivo, inclusive tables de lifecycle. Throughs de apps próprios devem ser modelos explícitos UUID.

## Migration e checks

`InstallRecordInfrastructure(app_label, model_name, specification)` usa histórico de migrations e spec congelada com scope, sensibilidade/colunas, append_only e imutabilidade. Reemita após mudanças auditáveis/relações. Atualizar cadastro de coluna não atualiza trigger.

Checks estáticos não consultam banco no ready: base/UUID, tenant obrigatório, throughs próprios, allowlists/sensibilidade, middleware/ATOMIC_REQUESTS e classificação. `check_skeleton_schema` pós-migrate inspeciona tabelas/tipos/triggers/guards/FKs/RLS/FORCE e inventário de policies/roles/grants. Policy permissiva extra, trigger faltante, UUID errado e desconhecido são falhas, não warning a ignorar. Produção inclui backend/ownership/privilégios/SET ROLE; o check comum não exige DB já migrado.

Apps próprias são identificadas por manifests explícitos e pelo inventário nominal do núcleo, não por prefixos de pacote. `AppInstallation` é tabela organizacional própria não exposta no gateway; tem UUID, configuração JSON sensível e infraestrutura congelada na migration `sk_system.0003_appinstallation`. Specs/ADRs e dependências do manifest participam dos checks estáticos.

`ComponentInstallation` e `DeploymentOperation` são entidades globais próprias, não exceções técnicas: derivam de SystemRecord, têm specs explícitas sem gateway e guards/auditoria instalados por `sk_system.0004_deployment`. Plano/resultado são sensíveis. Tabelas de apps retiradas permanecem catalogadas e protegidas por snapshots congelados do release confiável; o catálogo editável não vira fonte de DDL. Histórico de migrations fica separado, somente no migrator; modelos retidos nunca entram no registry/gateway.

No PostgreSQL o check compara também a função realmente referenciada pelo trigger (tgfoid/schema), timing/eventos/nível/WHEN/argumentos, guard global `assert_user_deactivation` (corpo, assinatura, retorno, linguagem, tipo de rotina `prokind=f`, owner, SECURITY DEFINER, search_path e EXECUTE/grant option) e FK composta integral (schema/tabela alvo, ordem de colunas, validação, ações e deferrability). Uma procedure com mesmo nome, assinatura e corpo não substitui a função exigida. Funções de trigger devem ser funções sem argumentos com retorno `trigger`. Negativos alteram DDL com owner; o check e as assertions continuam usando runtime.



A execução real desses checks e os negativos estão em [testing](testing.md); descrição do contrato não substitui resultado observado.
