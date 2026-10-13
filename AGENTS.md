# Instruções para agentes e projetos derivados

## Ordem de leitura

1. `README.md` e `docs/architecture.md`.
2. `docs/data-model.md`, `docs/access-control.md`, `docs/audit-soft-delete.md`.
3. `docs/table-catalog.md` e `docs/extending.md` antes de criar tabelas.
4. `docs/authentication-sso.md` para identidade; `docs/api.md` para endpoints.
5. `docs/testing.md` e `docs/deployment.md` antes de verificar/operar.

## Fronteiras de código

- `skeleton/common`: bases abstratas, querysets, erros, logging e operação de migrations.
- `skeleton/accounts`: identidade global e integração allauth; não reimplemente OAuth, OIDC, SAML, MFA ou password reset.
- `skeleton/tenancy`: organização, vínculo, convite e contexto transacional.
- `skeleton/access`: papéis/grupos, concessões e serviços/policies.
- `skeleton/system`: registro de specs, catálogo e checks; metadados não executam DDL.
- `skeleton/audit`: eventos e infraestrutura de auditoria.
- `skeleton/web`, `skeleton/api`: superfícies; autorização é revalidada no servidor.
- `config`: settings, URLs e deploy; `applications`: negócio do projeto derivado.
- `examples/notes`: exemplo didático opt-in, não dependência do núcleo.

**Não adicione regras de negócio em `skeleton`. Não renomeie os app labels do núcleo.** Adicione apps a `PROJECT_APPS` em `config/settings/project.py` e URLs a `config/urls_project.py`. O núcleo não importa `applications` ou `examples`.

## Contrato de cada tabela própria

- Toda entidade concreta própria deriva de `SystemRecord`, `OrganizationRecord` ou `SecuredRecord`. A herança é abstrata: não existe supertabela universal. UUID `sysid` é PK e imutável; códigos comerciais são campos separados.
- Negócio genérico organizacional deriva de `SecuredRecord`. Organização obrigatória/imutável, owner ativo da mesma organização, visibilidade e grupo consistentes.
- Registre `TableSpec` único por `_meta.label_lower`; `read_fields`, `write_fields` e `sensitive_fields` explícitos. Nunca `__all__`, `eval` ou nomes de classes fornecidos pelo cliente. Campos sistêmicos e de acesso não entram na escrita comum. Só `SecuredRecord` pode usar gateway genérico exposto.
- Na migration da criação, e após mudanças de colunas auditáveis/relações, instale `InstallRecordInfrastructure` com especificação congelada. Use modelos históricos; nunca imports dos modelos correntes nem dados editáveis do catálogo para DDL.
- Relações entre entidades próprias usam `PROTECT`, links concretos UUID e infraestrutura de integridade tenant. Não introduza through implícito inteiro; não use `.add/.set/.clear` como caminho alternativo de segurança.
- Marque segredos antes da persistência em audit. Claims/JSON sensíveis são redigidos integralmente; não prometa que campos desconhecidos serão detectados automaticamente.
- Toda tabela técnica precisa de exceção nominal com motivo no manifesto; não acrescente isenção por prefixo.

## Contexto, autorização e concorrência

Use `request_context`, `tenant_context` e `system_context` de `skeleton.tenancy.context`. Em jobs, materialize IDs e abra uma transação independente por organização. `system_context(reason=...)` é infraestrutura/CLI, nunca escolhido por body/header, e não concede BYPASSRLS. `on_commit` não herda consulta tenant: passe valores prontos ou abra novo contexto.

Para negócio use `authorized_queryset(model, user=..., action=...)` e `require_record_action(record, user=..., action=...)`; admin de tenant usa serviços de `skeleton.access.services`. Não use `_base_manager`, `all_objects`, raw SQL, relações reversas não filtradas ou permissões Django globais como atalho em endpoints. `with_deleted()` remove só o filtro de tombstone, nunca tenant/autorização. Seleção de FK/picker tem de ser legível para o ator e da mesma organização.

Não aceite actor/tenant/sysid/revision/flags de usuário por mass assignment. PATCH/DELETE/restore requerem revisão; `me` compara User e Profile. Não retente silenciosamente CAS sobrescrevendo alterações. Campos owner/visibility/owning_group mudam só pela gestão de acesso autorizada.

## Exclusão e auditoria

`delete` é soft delete. `restore` valida unicidade/referências ativas; não recria UUID. `bulk_update` e bulk conflict upsert são proibidos. User/Organization retêm vínculos históricos; os demais registros protegem referências vivas. Use serviços de lifecycle/revogação para não esquecer `auth_version`, último admin e relações de acesso.

Não implemente purge por HTTP/admin/manager. Auditoria vem de triggers, na mesma transação; não adicione signal de save como segunda fonte de eventos. AuditEvent é append-only, inclusive no SQLite. DBA/owner e quem controla arquivo SQLite permanecem fronteiras privilegiadas, não WORM.

## Checklist mínimo de nova tabela

- UUID/base/spec/allowlists/sensitive corretos; tabela física catalogada.
- Migration de infraestrutura, guards, FK tenant e triggers; deployment check positivo e negativos de infraestrutura removida.
- Tenant ausente falha fechado; A/B não vazam em lista, detalhe, contagem, FK picker, audit e restore.
- Default deny, TablePermission obrigatória, grants não pulam tenant/tabela; revogação imediata.
- Visibilidade org/grupo concede leitura, não escrita; último admin protegido quando pertinente.
- CAS com duas revisões iguais tem um vencedor; update_fields vazio não escreve.
- Auditoria create/save/queryset/bulk/SQL/delete/restore correta, segredos redigidos, rollback/savepoint sem eventos residuais.
- Soft delete/restore/conflito/referências preservam IDs e dados.
- SQLite real e PostgreSQL com **role runtime**, sem usar owner para provar RLS. Fixtures protegidas, sem flush/TRUNCATE de tabelas próprias.

Atualize documentação afetada. Registre somente testes realmente executados; serviços indisponíveis são bloqueios de verificação, nunca aprovação por inferência.
