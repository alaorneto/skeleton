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

### Core/agente 0.5.0 e apps fechadas por padrão

No core 0.5.0, uma app instalada nasce indisponível e desativada: sem linha de disponibilidade nenhuma organização a usa, e sem `AppInstallation` ela está desativada (#22). **Passo obrigatório do upgrade:** rode `manage.py upgrade_app_activation` depois das migrations e de `sync_table_catalog`; sem ele, as apps somem para todas as organizações (falha fechada). O agente 0.5.0 faz isso sozinho no `finish()`, sob a credencial runtime, quando o core alvo é >=0.5.0; num deploy manual, rode o comando antes de liberar tráfego (`--dry-run` mostra o que vai mudar). O comando é idempotente e retomável; repetir em todo deploy é seguro. A recusa `activation_default_upgrade_requires_review` saiu do agente, porque mudar `runtime.default_enabled` não ativa mais nada. **Rollback:** um release anterior volta a usar o `default_enabled`; as linhas criadas pelo upgrade são compatíveis com ele.

### Core/agente 0.4.0 e API de serviços

O core 0.4.0 é o primeiro que lê `runtime.service_api` (docs/service-api.md). Atualize o agente independente para 0.4.0 (ele fixa `django-skeleton==0.4.0`) e o core antes de instalar apps que declarem o campo: um core 0.3.0 recusa o manifesto por campo desconhecido, e o piso `>=0.4.0` da compatibilidade impede a instalação. Apps já instaladas sem o campo continuam carregando, com o aviso `skeleton.W018`; um release novo delas precisa declarar a API de serviços, porque `package build` e `package doctor` recusam a ausência. O release não muda schema nem migrations do núcleo.

### Core/agente 0.3.0 e contratos de apresentação

Atualize primeiro o agente independente para 0.3.0 e prepare core >=0.3.0 antes de publicar/consumir dimensões de campos. O core distribui `django-crispy-forms==2.7` e o pack SSR próprio `skeleton_crud`; inclua a dependência no lock/wheelhouse aprovado do release, sem download pelo web ou fallback ao renderer antigo. Novas publicações usam `manifest_version=2`, com compatibilidade inicial `>=0.3.0,<1.0.0`. O piso histórico v2 sem dimensões permanece 0.2.0: qualquer presença de `col_span` ou `rows` no campo, inclusive `null`, exige piso 0.3.0 na fonte. Pacotes v1/v2 históricos assinados continuam verificáveis sem normalizar JSON ou alterar bytes/digest/inventário. O [contrato de interfaces](application-admin-ui-plan.md) descreve grade de 12 unidades, dimensões e defaults e separa a administração da plataforma (`/platform/`) do viewer tenant; nenhum layout concede acesso ou modifica autorização/CAS/persistência.

Preview e aprovação incluem `presentation_changes`: grupos/seções/telas, layouts e campos de configuração. Alterar somente span/rows gera `model_layout/change`, sem incompatibilidade de configuração, e integra o digest do plano. Alterações incompatíveis ficam explícitas. Reordenação/rótulos não apagam configuração; remoção/renomeação/tipo/limite precisam de migration histórica que conserve o estado válido. Após migrations e antes de liberar tráfego, o agente executa `check_application_configuration` sob runtime, além dos checks existentes. O comando percorre organizações em transações independentes, não imprime valores/segredos e recusa estado persistido incompatível. Falha conserva manutenção e journal para recuperação; não há coerção ou descarte escolhido por HTTP. `runtime.default_enabled` é obsoleto e ignorado desde o core 0.5.0; mudá-lo não ativa tenants e não é recusado.

```sh
# Depois de migrations owner, no ambiente com somente DATABASE_URL runtime:
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_application_configuration
```

## Settings e serviços

- `development`: PostgreSQL (`DATABASE_URL`, ou `tools/dev_postgres.py` para um banco local descartável), email console, cache LocMem. Sem configuração, o `runserver` abre o [setup](#primeira-instalação-pelo-setup-146).
- `production`: PostgreSQL, Redis compartilhado, SMTP, SECRET_KEY, hosts explícitos, remetente e chaves MFA obrigatórios; DEBUG=False. Cookies secure, HTTPS redirect/HSTS, password validators, CSRF origins explícitas. Live/ready são exceções ao redirect para healthcheck interno, sem inventário/segredos.
- `migrate`: conexão exclusivamente `MIGRATION_DATABASE_URL` (PostgreSQL, role owner), só no ambiente; não há fallback.
- `test`: `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) de um PostgreSQL descartável; nunca o banco de produção.
- `build`: collectstatic com manifest storage sem exigir credenciais de produção. Não é settings para servir requests.
- `setup`: o [primeiro acesso](#primeira-instalação-pelo-setup-146), sem banco.
- **Só PostgreSQL (#144):** todos os dados (usuários, negócio, auditoria, eventos, jobs, notificações, anexos) vivem no PostgreSQL. O check `skeleton.E025` reprova qualquer outro backend em `DATABASES` e um `DATABASES` vazio fora do setup; as migrations de infraestrutura falham explicitamente fora do PostgreSQL. O SQLite da instância guarda só configuração e o estado do setup.
- Arquivos: `STORAGES["skeleton_files"]` é privado em todos os perfis. Em produção, defina `SKELETON_FILES_ROOT` fora de `STATIC_ROOT` num volume persistente, ou `SKELETON_FILES_BACKEND=s3` com o extra `[s3]` e um bucket sem acesso público. `check --deploy` (E024) recusa armazenamento público ou ausente. Para entregar downloads pelo proxy, use `SKELETON_FILES_ACCEL=nginx` com uma `location` **interna** (`internal;`) em `SKELETON_FILES_ACCEL_PREFIX` apontando para a raiz privada, ou `SKELETON_FILES_ACCEL=sendfile` no Apache; o Django continua autorizando cada pedido. Veja [arquivos](files.md#download).

### Configuração da instância (#143)

As chaves do catálogo (`skeleton/instance/schema.py`: `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CACHE_URL`, `EMAIL_URL`, chaves MFA e de provedores, limites e armazenamento de arquivos) são resolvidas com a precedência **ambiente > SQLite da instância > default**. O `.env` lido na partida conta como ambiente. Chave fora do catálogo é recusada.

- `SKELETON_INSTANCE_DB` (padrão `var/instance/instance.sqlite3`) e `SKELETON_INSTANCE_KEY_FILE` (padrão `var/instance/master.key`) são variáveis de bootstrap e ficam só no ambiente, como `DJANGO_SETTINGS_MODULE`, o inventário e os caminhos do agente e o marcador de manutenção. A credencial owner (`MIGRATION_DATABASE_URL`) nunca é gravada.
- `manage.py instance set <chave> <valor>` valida e grava; use `--stdin` para segredos, que ficam fora do histórico do shell. `instance unset` remove. `instance show` lista cada chave com a origem efetiva (`env`, `instance` ou `default`) e mascara segredos; `instance status` mostra caminhos, versão do schema e o que falta para o setup; `instance export-template` imprime um `.env` comentado, sem valores.
- **Migração de uma instalação existente:** ela continua funcionando só com o ambiente, porque o ambiente vence. `manage.py instance import-env` grava no SQLite as chaves do catálogo presentes no ambiente atual (`--keep-existing` preserva as já gravadas); depois, remova-as do ambiente.
- **Chave mestra:** gerada na primeira gravação de um segredo, com permissão 0600 (o SQLite também é criado 0600), num arquivo separado do SQLite. Sem ela, ou com outra, a leitura de um segredo falha com erro explícito na partida, sem cair para o default. **Faça backup da chave mestra separado do SQLite:** perdê-la torna os segredos ilegíveis, e eles terão de ser gravados de novo.

### Primeira instalação pelo setup (#146)

Uma instância nova não precisa de `.env` nem de comandos: enquanto faltar alguma chave exigida (`secret_key`, `allowed_hosts`, `database_url`) no ambiente e no SQLite da instância, o WSGI (`config.wsgi`, perfil de produção) e o `manage.py runserver` sobem em **modo setup** (`config.settings.setup`). Nesse modo não há alias de banco, a sessão é um cookie assinado com uma chave derivada da chave mestra, e só `/setup/` e as sondas de saúde respondem; qualquer outra rota leva a `/setup/`. `SKELETON_SETUP=off` desliga a troca (ferramentas e instâncias configuradas por outro meio).

1. **Token:** no primeiro start, o processo gera um token de uso único (24 h), grava só o SHA-256 no SQLite, imprime o token uma vez no log e o escreve em `setup-token` (0600, ao lado do SQLite). Token errado, reutilizado ou expirado é recusado (403), com limite de 5 tentativas por IP em 15 minutos (429). Um reinício sem token aberto gera outro.
2. **Instância:** hosts, origens CSRF, proxy e origem pública. O setup gera `SECRET_KEY` e os keyrings MFA, de provedores e de integração, cifrados no SQLite e nunca exibidos.
3. **Banco runtime:** a URL da role runtime é recusada se for superuser, tiver `BYPASSRLS`, for dona do schema `skeleton` ou tiver `CREATE` nele, ou se o schema não existir (provisione com `deploy/postgres/bootstrap.sql`).
4. **Schema:** a credencial owner (dona do schema, mesmo banco, outra role) roda `migrate`, `sync_table_catalog` e o check do schema **só na memória dessa requisição**: ela não vai para o SQLite, o log nem a sessão.
5. **Email** (com envio de teste), **cache e arquivos** (validações do E024 e pasta gravável) e **administrador e organização** (`bootstrap_identity`, senha pelos validadores do Django).
6. **Conclusão:** marca o setup como concluído (`/setup/` passa a responder 404) e pede o reinício: `SKELETON_SETUP_RESTART=exit` encerra o processo para a política de reinício do container, o Gunicorn recebe `SIGHUP` e o `runserver` recarrega. Sem supervisor conhecido, a página pede para reiniciar o serviço.

Todas as telas são formulários comuns com CSRF, sem JavaScript. Em modo setup, `SKELETON_SETUP_ALLOWED_HOSTS` (padrão `localhost,127.0.0.1`) define os hosts aceitos e `SKELETON_SETUP_SECURE_COOKIES` controla os cookies secure (padrão: ligado fora de `DJANGO_DEBUG`).

### Alterar a configuração depois do setup (#147)

`/platform/instance/` lista cada chave do catálogo com a origem efetiva (ambiente, SQLite da instância, padrão) e o valor; segredos aparecem só como definidos ou não. Só o administrador da plataforma acessa (403 para os demais, MFA quando configurada), e cada alteração exige autenticação recente. Chaves vindas do ambiente ficam somente leitura (409 ao tentar gravar). A URL do banco passa pela mesma validação de privilégios do setup. Cada mudança entra no `change_log` do SQLite (chave, ação e `platform:<usuário>`) e gera o evento de segurança `instance.setting_changed`, sempre sem o valor. As mudanças valem depois do reinício; o botão "Pedir reinício" usa o mesmo mecanismo da conclusão do setup.

Docker usa Python 3.14-slim, bibliotecas XML/xmlsec, usuário não-root e Gunicorn WSGI. Collectstatic é parte do build; WhiteNoise segue SecurityMiddleware. Produção usa CompressedManifestStaticFilesStorage e STATIC_ROOT=staticfiles; dev usa runserver/staticfiles. Não inicie migrations em cada worker.

Compose fixa postgres:16.15, redis:8.8.3 e Keycloak 26.8.0 no perfil integrations. Postgres/Redis/Keycloak expõem somente loopback. Postgres admin e Redis exigem secrets de ambiente. O `web` (#148) recebe só variáveis de bootstrap: o SQLite da instância e a chave mestra ficam no volume nomeado privado `instance` (`/instance/instance.sqlite3`, `/instance/keys/master.key`), preservado ao recriar o container. Numa instalação nova, `docker compose up` sobe em modo setup (o token aparece em `docker compose logs web`). Depois do setup, `SKELETON_SETUP_RESTART=exit` encerra o processo e `restart: unless-stopped` o traz de volta em modo normal. O healthcheck (`python -m skeleton.api.healthcheck`) descobre sozinho um Host aceito, nos dois modos. Sobrescritas pontuais da configuração vão no arquivo opcional `.env.instance` (`SKELETON_INSTANCE_ENV_FILE`), que vence o SQLite. O serviço `migrator` (perfil `migration`) continua opcional: o setup migra na primeira vez e o agente migra nas atualizações com a credencial owner dele, que nunca chega ao `web`. Para uma instalação existente configurada por variáveis, copie-as para `.env.instance` ou rode `manage.py instance import-env` dentro do container e remova-as do ambiente. URL do web usa hostname `postgres`/`redis` dentro da rede Compose; no host use 127.0.0.1.

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

O bootstrap cria a extensão `pg_trgm` quando ela está disponível ([#181](https://github.com/alaorneto/skeleton/issues/181)). Ela é opcional e confiável a partir do PostgreSQL 13. Com ela, o picker tolera erro de digitação; sem ela, procura só por prefixo. A busca textual das listas não precisa de extensão: a função `skeleton.immutable_unaccent` vem da migration `sk_system.0008`. Numa instância que já existe, rode de novo o bloco do `pg_trgm` do bootstrap como operador e depois `manage.py migrate`. O índice do picker é criado no tipo que a extensão permite no momento da migration.

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

`validate_provider_bindings` confere também os provedores do banco (#29): fingerprint recalculado, organização ativa, segredos decifráveis com as chaves atuais e papel padrão. Para migrar o JSON, rode `manage.py import_identity_providers --from-config <arquivo> --dry-run`, depois sem `--dry-run`, confira os avisos de transição do preflight e só então retire as entradas `oidc`/`saml` do arquivo ([provedores no banco](authentication-sso.md#provedores-no-banco-29)).

Env dos dois primeiros inclui MIGRATION_DATABASE_URL owner; ambiente web não inclui essa variável/credencial. Execute bootstrap offline com settings.migrate e segredo interativo. Migration owner é privilegiado por definição; check com owner não prova RLS runtime.

**Resultado esperado:** migrations e catálogo concluídos pelo owner; os três checks seguintes passam usando a conexão runtime. **Se falhar:** não inicie web nem troque o check para owner para obter sucesso. Corrija o provisionamento ou a infraestrutura indicada e repita a etapa pertinente; não conceda privilégios adicionais ao runtime.

RLS ENABLE/FORCE e policies exatas isolam tenants; ausência de GUC não vê linhas, Membership discovery tem exceção SELECT própria. Flags Django não bypassam RLS. Credencial runtime/SQL injection arbitrária pode chamar set_config; essa é a fronteira de confiança, não defesa contra invasor com DSN. Confira [access-control](access-control.md).

## Health, email e operação

`/health/live/` só vivacidade; `/health/ready/` usa SELECT 1/cache, sem secrets/schema/grants. Somente esses paths exatos são atendidos antes de sessão/autenticação/contexto transacional; DB parado mantém live=200 e ready=503 JSON. Host continua validado; outros paths não recebem bypass. Health não substitui check de infraestrutura ou teste de isolamento. Proteja interfaces de operação na rede/proxy.

Leia as respostas como sinais diferentes: `live=200` significa que o processo responde; `ready=200` significa que banco/cache responderam ao probe. Nenhuma delas prova isolamento entre organizações, SMTP, login ou versão correta do código. Para releases gerenciados, confira também `/health/release/` e os checks descritos abaixo.

Redis compartilhado é necessário a rate limits entre workers; configure autenticação e rede privada. O probe usa REDISCLI_AUTH com expansão de ambiente, não senha literal/argv. SMTP entrega após commit de mensagens já materializadas e pré-validadas. Falha real é registrada por classe/UUID sem segredo e não vira 500 enganoso após dados confirmados: API recebe `X-Email-Delivery: failed`, HTML recebe aviso, e permanece um caminho explícito de reenvio. Convite reenviado revoga o antigo por tombstone e emite UUID/token novos na mesma transação, com revisão obrigatória; convite consumido não pode ser reenviado. Não há retry/outbox nem promessa de entrega distribuída/transacional. Isso vale para emails com token (allauth e convites). Os emails de **notificação** são outra coisa: saem por job (`notifications.email`), com retentativa e estado em `NotificationDelivery`, e por isso exigem um worker (`manage.py run_worker`) e o agendador de jobs periódicos rodando; sem worker, ficam na fila. `SKELETON_DEFAULT_LANGUAGE` (padrão `pt-br`) e `SKELETON_DEFAULT_TIMEZONE` (padrão `UTC`) ficam no catálogo da instância e valem para quem não escolheu um idioma ou fuso no perfil, nem tem um padrão na organização ([#170](https://github.com/alaorneto/skeleton/issues/170)). A exportação CSV ([exportação e importação](data-transfer.md)) roda no worker e respeita `SKELETON_EXPORT_MAX_ROWS` (padrão 100.000), `SKELETON_IMPORT_MAX_ROWS` (padrão 10.000) e `SKELETON_TRANSFER_RETENTION_DAYS` (padrão 7), todos no catálogo da instância. Defina `SKELETON_PUBLIC_URL` (a origem pública, como `https://app.example.com`) para que os links desses emails sejam absolutos. A mesma origem vale para os convites emitidos sem requisição (por um job ou comando): sem ela, o convite é recusado com um erro que cita `SKELETON_PUBLIC_URL` ([#169](https://github.com/alaorneto/skeleton/issues/169)). Veja [notificações](notifications.md#email-imediato). O **web push** é opcional: gere a chave VAPID com `manage.py generate_vapid_keys /caminho/privado/vapid.pem` (arquivo novo, 0600, fora do repositório e do diretório estático; o comando imprime a chave pública) e defina `WEBPUSH_VAPID_PRIVATE_KEY_FILE`, `WEBPUSH_VAPID_SUBJECT` (`mailto:` ou `https:`) e, se preciso, `WEBPUSH_ALLOWED_HOSTS`. As assinaturas exigem `INTEGRATION_ENCRYPTION_KEYS`. Trocar a chave VAPID invalida as assinaturas existentes, e cada pessoa precisa ativar o push de novo. Sem a chave, a seção de push não aparece e `/sw.js` responde 404.

Os logs da aplicação e do Django usam `SecurityJSONFormatter`, com um evento de conclusão `http.request` após resultado transacional/callbacks e UUID gerado pelo servidor, sem URL/body/cookies. Outcomes distinguem success, validation_error, conflict, access_denied, not_found e server_error; falha de entrega preserva o status de persistência e registra reason_code. status_code do Django é mapeado para status; error_class e target_sysid são seguros, sem mensagem/traceback bruto. Registros com exceção trazem `error_stack`: até 25 posições `arquivo:linha:função`, da mais externa para a mais interna, sem mensagem, código-fonte ou variáveis locais. Um readiness com falha registra `health.probe_failed` com `reason_code` `database_unavailable` ou `cache_unavailable`; a resposta HTTP continua sendo só 503. O agente de deployment escreve no stderr uma linha JSON `agent.internal_error`, no mesmo formato, antes de responder `agent_internal_error`. O comando Gunicorn da imagem não habilita access log bruto. Proxy, ingress, APM e capturas de HTTP também precisam redigir caminhos/query de convite, reset e callbacks (as integrações do núcleo com Prometheus, OpenTelemetry e Sentry já não enviam caminhos, ver [Observabilidade](#observabilidade-87)); não acrescente `--access-logfile -` sem formatter seguro. O backend de email console é somente desenvolvimento e imprime links privados por definição.

**Antivírus de anexos (opcional):** rode um `clamd` numa rede privada (por exemplo, a imagem `clamav/clamav` com a porta 3310 sem publicação externa), defina `SKELETON_FILES_SCANNER=clamav` e `SKELETON_FILES_CLAMAV_ADDRESS=tcp://clamav:3310` e mantenha o worker de jobs rodando: sem ele, os anexos ficam `pending` e não podem ser baixados. Ajuste `StreamMaxLength` do `clamd` para pelo menos `SKELETON_UPLOAD_MAX_BYTES`, senão o `clamd` responde erro e o anexo fica pendente. A cota de armazenamento e a limpeza de órfãos também dependem do worker. Veja [arquivos e anexos](files.md#antivírus).

Backups PostgreSQL devem incluir entidades, audit e estado de migrations; proteja DSNs e chaves Fernet separadamente. Teste restore em DB vazio com mesmas versões/apps, check de schema e contagens/IDs. Backups de Authenticator exigem chaves antigas; não exclua chave só porque runtime novo deixou de usá-la. Banco/audit não é WORM contra DBA. Nunca use purge/flush/TRUNCATE no runtime protegido.

## Observabilidade (#87)

Métricas, traces e relatórios de erro são **extras opcionais**, todos desligados por padrão. Sem o setting de cada um, o processo não importa a biblioteca correspondente (`prometheus_client`, `opentelemetry`, `sentry_sdk`). Instale só o que for usar, na mesma imagem e com hashes: `pip install "django-skeleton[metrics]"`, `[otel]` ou `[sentry]`. As faixas ficam em `pyproject.toml`, e as versões travadas e auditadas por `pip-audit` estão em `requirements-dev.txt`. Um setting definido sem o extra instalado gera o aviso `skeleton.W030` e não liga nada.

Nenhum dos três envia dado de negócio. Rótulos e atributos não carregam sysid de organização ou de pessoa, slug, email, nome, corpo, cookie, cabeçalho de autenticação nem parâmetro SQL. O que cada um envia está descrito abaixo.

### Métricas Prometheus (#175)

Defina `SKELETON_METRICS_TOKEN` (setting sensível da instância, cifrado no SQLite; ao menos 32 caracteres sem espaços, senão `skeleton.E030`). Gere um com `python -c "import secrets; print(secrets.token_urlsafe(32))"`. Sem o token, `/metrics` não existe (404). Com ele:

- `/metrics` responde antes de sessão, autenticação e contexto de tenant, só a `GET`/`HEAD`, e só com `Authorization: Bearer <token>`. O token é comparado em tempo constante (SHA-256 dos dois lados e `hmac.compare_digest`). Token errado ou ausente responde 401 sem corpo, com `WWW-Authenticate: Bearer`, e registra `metrics.denied`. O host continua validado por `ALLOWED_HOSTS`.
- Restrinja o caminho também na rede ou no proxy: o token é um segundo fator, não o único.

Configuração do Prometheus:

```yaml
scrape_configs:
  - job_name: skeleton
    metrics_path: /metrics
    authorization:
      type: Bearer
      credentials_file: /etc/prometheus/skeleton-metrics-token
    static_configs:
      - targets: ["app.internal:8000"]
```

| Métrica | Rótulos | Origem |
|---|---|---|
| `skeleton_http_request_duration_seconds` (histograma) | `method`, `route`, `status` | Cada request, medido pelo middleware. `route` é o **padrão** resolvido (`o/<uuid:organization_id>/`), nunca o caminho; sem rota, `unresolved`; os probes usam `health/live/` e `health/ready/` |
| `skeleton_jobs` | `job_type`, `state` | Jobs por tipo registrado e estado; tipo não registrado entra como `other` |
| `skeleton_delivery_jobs` | `channel` (`webhook`, `email`, `push`), `state` | Jobs de entrega de webhook e de notificação |
| `skeleton_jobs_due`, `skeleton_jobs_oldest_due_lag_seconds`, `skeleton_jobs_dead` | — | `queue_health()`, o mesmo de `check_jobs` e de `/platform/jobs/` |
| `skeleton_job_workers` | `state` (`live`, `stale`) | Heartbeats dos workers |
| `skeleton_queue_metrics_up` | — | 1 quando a coleta leu a fila; 0 quando o banco falhou (o resto da coleta continua) |

As métricas da fila são calculadas **na coleta**, por consultas agregadas de leitura num `system_context` sem organização (as tabelas de jobs são globais). O worker não mantém contadores. Cada coleta faz algumas consultas agregadas sobre `Job` e `WorkerHeartbeat`; um intervalo de 15 a 60 s basta. Veja [jobs](jobs.md#métricas-da-fila).

**Gunicorn com vários workers (modo multiprocess).** Cada processo tem o próprio histograma de requests. Para somá-los, defina `PROMETHEUS_MULTIPROC_DIR` com um diretório privado e vazio, gravável pelo usuário do web, **antes** de o gunicorn iniciar, e esvazie-o a cada início do serviço:

```sh
export PROMETHEUS_MULTIPROC_DIR=/run/skeleton-metrics
rm -rf "$PROMETHEUS_MULTIPROC_DIR" && install -d -m 0700 -o skeleton "$PROMETHEUS_MULTIPROC_DIR"
gunicorn config.wsgi:application --workers 3
```

Com a variável, a coleta lê os arquivos de todos os processos (`MultiProcessCollector`) e as métricas da fila continuam vindo do banco. Só há histogramas por processo, sem gauges, então não é preciso o hook `child_exit`. Sem a variável, cada scrape vê apenas o processo que o atendeu. Em systemd, use `RuntimeDirectory=skeleton-metrics`, que o recria vazio a cada início. O worker de jobs não expõe métricas.

Fora do escopo: dashboards, alertas e métricas por organização.

### Tracing OpenTelemetry (#176)

Instale o extra `[otel]` (SDK, exportador OTLP/HTTP e as instrumentações oficiais de Django e psycopg) e configure-o pelas variáveis padrão do OpenTelemetry, no ambiente do web e do worker. O tracing liga quando há um endpoint e não há `OTEL_SDK_DISABLED=true`:

| Variável | Uso |
|---|---|
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` ou `OTEL_EXPORTER_OTLP_ENDPOINT` | Coletor OTLP/HTTP (`http://collector:4318`); sem nenhuma das duas, o tracing fica desligado |
| `OTEL_SERVICE_NAME` | Nome do serviço; padrão `skeleton`. Use nomes diferentes para web e worker |
| `OTEL_TRACES_SAMPLER`, `OTEL_TRACES_SAMPLER_ARG` | Amostragem, por exemplo `parentbased_traceidratio` e `0.1` |
| `OTEL_EXPORTER_OTLP_HEADERS` | Cabeçalhos do coletor. Podem conter credencial: entregue por secret do ambiente, nunca no repositório |
| `OTEL_PYTHON_DJANGO_EXCLUDED_URLS` | Rotas sem span; padrão `health/live/,health/ready/,metrics` |
| `OTEL_RESOURCE_ATTRIBUTES` | Atributos do recurso, como `deployment.environment=prod` |

Essas variáveis são lidas pelo SDK direto do ambiente, por isso não fazem parte do catálogo da instância. O núcleo instala a instrumentação em `AppConfig.ready()`, antes de o handler WSGI carregar os middlewares. Com `gunicorn --preload`, o `BatchSpanProcessor` é recriado em cada worker depois do fork.

O que vira span:

- **Request:** um span `SERVER` por request, com nome `<método> <padrão de rota>` (`GET o/<uuid:organization_id>/`), método, rota, status e `skeleton.request_id`, o mesmo UUID do cabeçalho `X-Request-ID` e do log `http.request`. O contexto W3C (`traceparent`) de entrada é respeitado.
- **Job:** um span `CONSUMER` `job <code>` por execução no worker, com `skeleton.job_id`, `skeleton.job_type`, `skeleton.job_queue`, `skeleton.job_attempt`, `skeleton.job_state` e, na falha, `error.type` (a classe). Cada execução tem `skeleton.request_id` próprio, que também vai nos logs do job.
- **SQL:** um span `CLIENT` por statement, filho do request ou do job, com `db.system`, `db.name`, a operação (`SELECT`, `INSERT`...) e o `skeleton.request_id` de quem o executou.

Nenhum span leva URL, query string, host, IP ou user agent do cliente, texto SQL, parâmetros SQL, email, nome, corpo, cookie ou cabeçalho. A garantia não depende das instrumentações: o único exportador é `RedactingSpanExporter`, que reconstrói cada span com uma lista fixa de atributos, reduz o nome dos spans de SQL à palavra-chave inicial, mantém dos eventos de exceção só `exception.type` (sem mensagem nem stack trace) e remove a descrição do status. Os comentários SQL (`sqlcommenter`) e a captura de parâmetros ficam desligados. Atributos que você acrescentar num span próprio também passam por essa lista: fora dela, são descartados.

O exportador fala com o coletor que o operador configurou; não é saída controlada por tenant, por isso não passa por `skeleton.common.outbound_http`. Ponha o coletor numa rede privada.

Fora do escopo: o coletor e o backend de traces.

### Sentry (#177)

Instale o extra `[sentry]` e defina `SKELETON_SENTRY_DSN` (setting sensível da instância, cifrado no SQLite) com o DSN do projeto, `https://<chave>@<host>/<projeto>`. Sem o DSN, `sentry_sdk` não é importado nem inicializado; a variável `SENTRY_DSN` do próprio SDK é ignorada, porque o núcleo só chama `sentry_sdk.init` com `SKELETON_SENTRY_DSN`. `SENTRY_ENVIRONMENT` e `SENTRY_RELEASE` continuam lidos pelo SDK do ambiente.

O SDK é iniciado com `send_default_pii=False`, `max_request_body_size="never"` (o corpo nunca é lido), sem variáveis locais nem linhas de código-fonte, sem sessões e sem performance monitoring (traces são do OpenTelemetry). A integração do Django agrupa os erros pelo padrão de rota (`transaction_style="url"`).

Um `before_send` reconstrói cada evento por lista fixa, a mesma regra dos logs JSON (`skeleton.common.logging`):

| Sai | Não sai |
|---|---|
| Tipo e módulo de cada exceção; frames com nome do arquivo sem diretório, linha, função e módulo, no máximo 25, como o `error_stack` dos logs | Mensagem da exceção, variáveis locais, linhas de código, caminho absoluto |
| Nome do evento de log (`job.dead`, `health.probe_failed`) e os campos estruturados que os logs aceitam (`safe_fields`: `status`, `reason_code`, `outcome`, `target_sysid` válido...) | Texto e parâmetros da mensagem de log, `extra` arbitrário, contexto de segurança |
| Método HTTP e padrão de rota | URL, query string, cabeçalhos, `Authorization`, cookies, corpo, IP, user agent |
| Tag `skeleton.request_id`, o mesmo UUID do `X-Request-ID` e dos logs | Usuário (`user`), tags arbitrárias |
| IDs de trace, nome e versão do runtime, `environment`, `release`, `server_name` | Threads, módulos instalados, outros contextos |
| Tipo, categoria, nível e hora dos breadcrumbs | Mensagem (SQL, texto de log) e dados (parâmetros, URLs) dos breadcrumbs |

Os eventos de log `ERROR` (como `job.dead`) também viram eventos no Sentry, pela integração de logging do SDK, com o mesmo tratamento. O SDK envia ao host do DSN, configurado pelo operador, e não passa por `skeleton.common.outbound_http`.

Fora do escopo: performance monitoring do Sentry.

## Instalações antigas com SQLite

Produção sempre exigiu PostgreSQL. Bancos SQLite de desenvolvimento deixam de ser suportados (#144) e não são migrados: recrie o ambiente local com `tools/dev_postgres.py start` e os comandos do README. O transporte de dados SQLite para PostgreSQL foi retirado deste guia.

## Sandbox no Coolify (#369)

Um ambiente de sandbox e testes numa VPS com [Coolify](https://coolify.io), atualizado a cada merge em `main`. Ele **não** é o caminho de produção: não há agente, então não há manutenção coordenada, backup antes da migration nem rollback. Use só dados de teste.

O `compose.sandbox.yaml` da raiz difere do `compose.yaml` em quatro pontos:

| Ponto | `compose.sandbox.yaml` |
|---|---|
| Portas | Nenhum serviço publica porta no host; o proxy do Coolify chega ao `web` na 8000 pela rede interna. |
| PostgreSQL | Imagem `deploy/coolify/postgres.Dockerfile`: aplica `deploy/postgres/bootstrap.sql` e define as senhas de `skeleton_owner` e `skeleton_runtime` **só na criação do volume**. |
| Migrations | O serviço `migrate` roda a cada deploy, com a credencial owner, `migrate` e `sync_table_catalog`, e sai. O `web` só sobe depois dele (`service_completed_successfully`). A URL owner existe só nesse serviço. |
| Start do `web` | Com a instância configurada, roda `upgrade_app_activation` e `check_skeleton_schema` com a role runtime antes do Gunicorn, como o agente faz no `finish()`. Uma falha impede o `web` de subir. Sem configuração, sobe em modo setup. |

### Criar o recurso

1. **DNS:** um registro A do host do sandbox (por exemplo `sandbox.example.com`) para o IP da VPS.
2. **Projeto:** no Coolify, crie um projeto e um environment `sandbox`.
3. **Recurso:** no environment, *+ New* → *Public Repository* (repositório privado: *Private Repository (with GitHub App)*), a URL do repositório e o branch. *Build Pack* `Docker Compose`, *Base Directory* `/` e *Docker Compose Location* `/compose.sandbox.yaml`.
4. **Variáveis** (aba *Environment Variables*). Gere cada senha com `openssl rand -hex 32`, só letras e dígitos, porque elas entram em URLs sem codificação:

   | Variável | Uso |
   |---|---|
   | `SANDBOX_HOST` | Host público, sem esquema: `sandbox.example.com` |
   | `POSTGRES_PASSWORD` | Administrador do PostgreSQL; só o container do banco a recebe |
   | `SKELETON_OWNER_DB_PASSWORD` | Role owner; só o `migrate` e o init do banco a recebem |
   | `SKELETON_RUNTIME_DB_PASSWORD` | Role runtime; digitada no setup |
   | `REDIS_PASSWORD` | `requirepass` do Redis |

5. **Domínio:** na configuração do serviço `web`, informe `https://<SANDBOX_HOST>:8000`. O sufixo `:8000` é a porta do container, não uma porta pública; o Coolify emite o certificado e o Traefik termina o TLS.
6. **Auto Deploy:** desligado. Quem dispara o deploy é o CI (abaixo).
7. *Deploy*. Na primeira vez, o `migrate` cria o schema e o `web` sobe em modo setup.

### Primeiro acesso

O token de uso único aparece nos logs do serviço `web` no Coolify. Abra `https://<SANDBOX_HOST>/setup/` e preencha o [setup](#primeira-instalação-pelo-setup-146) com:

- **Instância:** host `<SANDBOX_HOST>`, origem CSRF `https://<SANDBOX_HOST>` e confiança no proxy ligada (o Traefik substitui o `X-Forwarded-Proto` recebido de fora).
- **Banco runtime:** `postgres://skeleton_runtime:<SKELETON_RUNTIME_DB_PASSWORD>@postgres:5432/skeleton`.
- **Schema:** `postgres://skeleton_owner:<SKELETON_OWNER_DB_PASSWORD>@postgres:5432/skeleton`. O `migrate` já aplicou as migrations; o setup só confere.
- **Cache:** `redis://:<REDIS_PASSWORD>@redis:6379/0`.
- **Arquivos:** backend local em `/instance/files`, dentro do volume persistente `instance`.

Na conclusão, o processo sai, a política de reinício o traz de volta em modo normal, e o `worker` e o `scheduler` passam a ficar saudáveis.

### Deploy a cada merge em `main`

O job `deploy-sandbox` do `.github/workflows/ci.yml` roda só num push para `main` e só depois do job `CI` verde, e chama a API de deploy do Coolify. Ele nunca roda em pull request. Sem os secrets, o job termina com um aviso, sem disparar nada; projetos derivados sem sandbox não precisam remover o job.

1. **Coolify:** em *Settings*, habilite o acesso à API. Em *Keys & Tokens* → *API tokens*, crie um token com permissão de deploy. Na aba *Webhooks* do recurso, copie a URL de deploy, `https://<coolify>/api/v1/deploy?uuid=<uuid>&force=false`.
2. **GitHub:** em *Settings* → *Environments*, crie `sandbox` (opcionalmente restrito ao branch `main`) com os secrets `COOLIFY_DEPLOY_URL` e `COOLIFY_TOKEN`.

O deploy no Coolify é assíncrono: o job só confirma que o pedido foi aceito. O resultado aparece em *Deployments* no recurso.

**Limites:** o init do PostgreSQL só roda com o volume vazio. Trocar uma senha depois exige `ALTER ROLE` no banco e a atualização da variável e da configuração da instância. Se uma migration falha, o `migrate` termina com erro e o `web` com o código novo não sobe. Confira os logs do `migrate` no Coolify. Migrations já aplicadas antes da falha continuam no banco, porque o sandbox não tem o backup do agente.

## Distribuição do core e adoção de projetos derivados

O `pyproject.toml` é a fonte única da versão do `django-skeleton`. O wheel contém somente `skeleton`, incluindo migrations históricas, templates/static, defaults reutilizáveis e inventário técnico. `config`, `applications`, `examples`, `deployment_agent`, `manage.py`, bancos e secrets não pertencem à distribuição. Os imports históricos e os labels `sk_accounts`, `sk_tenancy`, `sk_access`, `sk_system` e `sk_audit` permanecem estáveis.

Os entrypoints de `config/settings` importam `skeleton.conf.base` e aplicam os profiles de `skeleton.conf.profiles`; o overlay de negócio continua em `config/settings/project.py`. O núcleo calcula `BASE_DIR` pelo entrypoint do projeto ou por `SKELETON_PROJECT_ROOT`, nunca pelo diretório site-packages. Templates de branding podem ficar em `<projeto>/templates`; templates do core são descobertos pelos apps instalados. `config/urls.py` usa `skeleton.conf.urls.application_urls("config.urls_project")`, conservando o hook de rotas do derivado.

### Construir e publicar

**Pré-requisitos:** versão definida em `pyproject.toml`, dependências fixadas, manifesto revisado e chave privada de um publicador aprovado. **Publisher** é quem assina o artefato; aprovação da chave é decisão do operador, não algo que o próprio pacote possa conceder. A assinatura demonstra procedência e integridade, não ausência de código malicioso.

```sh
# Ambiente de build com requirements-dev.txt atualizado e hashes:
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python manage.py build_core_package \
  --wheel dist/django_skeleton-0.2.0-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-0.2.0.skapp
# Equivalente independente de settings/Django startup:
skeleton-core build --wheel dist/django_skeleton-0.2.0-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-0.2.0.skapp
```

O manifesto core identifica o artefato e limita como instalá-lo:

- Use `manifest_version=2`, `kind="core"` e `component_id="django-skeleton"`, com nome, descrição, versão e publisher. Formato 1 continua aceito para verificação de artefatos existentes, sem reescrever sua assinatura.
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
    "stop": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "skeleton-web.service"],
    "worker_stop": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "stop", "skeleton-worker.service", "skeleton-scheduler.service"],
    "worker_restart": ["/usr/bin/sudo", "-n", "/usr/bin/systemctl", "restart", "skeleton-worker.service", "skeleton-scheduler.service"]
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
IDENTITY_PROVIDER_ENCRYPTION_KEYS=OUTRA_CHAVE_FERNET_VALIDA
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

DSNs runtime e owner apontam para o **mesmo banco** com roles diferentes. URL-encode caracteres especiais de credenciais. Arquivos Fernet/SSO/uploads permanecem externos; monte-os/read-only quando necessário, e preserve as chaves antigas. O agente monta ambientes sem herdar env do seu daemon e injeta paths de release/inventário/static/archives; `MIGRATION_DATABASE_URL` em runtime ou build é rejeitada. DSNs que não sejam PostgreSQL são recusados (`unsupported_database`); sem `DATABASE_URL` no arquivo runtime, o agente lê a URL efetiva do SQLite da instância pelos caminhos `SKELETON_INSTANCE_DB` e `SKELETON_INSTANCE_KEY_FILE` desse arquivo (#148).

Opcionalmente acrescente `"smoke_token_file": "/srv/skeleton/private/smoke.token"` e URLs exatas da aplicação/login em `health_urls`. Em produção use URLs HTTPS do proxy (com trust de cabeçalhos corretamente provisionado), pois login HTTP redireciona. O token permite somente GET/HEAD dos paths explicitamente listados em `SKELETON_DEPLOYMENT_SMOKE_PATHS` durante manutenção; não autentica um usuário. Live/ready/release continuam internos. O smoke de login autenticado/SSO precisa de verificação operacional separada; uma página login 200 não prova autenticação.

### Apps first-party

**Feed da agenda:** o link de assinatura (`/me/apps/skeleton_calendar/feed/<token>.ics`) leva o token no caminho, porque clientes de calendário não enviam cabeçalhos. Configure o proxy para **não registrar** esse caminho (ou redigir o segmento depois de `feed/`), como faria com uma senha: quem lê o log lê a agenda até o token expirar ou ser revogado em `/profile/tokens/`. O Django não registra o caminho no log de requisições do núcleo além do status.

As apps first-party (`skeleton/contrib`, como a agenda) vêm no wheel do núcleo e não passam pelo agente nem por `.skapp`. O operador as instala por nome ([extensão](extending.md#apps-first-party-do-núcleo-skeletoncontrib)):

| Passo | O que fazer | Resultado |
|---|---|---|
| Instalar | `SKELETON_BUNDLED_APPS=calendar` no ambiente ou em `/platform/instance/`, reinício e `manage.py migrate` | Depois do `migrate`, `ComponentInstallation` registra a app com `kind=bundled` e `state=bundled`. A disponibilidade nasce `selected` sem concessão: a app fica indisponível e desativada em todas as organizações até a plataforma liberar e cada organização ativar |
| Retirar com dados | Tire o nome de `SKELETON_BUNDLED_APPS`, ponha em `SKELETON_BUNDLED_RETAINED`, reinicie e rode `manage.py sync_bundled_apps` | O estado vira `schema_retained`; as tabelas ficam, validadas pelo snapshot congelado do wheel, e a ativação de cada organização é desligada. Nada é apagado |
| Reinstalar | Volte o nome para `SKELETON_BUNDLED_APPS`, tire de `SKELETON_BUNDLED_RETAINED`, reinicie e rode `migrate` | O estado volta a `bundled` com os mesmos UUIDs, registros e configurações; cada organização reativa a app |

`check_skeleton_schema` aponta `bundled_divergence` quando a app sai de `SKELETON_BUNDLED_APPS` sem entrar em `SKELETON_BUNDLED_RETAINED` (as tabelas ficariam sem classificação), quando volta sem `sync_bundled_apps` ou quando está instalada sem registro (falta `migrate`). Um nome fora do mapa fixo, ou o mesmo nome nas duas listas, para o startup.

### Supervisor e primeira adoção

O JSON acima usa comandos fixos do supervisor. Autorize em sudoers **somente** as ações e a unit exatas, não shell genérico. O marker HTTP sozinho não bloqueia produtores externos: um worker ativo durante as migrations escreveria com o código antigo.

### Sincronização de diretório LDAP

Para organizações com login LDAP (#33), agende `manage.py sync_directory --all --max-deactivations <N>` com cron ou um systemd timer, com o mesmo ambiente do `web` e a mesma chave `IDENTITY_PROVIDER_ENCRYPTION_KEYS`. Rode antes `--dry-run --format json` para conferir o efeito. Um código de saída diferente de zero indica que alguma organização abortou (diretório vazio, erro de busca ou limite de desativações) sem gravar; o relatório traz só contagens e UUIDs ([autenticação](authentication-sso.md#sincronização-do-diretório-33)). Exemplo de timer diário:

```ini
# /etc/systemd/system/skeleton-directory-sync.service
[Service]
Type=oneshot
EnvironmentFile=/etc/skeleton/web.env
ExecStart=/opt/skeleton/.venv/bin/python /opt/skeleton/manage.py sync_directory --all --max-deactivations 50

# /etc/systemd/system/skeleton-directory-sync.timer
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
```

### Reverificação de domínios

Organizações que admitem por domínio de email verificado (#28) dependem do registro TXT continuar publicado. Agende `manage.py verify_domains --recheck` uma vez por dia com cron ou um systemd timer, com o mesmo ambiente do `web`. O comando consulta o DNS fora das transações (3 s por domínio) e imprime só contagens; três falhas seguidas suspendem a admissão por aquele domínio ([autenticação](authentication-sso.md#domínio-de-email-verificado)).

### Worker de jobs e agendador

O worker (`manage.py run_worker`) e o agendador de periódicos (`manage.py run_scheduler`) são serviços operados como o web (#40). Notificações por email e push, antivírus de anexos, cota, webhooks, consumidores de eventos e as retenções dependem deles. As units de exemplo estão em `deploy/systemd/skeleton-worker.service` e `deploy/systemd/skeleton-scheduler.service`; no Compose, são os serviços `worker` e `scheduler` de `compose.yaml`, com a mesma imagem, ambiente e volume da instância do `web`.

| Momento | O que o agente faz |
|---|---|
| `drain` (antes do backup e das migrations) | Executa o hook `drain` e depois `worker_stop`. O SIGTERM termina o job atual e sai sem reivindicar outro; dê ao worker um `TimeoutStopSec` maior que o `timeout_s` dos seus jobs |
| `start` (depois do cutover) | Executa o hook `restart` e depois `worker_restart`: o worker sobe com o código do release novo |
| Compose | Liste os serviços em `"compose": {"worker_services": ["worker", "scheduler"]}`. O agente gera para eles o mesmo override de imagem e ambiente runtime do web (recusa `MIGRATION_DATABASE_URL`), para-os com `compose stop` no `drain` e os recria com `compose up --force-recreate` no `start` |

`worker_stop` e `worker_restart` são opcionais, mas vêm juntos: só um deles é recusado (`incomplete_worker_commands`). Um nome inválido ou repetido em `worker_services`, ou igual ao do web ou do migrator, é recusado (`invalid_compose_worker_services`). Sem eles, o worker só pausa pelo marcador de manutenção (heartbeat `paused`), o que não interrompe um job em execução.

**Deploy manual** (sem o agente): pare o worker e o agendador **antes** de `migrate` e suba-os depois do restart do web:

```sh
sudo systemctl stop skeleton-worker.service skeleton-scheduler.service
# migrate, sync_table_catalog, check_skeleton_schema, restart do web
sudo systemctl start skeleton-worker.service skeleton-scheduler.service
```

Um worker que morreu no meio de um job deixa o lease vencer: o próximo worker recoloca o job na fila uma vez (`lease_expired`). Para conferir a fila, use `manage.py check_jobs`:

```sh
python manage.py check_jobs --max-lag 300 --max-dead 0 --require-worker
```

O comando imprime só contagens (`due`, atraso do job vencido mais antigo, `dead`, workers vivos e sem heartbeat) e sai com 1 quando o atraso passa de `--max-lag` segundos, quando há mais jobs esgotados que `--max-dead` ou quando `--require-worker` não encontra worker com heartbeat recente. Use-o num probe do supervisor; o `compose.yaml` o usa como healthcheck do `worker`.

A tela `/platform/jobs/` mostra a fila e os jobs esgotados; veja [jobs](jobs.md#operar-a-fila-platformjobs). A retenção dos jobs (`SKELETON_JOB_SUCCEEDED_RETENTION_DAYS`, padrão 7, e `SKELETON_JOB_DEAD_RETENTION_DAYS`, padrão 30) roda como job periódico global. Hooks aceitam substituições `{release}`, `{active}`, `{operation}` em argumentos, nunca comandos vindos do pacote.

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

IDs de app são os `app_id` dos manifests, não labels Django.

**Dependências entre apps.** Antes de copiar o projeto ou extrair qualquer pacote, a prévia resolve o conjunto resultante: inventário atual, mais os pacotes do plano, menos as apps em retirada. As falhas têm códigos próprios e vêm com detalhes (`details`: app, dependência, faixa exigida, versão encontrada), que o admin mostra em linguagem do operador:

| Código | Quando | O que fazer |
|---|---|---|
| `dependency_unsatisfied` | Falta uma dependência, a versão ficou fora da faixa de uma app instalada, há ciclo ou conflito. | Incluir a dependência no plano, escolher outra versão ou ajustar o manifesto. |
| `installed_dependents` | A retirada deixaria apps instaladas sem uma dependência. | Retirar os dependentes no mesmo plano ou manter a app. |
| `not_installed_use_install` | Um `update` não atualiza nenhuma app instalada. | Usar `install`. |
| `target_runtime_incompatible` | `compatibility` não aceita o core, o Python ou o Django do alvo. | Publicar um release compatível. |

Um `update` pode trazer apps novas junto, desde que atualize ao menos uma app instalada. É assim que uma dependência nova entra num plano de atualização.

Para uma dependência faltante ou fora da faixa, a prévia sugere releases compatíveis dos catálogos já verificados por `check-updates`: a faixa exigida e o ambiente-alvo filtram as entradas, e as mais novas vêm primeiro. Nada é baixado sem escolha do operador. No admin, "Adicionar ao plano" baixa a release sugerida pelo mesmo caminho de `fetch-release` e refaz a prévia com ela; uma dependência instalada fora da faixa vira "Atualizar no plano", que troca a ação para `update`. O plano resultante passa pela confirmação normal.

O inventário grava as apps em ordem de dependência e registra em `runtime` as versões de Python e Django do alvo. `check-updates` usa essas versões, e não as do processo do agente, para marcar releases como `incompatible`.

Retirada não usa `migrate zero`: preserva schema, AppInstallation/configuração, ACLs/auditoria e o pacote assinado. As migrations históricas são copiadas byte-exact para archives sem modelos/rotas/hooks; apenas migrator carrega os labels. Preview rejeita imports de negócio retirado, histórico alterado e qualquer migration histórica retida ainda não aplicada. Reinstalação confere identidade/histórico/schema e deixa reativação organizacional explícita. Downgrade é recusado.

Atualização/reinstalação não transforma configuração de tenants: mudança incompatível de `runtime.configuration_fields` é bloqueada com `configuration_upgrade_requires_review`; `runtime.default_enabled` é ignorado desde o core 0.5.0. Publique um release com contrato compatível ou realize primeiro procedimento de evolução versionada revisado; o agente não substitui configuração persistida por defaults novos nem ativa organizações silenciosamente.

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

Antes da primeira migration, o journal registra `backup` apontando para `agent-state/backups/.../backup.json`: PostgreSQL custom dump do schema `skeleton` inteiro (negócio, tabelas técnicas, migrations e auditoria; o agente recusa outro backend), hashes e snapshots exatos dos recursos. Extensões/roles PostgreSQL são provisionadas separadamente; dados fora de `skeleton` exigem procedimento offline específico. Um backup parcial de crash é preservado; uma nova tentativa usa outro diretório. Backup/journal/uploads/plans permanecem privados e fora do DB da aplicação. Nunca apague o release anterior, os artefatos aprovados ou esses snapshots enquanto houver recuperação/retention pendente.

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
