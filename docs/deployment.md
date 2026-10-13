# Deployment, credenciais e transporte de dados

Este roteiro é para quem coloca a aplicação em funcionamento e precisa recuperá-la sem relaxar a segurança. **Deployment** é a instalação em execução (código, processos e serviços); **release** é um conjunto identificado de código e dependências preparado para essa instalação. Atualizar código não transporta dados nem torna uma migration reversível.

Antes de operar, leia [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados). Aqui os comandos usam caminhos de exemplo: substitua-os pelos caminhos provisionados, mantendo a separação de contas e credenciais.

### Quem executa o quê

| Termo | Significado e uso seguro |
|---|---|
| Credencial | Identidade de conexão e seu segredo. Não é a identidade do usuário Django. Nunca a publique em logs, Git ou argumentos de comandos. |
| DSN | Endereço de conexão, normalmente uma URL com banco, host e role; pode conter senha. `DATABASE_URL` é o DSN runtime; `MIGRATION_DATABASE_URL` é o DSN owner. |
| Schema | Espaço de nomes dentro do banco, aqui `skeleton`; não é outro banco. Contém tabelas, funções e infraestrutura de proteção. |
| Runtime | Processo que atende usuários, com `skeleton_runtime`; lê/grava somente o permitido, não modifica schema. |
| Owner | Role privilegiada `skeleton_owner`, usada offline para criar/alterar infraestrutura e recuperar dados. Não serve requests. |
| Migrator | Processo temporário que executa migrations com a credencial owner. Não é uma terceira role nem um worker web. |
| Operador/agente | Conta de sistema que prepara releases e coordena manutenção. Seus privilégios de supervisor/Docker não pertencem ao web. |

**Ordem segura:** provisione serviços e segredos → crie roles/schema → aplique migrations com owner → verifique com runtime → inicie web → confirme saúde e comportamento. Um check feito como owner não demonstra que o web está isolado.

## Settings e serviços

- `development`: SQLite local se DATABASE_URL ausente; email console, cache LocMem; PostgreSQL opcional.
- `production`: PostgreSQL, Redis compartilhado, SMTP, SECRET_KEY, hosts explícitos, remetente e chaves MFA obrigatórios; DEBUG=False. Cookies secure, HTTPS redirect/HSTS, password validators, CSRF origins explícitas. Live/ready são exceções ao redirect para healthcheck interno, sem inventário/segredos.
- `migrate`: conexão exclusivamente MIGRATION_DATABASE_URL; fallback SQLite existe apenas para setup local. Em produção declare URL owner explicitamente antes de executar.
- `test`: TEST_DATABASE_URL ou SQLite isolado; não apontar ao banco de produção.
- `build`: collectstatic com manifest storage sem exigir credenciais de produção. Não é settings para servir requests.

Docker usa Python 3.14-slim, bibliotecas XML/xmlsec, usuário não-root e Gunicorn WSGI. Collectstatic é parte do build; WhiteNoise segue SecurityMiddleware. Produção usa CompressedManifestStaticFilesStorage e STATIC_ROOT=staticfiles; dev usa runserver/staticfiles. Não inicie migrations em cada worker.

Compose fixa postgres:16.15, redis:8.8.3 e Keycloak 26.8.0 no perfil integrations. Postgres/Redis/Keycloak expõem somente loopback. Postgres admin e Redis exigem secrets de ambiente; web exige DSN runtime, nunca migrator. `${...:?}` de Compose pode exigir secrets web mesmo ao selecionar serviços: use arquivo de env privado completo. URL do web usa hostname `postgres`/`redis` dentro da rede Compose; no host use 127.0.0.1.

```sh
# .env privado contém todos os valores exigidos, nunca é versionado.
docker compose up -d postgres redis
# Após provisionar roles/schema e aplicar migrations/checks:
docker compose up -d --build web
```

Configure proxy HTTPS em frente ao web. Se ele termina TLS, habilite DJANGO_TRUST_PROXY=true somente se remover/substituir X-Forwarded-Proto recebido do cliente; não confie em cabeçalho público. Defina DJANGO_ALLOWED_HOSTS e DJANGO_CSRF_TRUSTED_ORIGINS com hosts/origens exatos. Compose encaminha essas opções explicitamente, com proxy trust false por padrão. Defina DJANGO_HEALTH_HOST como um host incluído na allowlist; o probe conecta a loopback e envia esse Host, sem wildcard. Não abra CORS/domínios de cookies wildcard. Para SSO no container, monte JSON privado read-only e injete SSO_PROVIDER_CONFIG_PATH + referências env; Compose base não provisiona IdP de produção.

Rate limiting de IP usa a topologia explícita `DJANGO_TRUSTED_PROXY_COUNT` (número de proxies confiáveis no X-Forwarded-For) **ou** `DJANGO_TRUSTED_CLIENT_IP_HEADER` (por exemplo X-Real-IP sobrescrito pelo proxy). Não configure ambos. Exigem DJANGO_TRUST_PROXY=true; defaults 0/sem header ignoram cabeçalhos forjados e usam REMOTE_ADDR. Bloqueie acesso direto ao backend quando confiar no proxy e remova/substitua cabeçalhos recebidos do cliente; confiar em TLS/proto não habilita automaticamente confiança em IP. Dois clientes atrás do proxy não devem compartilhar buckets de login/reset.


## Provisionamento PostgreSQL

**Pré-requisitos:** operador autorizado, banco novo vazio, cliente `psql` e DSN administrativo privado. O bootstrap prepara roles/schema, mas não cria o banco nem salva senhas. Não o trate como migração de uma instalação existente.

```sh
# ADMIN_DSN referencia banco recém-criado; segredo via pgpass/secret store.
psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -f deploy/postgres/bootstrap.sql
psql "$ADMIN_DSN"
```

No psql interativo, use `\password skeleton_owner` e `\password skeleton_runtime`; nunca senha na CLI/log/SQL versionado. Roles existentes também precisam ter atributos revisados pelo operador.

| Role | Privilégios/finalidade |
|---|---|
| skeleton_owner | LOGIN, NOSUPERUSER/NOCREATEDB/NOCREATEROLE, BYPASSRLS; ownership/schema/migrations offline |
| skeleton_runtime | LOGIN, NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOBYPASSRLS; sem ownership/membership privilegiada/CREATE/TRIGGER/TRUNCATE |
| skeleton_audit_writer | NOLOGIN/NOBYPASSRLS; dona das funções SECURITY DEFINER e INSERT audit, não escrita de negócio |

O banco usa schema `skeleton` e `search_path` `skeleton,public` (ordem de busca de nomes SQL). `CREATE` público é revogado. A distribuição de privilégios é deliberadamente restrita:

- Owner é membro de `audit_writer` para transferir funções offline; runtime nunca é membro.
- A migration concede `CREATE` no schema a `audit_writer` **temporariamente**, transfere a propriedade da função e revoga o privilégio na mesma migration. Não faça grant permanente nem regrant manual antes de `migrate`.
- Funções de auditoria usam `search_path` `pg_catalog,skeleton`, referências qualificadas e não concedem `EXECUTE` ao público/runtime. A função separada de verificação de lifecycle global permite `EXECUTE` runtime somente sob o controle de plataforma descrito em [controle de acesso](access-control.md).
- Os grants vêm do inventário congelado em migrations: tabelas próprias recebem `SELECT/INSERT/UPDATE`; `AuditEvent` permite somente `SELECT` runtime. `DELETE` físico não é concedido.
- Tabelas técnicas recebem só a escrita necessária ao framework. Nenhum grant genérico autoriza automaticamente tabelas futuras.

O inventário técnico está congelado em `sk_system.0002_technical_privileges`: migrations/contenttypes/auth permissions/groups e configuração/tokens sociais são somente SELECT; sessões, endereços/confirmações de email, vínculos sociais e autenticadores recebem o DML de lifecycle necessário; django_admin_log recebe SELECT/INSERT. Sequências acompanham apenas as tabelas mutáveis. Novas tabelas técnicas exigem migration e revisão explícita; o schema check compara também privilégios efetivos herdados e grant options.

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py migrate
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py sync_table_catalog
# Ambiente abaixo contém somente DATABASE_URL runtime e requisitos production:
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_skeleton_schema
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py validate_provider_bindings --database default
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check --deploy
```

Env dos dois primeiros inclui MIGRATION_DATABASE_URL owner; ambiente web não inclui essa variável/credencial. Execute bootstrap offline com settings.migrate e segredo interativo. Migration owner é privilegiado por definição; check com owner não prova RLS runtime.

**Resultado esperado:** migrations e catálogo concluídos pelo owner; os três checks seguintes passam usando a conexão runtime. **Se falhar:** não inicie web nem troque o check para owner para obter sucesso. Corrija o provisionamento ou a infraestrutura indicada e repita a etapa pertinente; não conceda privilégios adicionais ao runtime.

RLS ENABLE/FORCE e policies exatas isolam tenants; ausência de GUC não vê linhas, Membership discovery tem exceção SELECT própria. Flags Django não bypassam RLS. Credencial runtime/SQL injection arbitrária pode chamar set_config; essa é a fronteira de confiança, não defesa contra invasor com DSN. Confira [access-control](access-control.md).

## Health, email e operação

`/health/live/` só vivacidade; `/health/ready/` usa SELECT 1/cache, sem secrets/schema/grants. Somente esses paths exatos são atendidos antes de sessão/autenticação/contexto transacional; DB parado mantém live=200 e ready=503 JSON. Host continua validado; outros paths não recebem bypass. Health não substitui check de infraestrutura ou teste de isolamento. Proteja interfaces de operação na rede/proxy.

Leia as respostas como sinais diferentes: `live=200` significa que o processo responde; `ready=200` significa que banco/cache responderam ao probe. Nenhuma delas prova isolamento entre organizações, SMTP, login ou versão correta do código. Para releases gerenciados, confira também `/health/release/` e os checks descritos abaixo.

Redis compartilhado é necessário a rate limits entre workers; configure autenticação e rede privada. O probe usa REDISCLI_AUTH com expansão de ambiente, não senha literal/argv. SMTP entrega após commit de mensagens já materializadas e pré-validadas. Falha real é registrada por classe/UUID sem segredo e não vira 500 enganoso após dados confirmados: API recebe `X-Email-Delivery: failed`, HTML recebe aviso, e permanece um caminho explícito de reenvio. Convite reenviado revoga o antigo por tombstone e emite UUID/token novos na mesma transação, com revisão obrigatória; convite consumido não pode ser reenviado. Não há retry/outbox nem promessa de entrega distribuída/transacional.

Os logs da aplicação e do Django usam `SecurityJSONFormatter`, com um evento de conclusão `http.request` após resultado transacional/callbacks e UUID gerado pelo servidor, sem URL/body/cookies. Outcomes distinguem success, validation_error, conflict, access_denied, not_found e server_error; falha de entrega preserva o status de persistência e registra reason_code. status_code do Django é mapeado para status; error_class e target_sysid são seguros, sem mensagem/traceback bruto. O comando Gunicorn da imagem não habilita access log bruto. Proxy, ingress, APM e capturas de HTTP também precisam redigir caminhos/query de convite, reset e callbacks; não acrescente `--access-logfile -` sem formatter seguro. O backend de email console é somente desenvolvimento e imprime links privados por definição.

Backups PostgreSQL devem incluir entidades, audit e estado de migrations; proteja DSNs e chaves Fernet separadamente. Teste restore em DB vazio com mesmas versões/apps, check de schema e contagens/IDs. Backups de Authenticator exigem chaves antigas; não exclua chave só porque runtime novo deixou de usá-la. Banco/audit não é WORM contra DBA. Nunca use purge/flush/TRUNCATE no runtime protegido.

## Transportar SQLite existente para PostgreSQL — offline

**Trocar DATABASE_URL não migra dados.** O procedimento é manutenção explícita, primeiro ensaiado em cópia descartável. Não aponta a produção existente, não sobrescreve automaticamente tabelas. Não existe conversor automático do produto. Mantenha rollback SQLite intacto até aceite.

### 1. Congelar e inventariar

Objetivo desta etapa: obter uma fonte que não mudará durante a comparação e a carga. Sem parar **todos** os escritores (web, jobs e integrações), contagens e export podem descrever momentos diferentes. A cópia congelada é o ponto de retorno; não a reabra enquanto o destino estiver recebendo escritas.

Pare web/jobs/escritas; faça backup consistente do SQLite e dos arquivos privados/chaves. Registre commit, locks, apps habilitados, schema check, UUIDs/contagens por tabela própria (incluindo tombstones), relações e revision. Use o mesmo enable-example/PROJECT_APPS nos dois lados. Proteja export com `umask 077`.

Export por base manager (`--all`) é permitido **somente offline**, para incluir tombstones. Selecione todos os app labels próprios; inclua labels dos apps derivados. Técnicas escolhidas são apenas identidade necessária, nunca sessão/token social/confirmation pendente. Exemplo núcleo + notes:

```sh
umask 077
DJANGO_SETTINGS_MODULE=config.settings.development SKELETON_ENABLE_EXAMPLE_APP=true \
  .venv/bin/python manage.py dumpdata --all --indent 2 \
  sk_accounts sk_tenancy sk_access sk_system sk_audit example_notes \
  account.emailaddress socialaccount.socialaccount mfa.authenticator \
  --output /caminho/privado/sqlite-export.json
```

Não exporte django_session, admin_log, socialtoken, emailconfirmation, auth/contenttypes/migrations/site gerados. Preserve emails verificados, subjects e Authenticator cifrado; mantenha chaves Fernet. Reemita provas pendentes após cutover; login cria novas sessões. JSON contém password hashes/digests/dados privados: não versionar/enviar para logs.

### 2. Preparar destino novo

Operador cria DB **novo vazio** (por exemplo nome terminado `_skeleton_import`), provisiona roles/schema, configura MIGRATION_DATABASE_URL owner e aplica migrations completas com as mesmas apps. Sem runtime/workers ainda. O post_migrate cria catálogo/audit de bootstrap; não importe sobre usuários/dados reais.

Guard INSERT exige revision=1, portanto fixture com revisão histórica não pode ser carregada por runtime/loaddata normal. A manutenção offline precisa remover temporariamente **somente triggers USER das tabelas próprias** durante carga owner, preservando depois o conjunto exato. Isto é bypass privilegiado de importação, não feature HTTP. Audits históricos são preservados da fonte; não fabrique eventos create para todas as linhas importadas.

O bloco abaixo é um exemplo concreto de manutenção no banco novo; requer env `SKELETON_OFFLINE_IMPORT=1`, DSN owner e export privado. Revise labels/import antes de executar; nunca cole num shell com DSN de produção. A transação confirma que não existe usuário no destino, limita nomes de tabelas aos modelos próprios e exige sufixo do DB. O TRUNCATE só descarta catálogo/audits recém-gerados e tabelas técnicas vazias dependentes do banco **novo**, não dados de instalação existente.

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate SKELETON_OFFLINE_IMPORT=1 \
  .venv/bin/python manage.py shell <<'PY'
import os
from django.apps import apps
from django.core.management import call_command
from django.db import connection, transaction
from skeleton.common.models import SystemRecord
from skeleton.tenancy.context import system_context
assert os.environ.get('SKELETON_OFFLINE_IMPORT') == '1'
assert connection.vendor == 'postgresql'
assert connection.settings_dict['NAME'].endswith('_skeleton_import')
assert connection.settings_dict['USER'] == 'skeleton_owner'
User = apps.get_model('sk_accounts', 'User')
assert not User.all_objects.exists(), 'Destino não vazio: abortar, nunca sobrescrever'
models = [m for m in apps.get_models() if issubclass(m, SystemRecord) and not m._meta.proxy]
tables = sorted({m._meta.db_table for m in models})
q = connection.ops.quote_name
with system_context(reason='maintenance:sqlite-import'):
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'ALTER TABLE skeleton.{q(table)} DISABLE TRIGGER USER')
        cursor.execute('TRUNCATE ' + ','.join('skeleton.' + q(t) for t in tables) + ' CASCADE')
    call_command('loaddata', '/caminho/privado/sqlite-export.json', verbosity=0)
    with connection.cursor() as cursor:
        for table in tables:
            cursor.execute(f'ALTER TABLE skeleton.{q(table)} ENABLE TRIGGER USER')
    # Antes do sync: preservar UUID de catálogo e FKs de permissions/grants.
    Table = apps.get_model('sk_system', 'TableDefinition')
    for row in Table.all_objects.filter(database_alias='default', schema_name='main'):
        assert not Table.all_objects.filter(database_alias='default', schema_name='skeleton', db_table=row.db_table).exists(), 'Chave de catálogo ambígua: abortar'
        row.schema_name = 'skeleton'
        row.save(update_fields=['schema_name'])
PY
```

Há uma janela offline privilegiada sem guards/auditoria; mantenha destino inacessível e execute tudo num atomic (system_context). Qualquer erro aborta import e restaura DDL transacional. Se projeto tem signals técnicos que consultam tenant no raw load, revise-os para ignorar raw conforme contrato Django; não habilite runtime como workaround. Não desabilite FK/constraint permanente. SQLite pode conter violações legadas que PostgreSQL rejeita; corrija em cópia/serviços, não remova constraints para cutover.

### 3. Reconciliar, verificar e cortar

**Cutover** é o momento de trocar o destino usado pelos processos e liberar tráfego. Até esse momento, o PostgreSQL deve permanecer inacessível aos escritores normais. Execute primeiro a reconciliação com owner e depois a verificação com runtime; os ambientes são distintos mesmo quando os comandos aparecem no mesmo bloco.

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py sync_table_catalog
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_skeleton_schema
```

As atualizações main→skeleton no bloco de carga acontecem **com triggers já restaurados**, por save/CAS, preservam UUIDs de catálogo e geram audit legítimo de manutenção. Assim TablePermission/RecordGrant continuam apontando à mesma TableDefinition; sync pode reconciliar físicos/admin sem criar uma segunda identidade por tabela. Se a fonte contém entradas anteriores skeleton com mesma chave, o assert aborta: revise o inventário em cópia antes da importação, não escolha silenciosamente qual UUID descartar. Aliases adicionais exigem mapeamento explícito próprio. Builtin admin é reconciliado; permissões manuais precisam de comparação, e tabelas ausentes não devem continuar autorizadas.

Compare contagens e conjuntos de sysids próprios antes/depois (catálogo pode ganhar novas entradas, audit pode ganhar eventos legítimos de manutenção), revisions/timestamps/hashes, tombstones, memberships/roles/grants, vínculos sociais e IDs de audit. Confirme FK tenant/guards/RLS/roles runtime e testes A/B, login real/MFA/recovery/política de reset. Não imprima segredos ao comparar. Ajuste callbacks/domínio/proxy/SMTP; não copie sessões. Revogue versões de auth quando necessário via serviços de lifecycle.

Somente depois de aceite altere DATABASE_URL runtime do web e abra tráfego. Preserve backup/export/chaves em custódia; se falhar antes de abrir tráfego volte ao SQLite congelado. Depois de novas escritas no PostgreSQL, rollback exige reconciliação explícita, não simples troca de URL. Este runbook precisa ser ensaiado e verificado para os apps derivados; não é alegação de conversão já executada.

## Distribuição do core e adoção de projetos derivados

O `pyproject.toml` é a fonte única da versão do `django-skeleton`. O wheel contém somente `skeleton`, incluindo migrations históricas, templates/static, defaults reutilizáveis e inventário técnico. `config`, `applications`, `examples`, `deployment_agent`, `manage.py`, bancos e secrets não pertencem à distribuição. Os imports históricos e os labels `sk_accounts`, `sk_tenancy`, `sk_access`, `sk_system` e `sk_audit` permanecem estáveis.

Os entrypoints de `config/settings` importam `skeleton.conf.base` e aplicam os profiles de `skeleton.conf.profiles`; o overlay de negócio continua em `config/settings/project.py`. O núcleo calcula `BASE_DIR` pelo entrypoint do projeto ou por `SKELETON_PROJECT_ROOT`, nunca pelo diretório site-packages. Templates de branding podem ficar em `<projeto>/templates`; templates do core são descobertos pelos apps instalados. `config/urls.py` usa `skeleton.conf.urls.application_urls("config.urls_project")`, conservando o hook de rotas do derivado.

### Construir e publicar

**Pré-requisitos:** versão definida em `pyproject.toml`, dependências fixadas, manifesto revisado e chave privada de um publicador aprovado. **Publisher** é quem assina o artefato; aprovação da chave é decisão do operador, não algo que o próprio pacote possa conceder. A assinatura demonstra procedência e integridade, não ausência de código malicioso.

```sh
# Ambiente de build com requirements-dev.txt atualizado e hashes:
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python manage.py build_core_package \
  --wheel dist/django_skeleton-0.1.0-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-0.1.0.skapp
# Equivalente independente de settings/Django startup:
skeleton-core build --wheel dist/django_skeleton-0.1.0-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-0.1.0.skapp
```

O manifesto core identifica o artefato e limita como instalá-lo:

- Use `manifest_version=1`, `kind="core"` e `component_id="django-skeleton"`, com nome, descrição, versão e publisher.
- Declare intervalos explícitos de compatibilidade `skeleton/python/django`, dependências, conflitos opcionais e migrations (`leaves`, `from_versions`, labels opcionais).
- O arquivo de entrada usa `files=[]`, `python_dependencies` e lifecycle com `data_retention="retain"`, `maintenance=true`, `backup=true`.
- Declare o wheel principal em `python_dependencies`: `name="django-skeleton"`, versão, `wheel="wheels/<nome>.whl"` e `sha256`. O builder recalcula o hash e gera o inventário completo antes de assinar; a versão do wheel deve equivaler à do manifesto.
- `release_notes` e `build` registram instruções/notas e procedência. Declare wheels adicionais e forneça-os por `--dependency-wheel`; nunca use sdist (distribuição de fontes que exige build na instalação).

A assinatura segue o mesmo contrato e trust store dos pacotes de apps. O pacote não pode trazer uma segunda chave para autorizar a si mesmo.

O projeto derivado instala o wheel fixado em ambiente isolado e mantém um lock final com hashes para core, apps e dependências. O Dockerfile do template constrói esse wheel em stage separado, instala dependências dos locks com `--require-hashes`, instala o wheel já produzido sem resolver dependências e copia apenas os arquivos de projeto para o runtime. Não sobra uma cópia `skeleton/` em `/app` ocultando a distribuição instalada. A imagem do agente para releases gerenciados usa o lock final offline e wheels aprovados do release.

### Cutover de uma cópia do core

Faça backup e ensaie em clone offline antes de qualquer cutover. Identifique um release base **assinado e conhecido**; não use o HEAD corrente como evidência de equivalência de um projeto histórico. O **trust store** (lista de chaves confiáveis) é um JSON privado `publisher-key-id -> caminho PEM público` aprovado pelo operador. Ele contém referências a chaves públicas, não a chave privada de assinatura; uma chave fornecida pelo pacote não estabelece confiança.

```sh
skeleton-core adoption-plan --project-root /srv/projeto \
  --base-release /srv/artefatos/core-base.skapp \
  --trust-store /srv/privado/publishers.json
# Instale antes o wheel base verificado num virtualenv externo à cópia skeleton/.
# Converta customizações em applications/ e overlays de config sob revisão.
skeleton-core adopt --project-root /srv/projeto \
  --base-release /srv/artefatos/core-base.skapp \
  --trust-store /srv/privado/publishers.json --backup-root /srv/backups/core-adoption
```

**Resultado do plano:** `adoption-plan` compara cada arquivo da cópia com o inventário real do wheel no release verificado e lista adicionados/removidos/alterados. Qualquer diferença classifica a cópia como `fork` (derivação local modificada) e bloqueia sobrescrita/remoção. Sem base conhecida, não há promessa de upgrade automático.

**Condições para aplicar:** `adopt` exige uma distribuição instalada externa com versão **e todos os hashes** idênticos. Origem e backup devem estar no mesmo filesystem para mover a cópia inteira de `skeleton` por rename atômico. O backup conserva migrations históricas; `applications`, `config`, `manage.py`, dados e arquivos retidos não são alterados.

O comando Django `adopt_core_distribution` só produz o plano por padrão; o corte exige `--apply --backup-root ...`. Execute offline, com workers/jobs parados, e reinicie o processo no ambiente novo. Checks, migrations e smokes em clone com os mesmos IDs/dados continuam sendo critérios de aceite; não são efeitos implícitos de `adopt`.

### Inventário, manutenção e confirmação

Estes três registros respondem a perguntas distintas:

| Registro | Pergunta respondida | Limite |
|---|---|---|
| Geração | “Qual estado global este plano espera substituir?” É o número de estado do deployment, não a versão SemVer da app. | Uma geração antiga invalida o plano, mesmo que a versão pareça compatível. |
| Inventário | “Quais componentes, arquivos e históricos pertencem a este release?” É o JSON privado que orienta o startup. | Editá-lo não instala código nem prova que os workers carregaram os bytes declarados. |
| Journal | “O que a operação tentou, confirmou e precisa recuperar?” É o registro durável externo ao banco da aplicação. | Sobrevive ao restore do banco; não recria escritas de negócio perdidas. |

**Staging** é a preparação isolada de um novo release antes de ativá-lo. O release ativo permanece congelado; pacotes aprovados reconstituem o novo ambiente, em vez de mesclar código antigo e novo. Os detalhes a seguir explicam como o inventário determina o startup e como os probes confirmam o código realmente carregado.

`SKELETON_DEPLOYMENT_INVENTORY` aponta para o JSON privado gerado pelo agente antes do startup. Ele declara geração, core, apps installed/project-local/schema_retained, snapshots de schema e arquivos históricos de migrations. Settings não consultam banco para decidir `INSTALLED_APPS`: só apps `installed` são agregadas ao overlay; apps locais continuam em `PROJECT_APPS` e precisam ter manifesto/caminho coerentes com o inventário. Apps `schema_retained` não têm modelos, registry, menus ou URLs ativos. Os snapshots retidos ficam em `SKELETON_RETAINED_SCHEMA_PATHS`, preferencialmente com `path` e SHA-256.

Somente o profile `migrate` adiciona os arquivos históricos: packages isolados `archive_<label>` com `__init__.py` sem código, migrations originais byte-exact e nenhum `models.py`/`apps.py`. Um AppConfig técnico preserva o label original sem importar models/ready de negócio; `MIGRATION_MODULES` aponta para as migrations reais. O agente aprova o grafo oficial e os imports históricos antes de permitir retirada. Não existe placeholder para simular migration aplicada.

`SKELETON_MAINTENANCE_MARKER` é um arquivo externo controlado pelo operador/agente. Sua existência faz as requests de aplicação e admin responderem 503 antes de sessão/autenticação/contexto/DB; não libera uma view de status dependente do banco durante migrations. Live/ready mantêm seu contrato atual; `/health/release/` é GET/HEAD, valida Host, não consulta DB e informa somente geração, versão/origem e digest do core. Para um release gerenciado, cada worker confere uma vez no startup os hashes de `core.files` contra o wheel efetivamente carregado: versão/origem ou bytes divergentes retornam 503 `release_mismatch`, não sucesso por eco do inventário. Esses probes são internos e devem ficar restritos na rede/proxy.

A base source explicitamente adotada pode ser confirmada para recuperação somente com origem, versão e hashes iguais à base assinada; todo release preparado pelo agente usa origem `installed`. Staging não copia apps gerenciadas antigas para fazer merge: reconstitui seus arquivos a partir dos pacotes verificados. Só diretórios da nova cópia de projeto recebem permissão de escrita para preparação; o release ativo congelado permanece intacto.

O canal de controle independente usa `SKELETON_AGENT_SOCKET` e `SKELETON_AGENT_TOKEN_FILE`; o token/socket de API do agente não são o socket Docker nem credenciais de migration. `SKELETON_PACKAGE_MAX_BYTES` limita o upload web (default 128 MiB). O agente fornece status autenticado e limitado por operação durante manutenção; o web não recebe shell, escrita no release ativo ou credencial owner.

Compose inclui `migrator` no profile `migration`, com a mesma imagem do web e apenas `MIGRATION_DATABASE_URL` para a conexão owner; não há essa variável no serviço web. Para uso manual forneça o DSN privado explicitamente e execute `docker compose --profile migration run --rm migrator`. Não execute o job com DSN vazio. `SKELETON_RELEASE_METADATA_DIR` monta metadata read-only em `/run/skeleton/release`; inventário e marker precisam usar paths disponíveis no container. O agente gera overrides para imagem/path/env de cada release e roda migrator separado, em vez de alterar containers em execução. Secrets e estado mutável permanecem fora dos builds/releases.

## Agente independente de deployment

O executável é `skeleton-deployment-agent --config /caminho/agent.json <comando>`; no checkout, o equivalente é `.venv/bin/python -m deployment_agent --config /caminho/agent.json <comando>`. Ele **não** roda dentro do Gunicorn. Instale-o em virtualenv próprio, fora de `project_root`/`releases_root`, fixando a distribuição do agente e suas dependências. A partir dos artefatos previamente produzidos e aprovados:

```sh
python3.14 -m venv /opt/skeleton-agent/venv
# agent.lock é o lock com hashes da distribuição independente e dependências;
# o wheelhouse deve conter também o wheel do próprio agente.
/opt/skeleton-agent/venv/bin/python -m pip install --no-index \
  --only-binary=:all: --find-links /srv/agent-wheelhouse \
  --require-hashes -r /srv/agent.lock
```

O pacote independente é definido por `deployment_agent/pyproject.toml`; seu entrypoint não exige inicializar Django. A atualização do core gerenciado não atualiza esse virtualenv. O operador mantém `python`, `pg_dump`, `pg_restore`, `psql` e, para Compose, Docker disponíveis com versões compatíveis.

### Diretórios, secrets e configuração mínima

Use uma conta `skeleton-agent` separada da conta `skeleton-web`. O agente escreve somente estado privado, releases novos, ponteiro ativo e marker; o web lê o release e seus próprios secrets, sem acesso ao armazenamento privado/owner/Docker. O projeto inicial `/srv/skeleton/base` deve ter `manage.py`, `config`, `applications` e um `.venv` válido com a distribuição do core base **já instalada**. Sem uma cópia local `skeleton/`, a adoção compara todos os arquivos efetivamente instalados com o wheel assinado. Uma cópia source somente é aceita se seu inventário exato corresponder à base assinada; forks não são sobrescritos.

```sh
sudo install -d -o skeleton-agent -g skeleton-agent -m 0700 /srv/skeleton/agent-state
sudo install -d -o skeleton-agent -g skeleton-web -m 0750 \
  /srv/skeleton/releases /srv/skeleton/control /srv/skeleton/private
# PEM público aprovado, lock com hashes e wheelhouse são provisionados pelo operador.
# Config/env: 0600; token API e smoke: 0640 com grupo skeleton-web.
sudo chmod 0600 /srv/skeleton/private/agent.json /srv/skeleton/private/{runtime,migration,build}.env
sudo chmod 0640 /srv/skeleton/private/{agent,smoke}.token
```

Gere tokens ASCII independentes com pelo menos 32 caracteres (por exemplo `openssl rand -hex 32` com `umask 077` e destino privado). Não use a chave de assinatura de publisher como token de controle. O JSON mínimo do backend diretório é:

```json
{
  "backend": "directory",
  "storage_root": "/srv/skeleton/agent-state",
  "releases_root": "/srv/skeleton/releases",
  "project_root": "/srv/skeleton/base",
  "active_link": "/srv/skeleton/current",
  "socket_path": "/srv/skeleton/control/agent.sock",
  "token_file": "/srv/skeleton/private/agent.token",
  "runtime_env_file": "/srv/skeleton/private/runtime.env",
  "migration_env_file": "/srv/skeleton/private/migration.env",
  "build_env_file": "/srv/skeleton/private/build.env",
  "requirements_lock": "/srv/skeleton/requirements.txt",
  "wheelhouse": "/srv/skeleton/wheelhouse",
  "maintenance_marker": "/srv/skeleton/control/maintenance",
  "project_paths": ["manage.py", "config", "applications"],
  "approved_publishers": {
    "platform-release-key": "/srv/skeleton/private/platform-release-public.pem"
  },
  "commands": {
    "drain": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "skeleton-web.service"],
    "restart": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "skeleton-web.service"],
    "stop": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "skeleton-web.service"]
  },
  "health_urls": ["http://127.0.0.1:8000/health/live/", "http://127.0.0.1:8000/health/ready/"],
  "health_host": "app.example.org"
}
```

Todos os paths são absolutos; publisher IDs precisam corresponder aos manifests/índices assinados. `project_paths` é uma allowlist de overlays preservados: inclua `templates`, `examples` ou assets locais usados pelo derivado. Não inclua `.`, core, virtualenv, dados, secrets ou metadata gerada. `requirements_lock` tem pins `==` e SHA-256; disponibilize todos os wheels compatíveis, incluindo dependências transitivas e extras, em `wheelhouse`. O core selecionado vem de `.skapp` assinado; não use `pip` online no release. Um wheel faltante bloqueia o preview.

Cada env é texto `KEY=value`, uma variável por linha, com aspas opcionais; não use `export`, interpolação `$VAR`, substituições ou `source`. Exemplo de **estrutura** dos três arquivos; substitua os valores de credenciais pelo provisionamento PostgreSQL anterior:

```dotenv
# runtime.env — somente runtime, também usado pelo supervisor web
DJANGO_SETTINGS_MODULE=config.settings.production
DATABASE_URL=postgres://skeleton_runtime:SENHA_RUNTIME@127.0.0.1:5432/skeleton
DJANGO_SECRET_KEY=SEGREDO_DJANGO
DJANGO_ALLOWED_HOSTS=app.example.org
DJANGO_HEALTH_HOST=app.example.org
CACHE_URL=rediscache://:SENHA_REDIS@127.0.0.1:6379/0
EMAIL_URL=smtp://usuario:SENHA_SMTP@smtp.example.org:587
DEFAULT_FROM_EMAIL=app@example.org
MFA_ENCRYPTION_KEYS=CHAVE_FERNET_VALIDA
SKELETON_AGENT_SOCKET=/srv/skeleton/control/agent.sock
SKELETON_AGENT_TOKEN_FILE=/srv/skeleton/private/agent.token
SKELETON_DEPLOYMENT_INVENTORY=/srv/skeleton/current/inventory.json
SKELETON_MAINTENANCE_MARKER=/srv/skeleton/control/maintenance
SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE=/srv/skeleton/private/smoke.token
SKELETON_DEPLOYMENT_SMOKE_PATHS=/accounts/login/
```

```dotenv
# migration.env — nunca entregue ao web
DJANGO_SETTINGS_MODULE=config.settings.migrate
MIGRATION_DATABASE_URL=postgres://skeleton_owner:SENHA_OWNER@127.0.0.1:5432/skeleton
```

```dotenv
# build.env — sem credenciais do banco
DJANGO_SETTINGS_MODULE=config.settings.build
```

DSNs runtime e owner apontam para o **mesmo banco** com roles diferentes. URL-encode caracteres especiais de credenciais. Arquivos Fernet/SSO/uploads permanecem externos; monte-os/read-only quando necessário, e preserve as chaves antigas. O agente monta ambientes sem herdar env do seu daemon e injeta paths de release/inventário/static/archives; `MIGRATION_DATABASE_URL` em runtime ou build é rejeitada. SQLite só é aceito com `"allow_sqlite": true`, DSN absoluto `sqlite:////srv/skeleton/data/smoke.sqlite3` nos dois perfis e settings runtime de desenvolvimento; isso não constitui deployment de produção.

Opcionalmente acrescente `"smoke_token_file": "/srv/skeleton/private/smoke.token"` e URLs exatas da aplicação/login em `health_urls`. Em produção use URLs HTTPS do proxy (com trust de cabeçalhos corretamente provisionado), pois login HTTP redireciona. O token permite somente GET/HEAD dos paths explicitamente listados em `SKELETON_DEPLOYMENT_SMOKE_PATHS` durante manutenção; não autentica um usuário. Live/ready/release continuam internos. O smoke de login autenticado/SSO precisa de verificação operacional separada; uma página login 200 não prova autenticação.

### Supervisor e primeira adoção

O JSON acima usa comandos fixos do supervisor. Autorize em sudoers **somente** as ações e a unit exatas, não shell genérico. Se há jobs, `drain` deve parar também os workers e aguardar todas as escritas pendentes; o marker HTTP sozinho não bloqueia produtores externos. Hooks aceitam substituições `{release}`, `{active}`, `{operation}` em argumentos, nunca comandos vindos do pacote.

Exemplo de units provisionadas pelo operador (não pelos pacotes):

```ini
# skeleton-agent.service
[Unit]
After=network.target
[Service]
User=skeleton-agent
Group=skeleton-web
UMask=0077
ExecStart=/opt/skeleton-agent/venv/bin/skeleton-deployment-agent --config /srv/skeleton/private/agent.json serve
Restart=on-failure
[Install]
WantedBy=multi-user.target
```

```ini
# skeleton-web.service
[Unit]
After=network.target
[Service]
User=skeleton-web
Group=skeleton-web
WorkingDirectory=/srv/skeleton/current
EnvironmentFile=/srv/skeleton/private/runtime.env
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart=/srv/skeleton/current/.venv/bin/gunicorn config.wsgi:application --bind 127.0.0.1:8000 --workers 3
KillSignal=SIGTERM
TimeoutStopSec=1800
[Install]
WantedBy=multi-user.target
```

Mantenha a unit do agente fora dos serviços reiniciados/drenados. A primeira adoção e a recuperação são **offline**: pare o daemon do agente antes de executá-las. Prepare/provisione DB e aplique `migrate`, `sync_table_catalog` e `check_skeleton_schema` do release base como nas seções anteriores; `initialize` não cria banco nem aplica migrations. Para um projeto sem apps locais/gerenciadas, salve `{"apps":[]}` como `/srv/skeleton/private/initial-inventory.json`:

```sh
/opt/skeleton-agent/venv/bin/skeleton-deployment-agent \
  --config /srv/skeleton/private/agent.json initialize \
  --inventory /srv/skeleton/private/initial-inventory.json \
  --core-package /srv/skeleton/artifacts/core-base.skapp
sudo systemctl daemon-reload
sudo systemctl start skeleton-web.service skeleton-agent.service
```

Com apps existentes, cada entrada de `apps` declara o manifesto canônico completo e `state`. Para `installed`, informe `digest` SHA-256 do `.skapp` e repita `--app-package /caminho/app.skapp`: o código deve existir em `applications/<package_name>` com inventário **exato**, sem arquivos extras/divergentes. Para `project-local`, informe `path` relativo ao projeto (default `applications/<package_name>`), mantenha seu AppConfig em `PROJECT_APPS` e inclua sua raiz em `project_paths`; não é necessário um digest de artefato. Apps locais participam de compatibilidade/dependências, mas não podem ser substituídas/removidas por coincidência de ID. Adoção de `schema_retained` sem journal existente é recusada.

O socket é `0660`, no grupo do processo do agente; use `socket_gid` numérico se o grupo web for diferente. Só o grupo de controle pode ler o token e atravessar o diretório do socket. O status independente é opcional:

```json
{
  "status_http": {
    "host": "127.0.0.1",
    "port": 9081,
    "public_url": "https://app.example.org/deployment-status"
  }
}
```

Esses campos são adicionados ao JSON principal. O proxy encaminha **somente** `/deployment-status/` ao listener local, não ao Django; não registra query strings/tickets. Links `status_url` são read-only, vinculados a uma operação e expiram em cinco minutos. A API de controle Unix renova links; `revoke-status-tickets` revoga todos os tickets antigos. O listener não tem endpoints de execução/recuperação.

### Upload, plano aprovado e execução

**Pré-requisitos:** agente inicializado e ativo, pacote assinado por chave aprovada, geração conhecida e armazenamento privado para respostas. **Procedimento:** enviar artefato → examinar o plano → aprovar o vínculo exato de ID/digest/geração → acompanhar a operação. O digest é o resumo criptográfico que identifica bytes; copiar só a versão ou o nome do arquivo não identifica o pacote.

Com o daemon `serve` ativo, os demais comandos online usam sua API autenticada. Salve as respostas em diretório privado externo ao release; não edite um plano aprovado:

```sh
AGENT=/opt/skeleton-agent/venv/bin/skeleton-deployment-agent
CONFIG=/srv/skeleton/private/agent.json
"$AGENT" --config "$CONFIG" inventory
"$AGENT" --config "$CONFIG" upload /srv/skeleton/artifacts/notes-1.0.0.skapp
# Use o digest retornado por upload, não o hash de manifest.json.
"$AGENT" --config "$CONFIG" preview install --artifact "$ARTIFACT_DIGEST"
# Leia files/resources/migrations/impact e registre id, digest e generation do plano.
# OPERATION_UUID, ACTOR_UUID e REQUEST_UUID são UUIDs reais do operador/solicitação.
"$AGENT" --config "$CONFIG" submit --plan-id "$PLAN_UUID" \
  --plan-digest "$PLAN_DIGEST" --generation "$GENERATION" \
  --operation-id "$OPERATION_UUID" --actor-id "$ACTOR_UUID" --request-id "$REQUEST_UUID"
"$AGENT" --config "$CONFIG" operation "$OPERATION_UUID"
"$AGENT" --config "$CONFIG" journal
```

`submit` apenas entrega uma solicitação assíncrona. **Sucesso final** exige `state=succeeded`, `stage=ready` e geração/core reais em `/health/release/`; exit code de envio ou estado `queued` não bastam.

O vínculo aprovado reúne `plan_id`, `plan_digest`, `generation`, `operation_id`, `actor_id` e `request_id`. Reenviar exatamente esse vínculo retorna a mesma operação; trocar valores conflita. Se a confirmação de recebimento se perder, consulte o status da operação. Um reenvio explícito deve conservar os seis valores originais, sem novo preview, nova aprovação ou substituição pela geração atual. Na interface, o reenvio mantém o `request_id` lógico da aprovação; a nova request HTTP tem seu próprio identificador na auditoria. Sem vínculo original durável, consulte status/journal e falhe fechado: não invente outra aprovação.

Um plano/geração antigo é rejeitado para uma nova submissão. O preview prepara ambiente isolado e carrega o grafo Django real, mas não aplica migrations. Hashes e estado das migrations são novamente conferidos na execução e após drenar os escritores.

```sh
"$AGENT" --config "$CONFIG" preview update --artifact "$NEW_APP_DIGEST"
"$AGENT" --config "$CONFIG" preview uninstall --app-id notes
"$AGENT" --config "$CONFIG" preview reinstall --artifact "$REINSTALL_DIGEST"
"$AGENT" --config "$CONFIG" preview core_update --artifact "$NEW_CORE_DIGEST"
# Para atualização conjunta, repita --artifact; para retirada conjunta, --app-id.
# Cada preview exige novo submit com seu próprio id/digest/generation.
"$AGENT" --config "$CONFIG" check-updates
"$AGENT" --config "$CONFIG" fetch-release notes 1.1.0 platform-stable
```

IDs de app são os `app_id` dos manifests, não labels Django. Retirada não usa `migrate zero`: preserva schema, AppInstallation/configuração, ACLs/auditoria e o pacote assinado. As migrations históricas são copiadas byte-exact para archives sem modelos/rotas/hooks; apenas migrator carrega os labels. Preview rejeita imports de negócio retirado, histórico alterado e qualquer migration histórica retida ainda não aplicada. Reinstalação confere identidade/histórico/schema e deixa reativação organizacional explícita. Downgrade é recusado.

Atualização/reinstalação não transforma configuração de tenants: mudança de `runtime.configuration_fields` ou `runtime.default_enabled` é bloqueada com `configuration_upgrade_requires_review`. Publique um release com contrato compatível ou realize primeiro procedimento de evolução versionada revisado; o agente não substitui configuração persistida por defaults novos nem ativa organizações silenciosamente.

`catalogue_sources` é uma lista opcional de fontes fixas do operador, por exemplo `{"id":"platform-stable","path":"/srv/skeleton/catalog/index.json","channels":["stable"]}` ou `{"id":"platform-stable","url":"https://releases.example.org/index.json","channels":["stable"]}`. O índice é assinado, não expirado e monotônico; download HTTPS fica na mesma origem, sem redirects. Sem fontes configuradas a consulta retorna listas vazias, não uma alegação de atualização. Estados `available`, `incompatible`, `current` e source `unavailable` são distintos.

API equivalente pelo socket (todas as chamadas exigem `Authorization: Bearer <token>`; POST JSON usa `Content-Type: application/json` e Content-Length):

| Método/path | Corpo |
|---|---|
| GET `/inventory` | nenhum |
| GET `/operations/<uuid>` | nenhum; inclui status_url quando habilitado |
| POST `/uploads` | bytes `.skapp`, `application/octet-stream` |
| POST `/preview` | `{"action":"install","artifacts":["<sha256>"],"app_ids":[]}` |
| POST `/submit` | `{"plan_id":"<uuid>","plan_digest":"<sha256>","generation":0,"operation_id":"<uuid>","actor_id":"<uuid>","request_id":"<uuid>"}` |
| POST `/status-ticket` | `{"operation_id":"<uuid>"}`; permite emitir link antes de submit |
| POST `/catalog/check` | `{}` |
| POST `/catalog/fetch` | `{"component_id":"notes","version":"1.1.0","source_id":"platform-stable"}` |

Não existe API HTTP de `initialize` ou `recover`. Para curl, provisione arquivo privado `control-header` com a linha `Authorization: Bearer ...` (0600), evitando token em argv/history:

```sh
curl --unix-socket /srv/skeleton/control/agent.sock \
  --header @/srv/skeleton/private/control-header http://localhost/inventory
curl --unix-socket /srv/skeleton/control/agent.sock \
  --header @/srv/skeleton/private/control-header \
  --header 'Content-Type: application/octet-stream' \
  --data-binary @/srv/skeleton/artifacts/notes-1.0.0.skapp http://localhost/uploads
curl --unix-socket /srv/skeleton/control/agent.sock \
  --header @/srv/skeleton/private/control-header --header 'Content-Type: application/json' \
  --data-binary @/srv/skeleton/private/preview-request.json http://localhost/preview
```

### Backend Compose

O backend **diretório** prepara um virtualenv por release, troca o ponteiro `current` e usa o supervisor configurado. O backend **Compose** prepara uma imagem por release, roda um container migrator separado e recria o serviço web. São duas formas de executar o mesmo contrato, não duas evidências equivalentes: um ensaio bem-sucedido em diretório não valida Docker, permissões de volumes ou recuperação Compose.

**Pré-requisitos adicionais:** Docker acessível pela conta do agente, Compose e imagem base provisionados, serviços/volumes privados e endpoints de backup acessíveis no host. A configuração abaixo é um exemplo a adaptar; não é prova de que a stack foi executada.

Troque `backend` para `compose` e adicione ao mesmo JSON:

```json
{
  "compose": {
    "file": "/srv/skeleton/compose.yaml",
    "env_file": "/srv/skeleton/private/compose.env",
    "dockerfile": "/opt/skeleton-agent/deploy/agent/Dockerfile",
    "project": "skeleton",
    "web_service": "web",
    "migrator_service": "migrator",
    "base_image": "skeleton-python-xmlsec:3.14",
    "image_repository": "skeleton-managed",
    "runtime_uid": 10001,
    "migration_uid": 1000,
    "target_environment": {
      "python_full_version": "3.14.0",
      "python_version": "3.14",
      "platform_machine": "x86_64",
      "sys_platform": "linux",
      "implementation_name": "cpython"
    }
  },
  "backup_env_file": "/srv/skeleton/private/backup.env"
}
```

`target_environment` deve refletir o Python/platform **reais** da imagem base provisionada, não copiar o exemplo sem conferir. A imagem base já contém as bibliotecas nativas XML/xmlsec dos wheels SAML; ela é acessível localmente e não contém código/secrets do projeto. `RELEASE_BASE` é passado ao Dockerfile; builds de release usam `--network=none` e wheels/lock aprovados. O agente prepara imagem provisória para checks/preflight, depois sela a imagem selecionada com static e metadata finais. Não extrai código num container em execução.

`compose.env` contém os valores necessários à interpolação do Compose inteiro (inclusive PostgreSQL/Redis/Keycloak quando `${...:?}` exigir), mas o agente limpa as variáveis herdadas dos serviços e aplica **somente** o perfil privado escolhido. Runtime nunca recebe DSN owner; migrator é um job separado. `migration_uid` deve ser o UID numérico do agente no host, pois precisa escrever resultados de probes no diretório 0700 montado. As raízes release/marker/probes/operations usam o mesmo path absoluto dentro e fora do container; não mude os paths só num dos ambientes.

Em runtime/migration envs, use DSNs acessíveis ao container (`postgres`, `redis` da rede Compose, por exemplo). `pg_dump`/`pg_restore`/`psql` são executados **no host**: se os nomes internos não resolvem nele, `backup.env` contém `MIGRATION_DATABASE_URL` owner com endpoint publicado em loopback do **mesmo banco/role**. O agente compara banco/usuário; o operador confirma que ambos endpoints chegam à mesma instância. Esse segredo não é passado a containers. Sem `backup_env_file`, os utilitários usam o DSN de migration.

O Compose base deve montar secrets runtime/SSO/smoke e socket de controle read-only, dados externos nos paths de runtime necessários e jamais owner/Docker no web. Esses binds fixos são provisionados no Compose, não no pacote. Garanta que UID 10001/grupo do web lê secrets e atravessa o socket/marker. Para inicialização de base existente, sua imagem com os wheels verificados deve estar disponível na tag `skeleton-managed:<nome-do-diretório-base>` (neste exemplo `skeleton-managed:base`).

O backend já faz `stop web` no drain e `up --detach --no-deps --no-build --force-recreate web` no restart. Configure hooks fixos reais para drenar/parar/reabrir jobs/proxy externos; se só há web, hooks podem operar o gate do proxy. Não use hooks de systemd do exemplo diretório para serviços inexistentes nem comandos fictícios/no-op. O agente precisa de acesso Docker delimitado operacionalmente (socket Docker equivale a privilégios elevados); isso nunca é concedido ao web.

### Backup, crash e recuperação

**Backup** é a cópia consistente de banco e recursos necessários; **restore** é a reposição dessa cópia; **rollback** é retornar a um estado anterior. Só trocar arquivos ou o ponteiro de release não desfaz migrations ou escritas. A indisponibilidade planejada começa quando o tráfego é bloqueado e os escritores são drenados, e termina somente após verificar banco, release e workers.

Antes de escolher recuperação, identifique no journal a primeira migration e a intenção de reabrir tráfego:

| Momento da falha | Ação segura e limite |
|---|---|
| Antes de iniciar migrations | `abort` pode verificar e reiniciar o release anterior. Não executar os outros caminhos em sequência. |
| Depois de iniciar migrations, ainda sem novas escritas | `restore` requer backup íntegro; alternativamente `complete` conclui somente o plano aprovado. Voltar só o código não basta. |
| Após intenção de liberar tráfego | Trate novas escritas como possíveis, mesmo sem confirmação de sucesso. Restore pode perdê-las; aprove antes um plano explícito de perda/reconciliação. |

**Resultado esperado:** recuperação confirmada por checks runtime e saúde do release, seguida da liberação controlada de tráfego. **Se falhar:** mantenha manutenção e evidências; não remova o marker manualmente, não habilite runtime privilegiado e não invente nós de migration.

**Barreira antes de recuperar:** confirme que o release/geração ativos ainda correspondem à origem ou ao destino da aprovação escolhida. Uma operação antiga não pode desfazer um deployment posterior: `operation_superseded` recusa esse caso antes de manutenção, drain ou restore. Se existir outra operação `queued`, `running` ou `interrupted`, resolva seu estado primeiro; `recovery_or_operation_pending` impede recuperar em paralelo. Não contorne essas recusas alterando inventário/journal nem escolhendo outra operação sem examinar seu vínculo.

Adicione `recovery_paths`, por exemplo `["/srv/skeleton/uploads","/srv/skeleton/private/mfa-keys.json"]`, para recursos mutáveis/chaves que precisam acompanhar o DB. Eles devem existir, ser externos a releases e não conter symlinks. Pare todos os escritores antes do snapshot. Não inclua diretório de estado/journal do agente, o marker ou o próprio backup em `recovery_paths`.

Antes da primeira migration, o journal registra `backup` apontando para `agent-state/backups/.../backup.json`: PostgreSQL custom dump do schema `skeleton` inteiro (negócio, tabelas técnicas, migrations e auditoria) ou SQLite online backup, hashes e snapshots exatos dos recursos. Extensões/roles PostgreSQL são provisionadas separadamente; dados fora de `skeleton` exigem procedimento offline específico. Um backup parcial de crash é preservado; uma nova tentativa usa outro diretório. Backup/journal/uploads/plans permanecem privados e fora do DB da aplicação. Nunca apague o release anterior, os artefatos aprovados ou esses snapshots enquanto houver recuperação/retention pendente.

Falha/crash sob manutenção mantém o marker; ao reiniciar `serve`, queued/running/commit interrompidos tornam-se `interrupted`. **Não remova o marker à mão para esconder divergência.** Pare o daemon, consulte `journal`, escolha a operação e execute como conta do agente:

```sh
sudo systemctl stop skeleton-agent.service
"$AGENT" --config "$CONFIG" journal
# Antes de qualquer migration: abort verifica/reinicia o release anterior.
"$AGENT" --config "$CONFIG" recover "$OPERATION_UUID" abort
# Depois de migrations: restore repõe DB + recursos + release anterior.
"$AGENT" --config "$CONFIG" recover "$OPERATION_UUID" restore
# Alternativa revisada pelo operador: reaplica somente migrations restantes
# do plano aprovado, checks runtime, cutover e confirmação de workers.
"$AGENT" --config "$CONFIG" recover "$OPERATION_UUID" complete
sudo systemctl start skeleton-agent.service
```

São alternativas, não três comandos em sequência. `abort` é recusado depois do início de migrations; `restore` exige backup íntegro. Restore PostgreSQL **remove o schema `skeleton` com CASCADE**, depois restaura o dump, reinstala a infraestrutura congelada e valida com runtime antes de abrir tráfego: isso também elimina objetos criados pela tentativa que não existiam no backup. Projetos com dados em outros schemas precisam de procedimento offline revisado e ensaiado; não presumir restauração exata fora do schema suportado. Restore de recursos substitui a árvore inteira, preservando a árvore deslocada como evidência.

Após intenção de liberar tráfego, `restore`/`abort` exigem `--acknowledge-new-writes`: essa flag registra decisão explícita do operador, **não** reconcilia escritas nem impede perda de dados. Só use depois de plano de perda/reconciliação aprovado. `complete` valida o grafo restante contra a aprovação; não permite comandos arbitrários nem cria nós fake. Se falhar novamente, manutenção/journal permanecem disponíveis para nova intervenção. Faça o smoke install → update → uninstall → reinstall → core_update e ensaie restore em clone com PostgreSQL runtime e, separadamente, Compose real; esta documentação não afirma que esses checks foram executados.
