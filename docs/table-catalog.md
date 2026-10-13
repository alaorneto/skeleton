# Catálogo de tabelas e colunas

O catálogo responde **“quais tabelas e colunas existem neste banco?”**. O registro de especificações em código responde **“quais modelos o sistema conhece e quais campos pode expor?”**. São responsabilidades distintas: editar uma descrição não deve criar tabelas nem abrir uma API. Veja [registros e banco de dados](concepts.md#registros-e-banco-de-dados) e [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment).

## Duas fontes com responsabilidades diferentes

**Registry, ou registro de especificações em código:** cada `TableSpec` imutável descreve um modelo, com registro único por `_meta.label_lower`. Declara explicitamente campos de leitura (`read_fields`), escrita (`write_fields`) e sensibilidade (`sensitive_fields`): listas de permissão, não “todos os campos”. `expose_api=False` é o padrão. Só uma spec exposta de `SecuredRecord` pode usar o gateway, a entrada genérica da API para registros de negócio. O cliente nunca fornece classe Python, nome físico/SQL ou `__all__`.

**Catálogo no banco:** inventaria tabelas reais e colunas/restrições encontradas por introspecção, isto é, consulta da estrutura do banco. Cada `TableDefinition`/`FieldDefinition` tem UUID próprio, mesmo quando descreve uma tabela técnica com chave primária inteira. O operador só edita `label`/`description`. O catálogo não habilita o gateway nem executa DDL (SQL de criação/alteração de estrutura). Não construa um mecanismo de schema dinâmico a partir dele.

Por exemplo, o catálogo pode descrever a tabela de notas usada por Alfa e Beta e a tabela técnica `django_session`. Isso não abre as notas privadas de Alfa para Beta nem transforma sessões em recursos da API. O UUID da `TableDefinition` identifica a descrição da tabela; o `sysid` de uma nota identifica uma linha dessa tabela.

## Sincronização

```sh
.venv/bin/python manage.py sync_table_catalog --database default
.venv/bin/python manage.py check_skeleton_schema --database default
```

O serviço público `sync_table_catalog(*, using='default', apps_registry=None)` cruza apps/modelos, incluindo modelos criados automaticamente (`auto-created`), com a estrutura física: schema `skeleton` no PostgreSQL e `main` no SQLite. Deduplica modelos proxy, substituídos (`swapped`) e modelos que apontam à mesma tabela. Não inventaria sequências nem catálogos internos do banco como entidades de negócio.

A reconciliação altera **somente dados**, em uma transação `atomic`. Usa as chaves `(database_alias,schema_name,db_table)` e `(table,column)`, preserva UUIDs/textos humanos, atualiza informações físicas e marca `available=False` quando uma tabela/coluna desaparece. Não apaga entradas. Se não houver diferenças, não aumenta revisão nem gera auditoria extra.

O receiver `post_migrate` do app system usa o registro histórico de modelos e o alias do banco; só age se as tabelas existem e o router permite. `AppConfig.ready` não consulta o banco. O comando chama o mesmo serviço.

Depois do commit global, as concessões do papel interno `organization_admin` são reconciliadas em transações `system_context` independentes por organização, com IDs já materializados; bootstrap reutiliza a rotina. Alfa e Beta são processadas separadamente. O papel `member` não recebe concessões automáticas.

`origin` informa a origem: `core/application/framework/third_party/unclassified`. `scope` informa o alcance: `global/organization/technical`. Uma tabela física desconhecida fica `unclassified` e faz a verificação de implantação falhar: nunca ganha isenção por prefixo. Remover uma tabela preserva eventos e deixa sua entrada no catálogo indisponível.

## Campos físicos versus humanos

| Registro | Informações derivadas da estrutura/implementação | Textos editáveis por humanos |
|---|---|---|
| `TableDefinition` | alias/schema/db_table/model_label; base_class; uuid_managed; soft_delete; audit_mode; api_exposed; available | `label`, `description` |
| `FieldDefinition` | name/column/type_name/nullable/primary_key/relation_target/sensitive/available | `label`, `description` |

Uma FK pode se chamar `owner` em Django e `owner_id` no SQL. `relation_target` descreve o destino; não concede permissão para ler esse destino.

O catálogo organizacional mostra apenas tabelas que o usuário pode ler. Inventário técnico completo e edição dos textos pertencem à plataforma. Ver metadados de uma tabela não autoriza abrir um registro negado.

## Exceções técnicas nominais

Fonte versionada: `skeleton/system/technical_tables.json`. A lista descreve tabelas possíveis das versões fixadas; uma entrada ausente fisicamente não é inventada como disponível. Cada exceção conserva a chave primária e o ciclo de vida oficiais da biblioteca: não recebe base própria, gateway empresarial, exclusão lógica genérica ou cópias do conteúdo para auditoria.

| Tabela | Motivo específico |
|---|---|
| django_migrations | Histórico do executor de migrations; chave inteira e ciclo de vida Django |
| django_content_type | Registro técnico de tipos de modelo; chave inteira oficial |
| auth_permission | Permissões técnicas Django, não papéis/permissões empresariais (RBAC); chave inteira oficial |
| auth_group | Grupo técnico Django sem relação de groups do User próprio; não grupo empresarial |
| auth_group_permissions | Associação oficial Group/Permission; não associação empresarial própria |
| django_admin_log | Log de admin; evita auditoria genérica duplicada e cópias de segredos |
| django_session | Sessão opaca; expiração/invalidação oficial, nunca cópia de conteúdo para auditoria |
| django_site | Site técnico utilizado por bibliotecas quando instalado; chave inteira oficial |
| account_emailaddress | Estado de endereço/verificação allauth, ciclo de vida de identidade; sem exclusão lógica genérica |
| account_emailconfirmation | Prova técnica de confirmação; tokens não entram em auditoria genérica |
| socialaccount_socialaccount | Identificador externo (subject)/vínculo social allauth; atributos de identidade (claims) não recebem cópias genéricas para auditoria |
| socialaccount_socialapp | Configuração técnica allauth; segredos fora do catálogo empresarial |
| socialaccount_socialapp_sites | Associação técnica aplicação/site, não associação empresarial |
| socialaccount_socialtoken | Token allauth; armazenamento desabilitado e sem auditoria genérica de segredo |
| mfa_authenticator | Autenticador cifrado allauth; ciclo de vida próprio, sem cópia de segredo MFA para auditoria |

Não existe isenção geral para `django_*`, `account_*` ou apps de terceiros. Uma dependência nova exige revisão de cada tabela concreta e de associação (`through`), com motivo específico, inclusive tabelas de ciclo de vida. Associações de apps próprias devem ser modelos explícitos com UUID, não tabelas implícitas com chave inteira.

## Migration e checks

**A spec define o contrato; a migration instala sua proteção física.** `InstallRecordInfrastructure(app_label, model_name, specification)` usa modelos históricos de migrations e uma spec congelada com alcance (`scope`), sensibilidade/colunas, `append_only` e imutabilidade. Reemita a operação após alterações de campos auditáveis ou relações. Atualizar o cadastro de uma coluna não atualiza o trigger.

Há duas verificações complementares:

- **Checks estáticos:** não consultam o banco no `ready`; verificam base/UUID, organização obrigatória, associações próprias, listas de campos permitidos/sensibilidade, middleware/`ATOMIC_REQUESTS` e classificação.
- **`check_skeleton_schema` após migrations:** inspeciona tabelas/tipos/triggers/guards/FKs/RLS/FORCE e inventário de policies/roles/grants SQL. Policy permissiva extra, trigger ausente, UUID incorreto e tabela desconhecida são falhas, não avisos a ignorar. Em produção, inclui backend, ownership, privilégios e `SET ROLE`; o check comum não exige banco já migrado.

Apps próprias são identificadas por manifestos explícitos e pelo inventário nominal do núcleo, não por prefixos de pacote. `AppInstallation` é uma tabela organizacional própria não exposta no gateway; tem UUID, configuração JSON sensível e infraestrutura congelada na migration `sk_system.0003_appinstallation`. Specs, registros de decisões arquiteturais (ADRs) e dependências do manifesto participam dos checks estáticos.

`ComponentInstallation` e `DeploymentOperation` são entidades globais próprias, não exceções técnicas: derivam de SystemRecord, têm specs explícitas sem gateway e guards/auditoria instalados por `sk_system.0004_deployment`. Plano/resultado são sensíveis. Tabelas de apps retiradas permanecem catalogadas e protegidas por cópias congeladas da infraestrutura do release confiável; o catálogo editável não vira fonte de DDL. Histórico de migrations fica separado, somente no migrator; modelos conservados nunca entram no registry/gateway.

No PostgreSQL, encontrar um nome conhecido não basta. O check compara:

- a função realmente referenciada pelo trigger (`tgfoid`/schema), momento de execução, eventos, nível, `WHEN` e argumentos;
- o guard global `assert_user_deactivation`: corpo, assinatura, retorno, linguagem, tipo de rotina `prokind=f`, owner, `SECURITY DEFINER`, `search_path` e `EXECUTE`/grant option;
- a FK composta integral: schema/tabela alvo, ordem de colunas, validação, ações e possibilidade de adiar a verificação (`deferrability`).

Uma procedure com o mesmo nome, assinatura e corpo não substitui a função exigida. Funções de trigger devem ser funções sem argumentos com retorno `trigger`. Os testes negativos alteram DDL com owner para simular infraestrutura adulterada; o check e as assertions continuam usando a credencial runtime. Assim, possuir privilégios de migração não mascara a falha que a aplicação enfrentaria.



A execução real desses checks e os negativos estão em [testing](testing.md); descrição do contrato não substitui resultado observado.
