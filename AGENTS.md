# Instruções para agentes e projetos derivados

Este arquivo é o contrato normativo de implementação, tanto para agentes quanto para desenvolvedores humanos. Ele lista obrigações e limites, não um tutorial. Comece pelo `README.md`; use o [guia dos conceitos](docs/concepts.md) para entender o vocabulário e os guias abaixo para os procedimentos. Um exemplo ou comando não dispensa as regras de segurança deste contrato.

## Ordem de leitura

1. `README.md` e `docs/architecture.md`.
2. `docs/data-model.md`, `docs/access-control.md`, `docs/audit-soft-delete.md`.
3. `docs/table-catalog.md` e `docs/extending.md` antes de criar tabelas.
4. `docs/service-api.md` antes de criar uma app ou integrar apps; o procedimento é o skill `service-api`.
5. `docs/authentication-sso.md` para identidade; `docs/api.md` para endpoints.
6. `docs/testing.md` e `docs/deployment.md` antes de verificar/operar.
7. `docs/workflow.md` antes de planejar, commitar ou abrir PR.

## Rastreabilidade: plano, issue, commit e fechamento

Todo desenvolvimento versionado (código, testes, documentação, CI, dependências) está vinculado a uma issue do GitHub. O procedimento é o skill `issue-workflow` (`.claude/skills/issue-workflow/`); o racional e os mecanismos estão em [docs/workflow.md](docs/workflow.md).

- **Planos** do modo de planejamento terminam com a seção `Itens do GitHub`: para cada item, label de tipo, título, critérios de aceite, plano de testes e documentação a atualizar. Quando o plano é aceito, as issues (e um `epic` se houver duas ou mais) são criadas antes de qualquer edição. As filhas são sub-issues nativos do epic, e as dependências são relações "blocked by" (`tools/workflow/github_issues.py sync-epic`).
- **Sem plano**, a issue é identificada ou criada antes da primeira edição. Perguntas e investigações sem mudança de arquivo não precisam de issue.
- Cada issue tem **exatamente um** label de tipo (`bug`, `feature`, `enhancement`, `documentation`, `refactor`, `test`, `ci`, `chore`), mais `security` quando pertinente, e todas as seções do modelo: contexto, objetivo, escopo, abordagem, critérios de aceite, plano de testes, documentação, segurança, riscos e definição de pronto.
- Branch `<tipo>/<N>-<slug>`; commits com trailer `Refs #N` (o hook `commit-msg` verifica); PR com `Closes #N`. Nunca `--no-verify`.
- A issue fecha só depois de aprovada: pelo merge do PR ou por aprovação explícita do responsável, com comentário listando commits, testes executados e documentação. Mudança de escopo atualiza a issue ou abre outra.

## Fronteiras de código

- `skeleton/common`: bases abstratas, querysets, erros, logging e operação de migrations.
- `skeleton/accounts`: identidade global e integração allauth; não reimplemente OAuth, OIDC, SAML, MFA ou password reset.
- `skeleton/tenancy`: organização, vínculo, convite e contexto transacional.
- `skeleton/access`: papéis/grupos, concessões e serviços/policies.
- `skeleton/system`: registro de specs, catálogo e checks; metadados não executam DDL.
- `skeleton/audit`: eventos e infraestrutura de auditoria.
- `skeleton/integrations`: webhooks da organização (endpoints, entrega assinada, log); saídas HTTP só por `skeleton.common.outbound_http`.
- `skeleton/sequences`: sequências sem lacunas por organização e período.
- `skeleton/transfer`: exportação e importação CSV de tabelas expostas por job, com o arquivo no storage privado e a retenção declarada (`TransferJob`, operacional); só pelo caminho autorizado de leitura e escrita do gateway.
- `skeleton/flows`: motor de fluxos de processo de negócio (definições versionadas, instâncias, tarefas, formulários, aprovações; [docs/flows.md](docs/flows.md)). É só o motor: apps declaram os fluxos em código por `skeleton.flows.registry` e os usam pelo `FlowService`. Condições são árvores de dados ou callables registrados, nunca `eval`; nenhuma regra de negócio de um fluxo entra no núcleo.
- `skeleton/instance`: configuração da instância num SQLite próprio, lido antes do Django; nunca dados de aplicação. Todo dado vive no PostgreSQL (E025).
- `skeleton/setup`: setup do primeiro acesso, sem banco; a credencial owner vive só na requisição que migra.
- `skeleton/web`, `skeleton/api`: superfícies; autorização é revalidada no servidor.
- `skeleton/service_api`: API de serviços pública do núcleo para apps; base, DTOs, erros, introspecção do contrato e registro das APIs declaradas, e os serviços do núcleo (identidade, organização, acesso, apps, auditoria, notificações, jobs, eventos, arquivos, sequências e fluxos de processo; epics #113 e #209).
- `skeleton/testing`: kit de testes para projetos derivados (contrato da API de serviços, cenários A/B); só em testes.
- `skeleton/web/platform`: administração da plataforma (`/platform/`) em views explícitas sobre os serviços de lifecycle. O Django admin não tem site nem rotas (só `SimpleAdminConfig` para migrations históricas): não registre modelos nele nem crie editores genéricos de registros globais. Apps não podem capturar as raízes `platform/` e `admin/`.
- `config`: settings, URLs e deploy; `applications`: negócio do projeto derivado.
- `examples/notes`: exemplo didático opt-in, não dependência do núcleo.
- `skeleton/contrib`: apps first-party distribuídas no wheel do núcleo (manifest `kind: "bundled_app"`, label `skeleton_*`, versão igual à do core). Entram em `INSTALLED_APPS` só por nome em `SKELETON_BUNDLED_APPS`, contra o mapa fixo de `skeleton/conf/bundled.py` (nunca import path do ambiente), e seguem as regras de qualquer app: API de serviços, disponibilidade e ativação fechadas. O resto do núcleo nunca importa `skeleton.contrib`; apps de terceiros não usam o prefixo `skeleton_` nem `runtime.user_urls`.

**Não adicione regras de negócio em `skeleton`. Não renomeie os app labels do núcleo.** Adicione apps a `PROJECT_APPS` em `config/settings/project.py` e URLs a `config/urls_project.py`. O núcleo não importa `applications` ou `examples`.

## API de serviços entre apps

Apps interagem com o núcleo e entre si pela API de serviços: classes em processo definidas em [docs/service-api.md](docs/service-api.md). O procedimento é o skill `service-api` (`.claude/skills/service-api/`). A base técnica e as verificações automáticas estão entregues (epic #112): E018 confere a API declarada, E019/W019 a fronteira de importação, E012 a documentação, e o kit `skeleton.testing.service_api` prova o comportamento. Os serviços do núcleo (epic #113) estão catalogados em [docs/service-api.md](docs/service-api.md#catálogo-do-núcleo); use-os em vez dos módulos internos. O que os checks não alcançam (imports dinâmicos, semântica da autorização) continua valendo por revisão de código.

- Toda app publica `service_api.py` com ao menos uma classe de serviço (`app_label` da própria app) e a declara no manifesto em `runtime.service_api`, com piso de core `>=0.4.0`. Toda app mantém `docs/service-api.md` no `documentation_root`. `package build`/`doctor` recusam release sem a declaração; manifestos históricos só recebem W018.
- Uma app só importa de outra o `service_api` dela, e declara a dependência em `dependencies` com faixa de versão. Modelos, managers, `services.py`, forms e views de outra app não são contrato (E019). Do núcleo, importe só as superfícies públicas listadas na especificação (W019, depois erro). Não use import dinâmico para contornar a análise.
- Métodos públicos são keyword-only, recebem `actor` explícito igual ao ator do contexto e devolvem DTOs imutáveis, `Page` ou primitivos; nunca modelos, querysets ou campos sensíveis.
- O serviço autoriza internamente (`authorized_queryset`, `require_record_action`, capacidades) e exige a app ativa. Ser chamado por código não concede permissão. Inexistente e não autorizado respondem igual.
- O serviço participa da transação do chamador: não faz commit, não abre `system_context` para ampliar acesso, não troca de organização e não executa efeito externo dentro do método (use jobs, eventos ou `on_commit` com valores prontos).
- Alterações recebem `expected_revision`; criações repetíveis aceitam `idempotency_key`. Mudança incompatível em método público é versão *major* da app, precedida de depreciação.
- Testes de cada método cobrem caminho autorizado, default deny, A/B, app desativada, ator divergente e, em comandos, CAS e idempotência, em PostgreSQL com a role runtime, com `assert_service_contract` e os helpers de `skeleton.testing.service_api`.

## Contrato de cada tabela própria

- Toda entidade concreta própria deriva de `SystemRecord`, `OrganizationRecord` ou `SecuredRecord`. A herança é abstrata: não existe supertabela universal. UUID `sysid` é PK e imutável; códigos comerciais são campos separados.
- **Exceção operacional:** estado de pipeline do núcleo (fila de jobs, outbox, entregas, leitura de notificações) deriva de `OperationalRecord`: UUID, isolamento tenant e guard de identidade, sem revisão, soft delete nem auditoria por linha. A migration congela `lifecycle: operational`; exclusão física só pelo serviço de retenção declarado em código. Nunca use a base para dado de negócio, nunca a exponha no gateway (E020) e nunca dê `write_fields` a ela.
- Negócio genérico organizacional deriva de `SecuredRecord`. Organização obrigatória/imutável, owner ativo da mesma organização, visibilidade e grupo consistentes.
- Registre `TableSpec` único por `_meta.label_lower`; `read_fields`, `write_fields` e `sensitive_fields` explícitos. Nunca `__all__`, `eval` ou nomes de classes fornecidos pelo cliente. Campos sistêmicos e de acesso não entram na escrita comum. Só `SecuredRecord` pode usar gateway genérico exposto.
- Na migration da criação, e após mudanças de colunas auditáveis/relações, instale `InstallRecordInfrastructure` com especificação congelada. Use modelos históricos; nunca imports dos modelos correntes nem dados editáveis do catálogo para DDL.
- Relações entre entidades próprias usam `PROTECT`, links concretos UUID e infraestrutura de integridade tenant. Não introduza through implícito inteiro; não use `.add/.set/.clear` como caminho alternativo de segurança.
- Marque segredos antes da persistência em audit. Claims/JSON sensíveis são redigidos integralmente; não prometa que campos desconhecidos serão detectados automaticamente.
- Dados pessoais (email, nomes, documentos) entram em `personal_fields`: continuam legíveis pela API e saem `[REDACTED]` na auditoria, que cita a pessoa só pelo sysid (#189). A declaração é congelada com a infraestrutura (E032).
- Toda tabela técnica precisa de exceção nominal com motivo no manifesto; não acrescente isenção por prefixo.

## Contexto, autorização e concorrência

Use `request_context`, `tenant_context` e `system_context` de `skeleton.tenancy.context`. Em jobs, materialize IDs e abra uma transação independente por organização. `system_context(reason=...)` é infraestrutura/CLI, nunca escolhido por body/header, e não concede BYPASSRLS. `on_commit` não herda consulta tenant: passe valores prontos ou abra novo contexto.

Para negócio use `authorized_queryset(model, user=..., action=...)` e `require_record_action(record, user=..., action=...)`; admin de tenant usa serviços de `skeleton.access.services`. Não use `_base_manager`, `all_objects`, raw SQL, relações reversas não filtradas ou permissões Django globais como atalho em endpoints. `with_deleted()` remove só o filtro de tombstone, nunca tenant/autorização. Seleção de FK/picker tem de ser legível para o ator e da mesma organização.

Não aceite actor/tenant/sysid/revision/flags de usuário por mass assignment. PATCH/DELETE/restore requerem revisão; `me` compara User e Profile. Não retente silenciosamente CAS sobrescrevendo alterações. Campos owner/visibility/owning_group mudam só pela gestão de acesso autorizada.

## Exclusão e auditoria

`delete` é soft delete. `restore` valida unicidade/referências ativas; não recria UUID. `bulk_update` e bulk conflict upsert são proibidos. User/Organization retêm vínculos históricos; os demais registros protegem referências vivas. Use serviços de lifecycle/revogação para não esquecer `auth_version`, último admin e relações de acesso.

Não implemente purge por HTTP/admin/manager. O único expurgo de conteúdo de negócio é o job `files.purge` (#192), declarado em código: ele apaga os bytes de anexos excluídos há mais de `SKELETON_FILES_DELETED_RETENTION_DAYS` e mantém a linha como tombstone `purged`. Auditoria vem de triggers, na mesma transação; não adicione signal de save como segunda fonte de eventos. AuditEvent é append-only. A única exceção é `manage.py redact_audit_personal_data --confirm`: só o owner pode rodá-lo, e ele redige os `personal_fields` de eventos antigos numa transação que religa o guard (#189). DBA/owner permanecem fronteira privilegiada, não WORM; quem controla o SQLite da instância e a chave mestra controla a configuração.

## Checklist mínimo de nova tabela

- UUID/base/spec/allowlists/sensitive corretos; tabela física catalogada.
- Migration de infraestrutura, guards, FK tenant e triggers; deployment check positivo e negativos de infraestrutura removida.
- Tenant ausente falha fechado; A/B não vazam em lista, detalhe, contagem, FK picker, audit e restore.
- Default deny, TablePermission obrigatória, grants não pulam tenant/tabela; revogação imediata.
- Visibilidade org/grupo concede leitura, não escrita; último admin protegido quando pertinente.
- CAS com duas revisões iguais tem um vencedor; update_fields vazio não escreve.
- Auditoria create/save/queryset/bulk/SQL/delete/restore correta, segredos redigidos, rollback/savepoint sem eventos residuais.
- Soft delete/restore/conflito/referências preservam IDs e dados.
- PostgreSQL com **role runtime** (toda a suíte; `tools/dev_postgres.py` localmente), sem usar owner para provar RLS; o owner só migra ou monta DDL de fixture. Fixtures protegidas, sem flush/TRUNCATE de tabelas próprias.
- Testes novos seguem [Escrever testes que mantêm a suíte rápida](docs/testing.md#escrever-testes-que-mantêm-a-suíte-rápida). Banco só quando a prova depende dele; casos de leitura dividem um preparo (`subtests`). Nada de porta fixa, arquivo fora de `tmp_path`, `sleep` de espera, estado global sem restauração ou dependência de ordem: a suíte roda em paralelo (`-n`). Tempo não se ganha pulando teste nem trocando o PostgreSQL com a role runtime.

Atualize documentação afetada. Registre somente testes realmente executados; serviços indisponíveis são bloqueios de verificação, nunca aprovação por inferência.
