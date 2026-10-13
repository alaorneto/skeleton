# Arquitetura

## Planos separados

1. **Identidade/controle global:** User, Profile, Organization, catálogo e bibliotecas de autenticação. A organização não duplica identidade. Essas tabelas não recebem RLS tenant; superfícies próprias limitam quem pode vê-las.
2. **Dados organizacionais:** Membership, RBAC, convites e negócio derivado. Toda linha tem organização obrigatória. Django verifica vínculo, permissão de tabela e visibilidade/ação; PostgreSQL RLS protege o isolamento tenant.
3. **Técnico:** migrations, sessões, content types, permissões globais técnicas e tabelas allauth. Conservam schema e lifecycle oficiais, são catalogadas individualmente e não entram no gateway empresarial.

UUID não é autorização. `sysid` identifica uma linha; código comercial e nome físico de tabela não são esse identificador. `table_key` público é label registrado, nunca SQL fornecido pela URL.

## Núcleo e extensão

`SystemRecord` → `OrganizationRecord` → `SecuredRecord` são classes abstratas. Seus campos são copiados para cada tabela concreta; não existe tabela pai universal nem join universal. Apps próprios globais podem usar `SystemRecord`; novos modelos empresariais usam `SecuredRecord`. Projetos vivem em `applications`; o exemplo externo `examples.notes` é habilitado explicitamente. Settings/URLs de extensão evitam renomear o núcleo.

App labels: `sk_accounts`, `sk_tenancy`, `sk_access`, `sk_system`, `sk_audit`; exemplo `example_notes`. `AUTH_USER_MODEL='sk_accounts.User'` é definido antes de qualquer migration. Alterá-lo após produção não é uma configuração trivial.

## Fluxo de uma request

- Security/WhiteNoise, sessão, CSRF, autenticação e mensagens precedem `TenantContextMiddleware`; AccountMiddleware allauth vem depois.
- Middleware resolve o usuário da sessão, confere atividade/tombstone/auth_version e abre `atomic` explícito. `ATOMIC_REQUESTS=False`.
- Gera request UUID no servidor, instala contexto global e GUCs `SET LOCAL` no PostgreSQL.
- URL `/o/<uuid>/...` ou `/api/v1/o/<uuid>/...` seleciona tenant após validar vínculo e organização ativos; não aceita actor/tenant de header/body.
- Policy/service filtra autorização antes de detalhes, contagens e paginação. Forms e API chamam os mesmos serviços.
- Renderização de TemplateResponse ocorre ainda no contexto. Resposta >=400 marca rollback; ContextVars são restauradas em `finally`.

Não use streaming dependente de ORM tenant nem ORM transacional em async views. WSGI/Gunicorn é o runtime síncrono inicial; ASGI é entrypoint Django, não promessa de transações async.

## Persistência e auditoria

Save usa compare-and-swap por sysid/revision original, incrementando revisão. Metadados de criação/alteração recebem ator do contexto. Querysets controlam bulk e soft delete; migrations instalam guards independentes de Python. Triggers criam um evento por mudança efetiva, dentro da transação, com redação antes de gravar.

Grafo inicial: accounts 0001 → tenancy 0001; system 0001; access 0001 depende tenancy/system; tenancy 0002 cria Invitation; audit 0001 cria evento sem FKs; audit 0002 depende do conjunto e instala infraestrutura. Example notes 0001 depende audit 0002 e instala a sua. `sk_system.0002_technical_privileges` depende das migrations finais das bibliotecas fixadas e concede o inventário técnico explícito ao runtime PostgreSQL. Schema parcialmente protegido não é runtime aceitável; rode o check de schema após migrations.

## Catálogo versus registry

Registry é allowlist no código: modelo, campos, sensibilidade e exposição. Catálogo é inventário físico sincronizado: também inclui tabelas técnicas e desconhecidas. Alterar label/description do catálogo não ativa API, não cria tabela nem muda policy. `AppConfig.ready` registra metadados/checks, não consulta schema ou faz DDL.

Extensões têm `manifest.json` canônico, carregado como `AppManifest`, separado do registry de tabelas. `AppInstallation` guarda ativação/configuração organizacional auditada; nenhum metadado executa DDL. Rotas e menus declarados são integrados sob prefixes tenant reservados, antes do gateway; navegação e políticas revalidam ativação e permissão. O admin padrão é a única instância segura de plataforma, com autodiscover Django.

O core é uma distribuição wheel `django-skeleton`; `skeleton.conf` fornece defaults/profiles/URLs e `config` permanece overlay do derivado. `ComponentInstallation` e `DeploymentOperation` representam controle global, não ativação tenant. `/admin/system/releases/` solicita operações ao agente independente `deployment_agent`, que mantém journal privado, valida pacotes assinados, prepara releases imutáveis, faz backup/migrations/checks runtime e reinicia workers. A request web não executa pip/DDL nem recebe a credencial owner. Inventário privado determina apps gerenciadas no startup; apps locais continuam em `PROJECT_APPS`. Retirada de código conserva schema e migrations históricas sem carregar modelos/rotas de negócio. [Provisionamento e recuperação](deployment.md#agente-independente-de-deployment).


## Segurança: limites explícitos

- PostgreSQL: runtime não é owner/superuser/BYPASSRLS; owner offline e audit writer separados. RLS/FORCE protege filtros tenant esquecidos, não uma credencial runtime maliciosa capaz de `set_config`, nem SQL injection arbitrária.
- Django: papéis/visibilidade/soft delete não são feitos pela policy RLS. Superuser Django não pula requisitos nas rotas tenant.
- SQLite: triggers/integridade/auditoria e serviços autorizados existem; RLS e segurança contra leitura/DDL do arquivo não existem.
- Auditoria: append-only no runtime, não WORM contra DBA/migration owner.
- Redis: rate limiting distribuído em produção. LocMem não coordena workers.
- Sessão no banco + CSRF; sem JWT, BasicAuth, CORS aberto ou domínio de cookie wildcard.

Leia [controle de acesso](access-control.md), [deployment](deployment.md) e [testes](testing.md) para regras operacionais e evidência.
