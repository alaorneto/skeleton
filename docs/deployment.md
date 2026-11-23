# Deployment, credenciais e transporte de dados

Este roteiro é para quem coloca a aplicação em funcionamento e precisa recuperá-la sem relaxar a segurança. **Deployment** é a instalação em execução (código, processos e serviços); **release** é um conjunto identificado de código e dependências preparado para essa instalação. Atualizar código não transporta dados nem torna uma migration reversível.

Antes de operar, leia [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados). Aqui os comandos usam caminhos de exemplo: substitua-os pelos caminhos provisionados, mantendo a separação de contas e credenciais.

### Quem executa o quê

| Termo | Significado e uso seguro |
|---|---|
| Credencial | Identidade de conexão e seu segredo. Não é a identidade do usuário Django. Nunca a publique em logs, Git ou argumentos de comandos. |
| DSN | Endereço de conexão, normalmente uma URL com banco, host e role; pode conter senha. `DATABASE_URL` é o DSN runtime; `MIGRATION_DATABASE_URL` é o DSN owner. As conexões runtime são persistentes por 60 s e verificadas antes do reuso (`CONN_HEALTH_CHECKS`), então um PostgreSQL reiniciado não derruba a requisição seguinte (#333). |
| Schema | Espaço de nomes dentro do banco, aqui `skeleton`; não é outro banco. Contém tabelas, funções e infraestrutura de proteção. |
| Runtime | Processo que atende usuários, com `skeleton_runtime`; lê/grava somente o permitido, não modifica schema. |
| Owner | Role privilegiada `skeleton_owner`, usada offline para criar/alterar infraestrutura e recuperar dados. Não serve requests. |
| Migrator | Processo temporário que executa migrations com a credencial owner. Não é uma terceira role nem um worker web. |
| Operador/agente | Conta de sistema que prepara releases e coordena manutenção. Seus privilégios de supervisor/Docker não pertencem ao web. |

**Todo deployment roda o agente (#649).** Nos containers, o serviço `agent` dos arquivos Compose instala o release publicado na [primeira partida](#primeira-partida-pelo-agente-containers) e controla os serviços de aplicação [sem o socket Docker](#serviços-em-containers-sem-socket-docker); com systemd, a unit `skeleton-agent.service` com o [backend diretório](#agente-independente-de-deployment). Em qualquer caso só releases publicados e assinados chegam à instalação, e o operador [escolhe a versão do core](updates.md#atualizar-pelo-github), com uma confirmação por passo. Sem agente fica só o desenvolvimento local (`runserver`, [README](../README.md)).

**Ordem segura:** provisione serviços e segredos → crie roles/schema → aplique migrations com owner → verifique com runtime → inicie web → confirme saúde e comportamento. Um check feito como owner não demonstra que o web está isolado.

### Índices da auditoria (`sk_audit.0005`)

A migration `sk_audit.0005_audit_indexes` (#270) cria `sk_audit_record_created` com `CREATE INDEX CONCURRENTLY` e só depois remove os índices sem leitor; ela não roda em transação (`atomic = False`). Em instalações grandes a criação demora e consome I/O, mas não bloqueia as escritas. Se a execução for interrompida, o índice pode ficar inválido e a migration fica sem registro em `django_migrations`: com o owner, rode `DROP INDEX CONCURRENTLY IF EXISTS skeleton.sk_audit_record_created;` e `migrate` de novo. O reverso (`migrate sk_audit 0004`) recria os índices antigos.

### Apps fechadas por padrão

Uma app instalada nasce indisponível e desativada: sem linha de disponibilidade nenhuma organização a usa, e sem `AppInstallation` ela está desativada (#22). Depois das migrations e de `sync_table_catalog`, rode `manage.py upgrade_app_activation` antes de liberar tráfego (`--dry-run` mostra o que vai mudar). O agente faz isso sozinho no `finish()`, sob a credencial runtime, em todo core publicado (`APP_ACTIVATION_UPGRADE_CORE`, 0.1.0); num deploy manual, rode o comando. Ele é idempotente e retomável; repetir em todo deploy é seguro. `runtime.default_enabled` é ignorado: mudá-lo não ativa nada.

### API de serviços

Todo core publicado lê `runtime.service_api` ([API de serviços](service-api.md)); o piso `SERVICE_API_CORE_MINIMUM` é 0.0.0. Apps instaladas sem o campo continuam carregando, com o aviso `skeleton.W018`; um release novo delas precisa declarar a API de serviços, porque `package build` e `package doctor` recusam a ausência.

### Contratos de apresentação

O core distribui `django-crispy-forms==2.7` e o pack SSR próprio `skeleton_crud`; a dependência vem no lock do release, sem download pelo web nem renderer alternativo. Novas publicações usam `manifest_version=2`. Todo core publicado entende as dimensões `col_span` e `rows` (os pisos `MANIFEST_V2_CORE_MINIMUM` e `FIELD_DIMENSIONS_CORE_MINIMUM` são 0.0.0). Pacotes v1/v2 assinados continuam verificáveis sem normalizar JSON ou alterar bytes/digest/inventário. O [contrato de interfaces](application-admin-ui-plan.md) descreve grade de 12 unidades, dimensões e defaults e separa a administração da plataforma (`/platform/`) do viewer tenant; nenhum layout concede acesso ou modifica autorização/CAS/persistência.

Preview e aprovação incluem `presentation_changes`: grupos/seções/telas, layouts e campos de configuração. Alterar somente span/rows gera `model_layout/change`, sem incompatibilidade de configuração, e integra o digest do plano. Alterações incompatíveis ficam explícitas. Reordenação/rótulos não apagam configuração; remoção/renomeação/tipo/limite precisam de migration histórica que conserve o estado válido. Após migrations e antes de liberar tráfego, o agente executa `check_application_configuration` sob runtime, além dos checks existentes. O comando percorre organizações em transações independentes, não imprime valores/segredos e recusa estado persistido incompatível. Falha conserva manutenção e journal para recuperação; não há coerção ou descarte escolhido por HTTP.

```sh
# Depois de migrations owner, no ambiente com somente DATABASE_URL runtime:
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_application_configuration
```

## Settings e serviços

- `development`: PostgreSQL (`DATABASE_URL`, ou `tools/dev_postgres.py` para um banco local descartável), email console, cache LocMem. Sem configuração, o `runserver` abre o [setup](#primeira-instalação-pelo-setup-146).
- `production`: PostgreSQL, Redis compartilhado (com um prefixo de chaves por instância: `SKELETON_CACHE_KEY_PREFIX`, ou `?key_prefix=` na `CACHE_URL`, que os settings passam para o `KEY_PREFIX` que o Django lê; os dois juntos impedem a partida; sem prefixo, `skeleton.W049`, e no modo cluster `skeleton.E049`, #675), SMTP, SECRET_KEY, hosts explícitos, remetente e chaves MFA obrigatórios; DEBUG=False. Cookies secure, HTTPS redirect/HSTS, password validators, CSRF origins explícitas. Live/ready são exceções ao redirect para healthcheck interno, sem inventário/segredos.
- `migrate`: conexão exclusivamente `MIGRATION_DATABASE_URL` (PostgreSQL, role owner), só no ambiente; não há fallback.
- `test`: `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) de um PostgreSQL descartável; nunca o banco de produção.
- `build`: collectstatic com manifest storage sem exigir credenciais de produção. Não é settings para servir requests.
- `setup`: o [primeiro acesso](#primeira-instalação-pelo-setup-146), sem banco.
- **Só PostgreSQL (#144):** todos os dados (usuários, negócio, auditoria, eventos, jobs, notificações, anexos) vivem no PostgreSQL. O check `skeleton.E025` reprova qualquer outro backend em `DATABASES` e um `DATABASES` vazio fora do setup; as migrations de infraestrutura falham explicitamente fora do PostgreSQL. O SQLite da instância guarda só configuração e o estado do setup.
- Arquivos: `STORAGES["skeleton_files"]` é privado em todos os perfis. Em produção, defina `SKELETON_FILES_ROOT` fora de `STATIC_ROOT` num volume persistente, ou `SKELETON_FILES_BACKEND=s3` com o extra `[s3]` e um bucket sem acesso público. `check --deploy` (E024) recusa armazenamento público ou ausente. Para entregar downloads pelo proxy, use `SKELETON_FILES_ACCEL=nginx` com uma `location` **interna** (`internal;`) em `SKELETON_FILES_ACCEL_PREFIX` apontando para a raiz privada, ou `SKELETON_FILES_ACCEL=sendfile` no Apache; o Django continua autorizando cada pedido. Veja [arquivos](files.md#download).

### Configuração da instância (#143)

As chaves do catálogo (`skeleton/instance/schema.py`: `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`, `CACHE_URL`, `EMAIL_URL`, chaves MFA e de provedores, limites e armazenamento de arquivos) são resolvidas com a precedência **ambiente > SQLite da instância > default**. O `.env` lido na partida conta como ambiente. Chave fora do catálogo é recusada.

- `SKELETON_INSTANCE_DB` (padrão `var/instance/instance.sqlite3`) e `SKELETON_INSTANCE_KEY_FILE` (padrão `var/instance/master.key`) são variáveis de bootstrap e ficam só no ambiente, como `DJANGO_SETTINGS_MODULE`, o inventário e os caminhos do agente e o marcador de manutenção. A credencial owner (`MIGRATION_DATABASE_URL`) nunca é gravada.
- `manage.py instance set <chave> <valor>` valida e grava; use `--stdin` para segredos, que ficam fora do histórico do shell. Um valor que começa com `-` (uma chave Fernet gerada começa assim cerca de uma vez em 64) seria lido como opção e recusado com `unrecognized arguments`: passe-o por `--stdin` ou depois de `--` (`instance set mfa_encryption_keys -- <valor>`) (#444). `instance unset` remove; uma chave obrigatória (`DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DATABASE_URL`) só sai do SQLite quando o ambiente a fornece, ou com `--force`, ferramenta de recuperação de quem está no host (#267). `instance show` lista cada chave com a origem efetiva (`env`, `instance` ou `default`) e mascara segredos; `instance status` mostra caminhos, versão do schema e o que falta para o setup; `instance export-template` imprime um `.env` comentado, sem valores; `instance setup-token` emite um token novo de primeiro acesso, revogando o aberto ([setup](#primeira-instalação-pelo-setup-146)).
- **Migração de uma instalação existente:** ela continua funcionando só com o ambiente, porque o ambiente vence. `manage.py instance import-env` grava no SQLite as chaves do catálogo presentes no ambiente atual (`--keep-existing` preserva as já gravadas); depois, remova-as do ambiente.
- **Chave mestra:** gerada na primeira gravação de um segredo, com permissão 0600 (o SQLite também é criado 0600), num arquivo separado do SQLite. Sem ela, ou com outra, a leitura de um segredo falha com erro explícito na partida, sem cair para o default. **Faça backup da chave mestra separado do SQLite:** perdê-la torna os segredos ilegíveis, e eles terão de ser gravados de novo. Com segredos já gravados e o arquivo da chave ausente, gravar outro segredo é recusado (`MasterKeyMissing`): restaure o backup da chave em vez de deixar o sistema criar outra, que não decifraria os antigos. A chave nova é publicada de forma atômica (arquivo temporário e `link`), e partidas simultâneas leem a mesma chave completa (#323).
- **Rotação da chave mestra (#342):** o arquivo guarda uma chave por linha, a atual primeiro (um arquivo de uma linha é o formato original e continua válido). `manage.py instance rotate-key` grava uma chave nova à frente das antigas (troca atômica do arquivo, 0600) e só então recifra todos os segredos do SQLite numa transação, registrando `("*", "rotate-key", "cli:rotate-key")` no `change_log`, sem valores. Se a recifra falhar, o arquivo fica com as duas chaves e tudo continua legível; rode de novo. Depois, faça backup do novo arquivo de chave, reinicie a instância (os processos leram os segredos na partida) e rode `manage.py instance rotate-key --drop-old`, que remove as chaves antigas e recusa enquanto algum segredo ainda depender delas. Backups antigos do SQLite exigem o arquivo de chave da mesma época. O `check --deploy` avisa com `skeleton.W036`, em POSIX, quando o SQLite ou a chave mestra são legíveis pelo grupo ou por outros usuários (`chmod 600`).
- **Modo cluster (#673):** com vários nós de app, a configuração precisa ser a mesma em todos, e um SQLite por nó divergiria. A variável de bootstrap `SKELETON_INSTANCE_MODE=cluster` (padrão `standalone`; outro valor impede a partida) faz a instância ler a configuração **só do ambiente**: o SQLite da instância não é aberto para configuração, o setup nunca roda (faltando `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS` ou `DATABASE_URL`, a partida falha com `ImproperlyConfigured` citando as chaves), `/platform/instance/` fica só leitura (gravar e "Pedir reinício" respondem 409 `instance_cluster_mode`) e `manage.py instance set|unset|import-env|rotate-key|setup-token` recusam. Para levar uma instância configurada pelo setup para o cluster, rode nela, ainda `standalone`, `manage.py instance export-env --output /caminho/cluster.env`: o comando grava num arquivo novo, 0600, todas as chaves efetivas (gravadas e do ambiente), com os segredos decifrados, no formato `NOME="valor"` que o `env_file` do Compose e o `EnvironmentFile` do systemd leem; recusa sobrescrever (`export_target_exists`) e escrever no terminal (`export_requires_file`), e registra no `change_log` e no evento `instance.exported` só a exportação, sem valores. Guarde o arquivo no cofre de segredos, entregue-o a todos os nós e apague a cópia local. Em cluster, os anexos precisam de armazenamento compartilhado: `SKELETON_FILES_BACKEND=s3`, ou `SKELETON_FILES_SHARED=true` com `SKELETON_FILES_ROOT` num volume que todos os nós montam (NFS, EFS, CephFS); senão, `skeleton.E048`. Chaves ainda gravadas no SQLite são ignoradas e geram `skeleton.W048`. Para mudar a configuração de um cluster, mude o arquivo de ambiente e reinicie os nós.
- **Validação na gravação:** os keyrings (`MFA_ENCRYPTION_KEYS`, `IDENTITY_PROVIDER_ENCRYPTION_KEYS`, `INTEGRATION_ENCRYPTION_KEYS`) só aceitam chaves Fernet, e `secret_key` exige 50 ou mais caracteres com 5 ou mais diferentes (o critério do `security.W009`). A recusa vale para `manage.py instance set`, `import-env`, o setup e `/platform/instance/`, e a mensagem nunca repete o valor. Valores antigos já gravados continuam lidos; só uma nova gravação é validada (#323).

### Endereços de organização (#477)

O guia completo, com DNS, TLS, proxy, roteiro de verificação e limites, é [endereços de organização](organization-sites.md).

- `SKELETON_ORGANIZATION_BASE_DOMAIN` (catálogo da instância, padrão vazio): domínio base dos endereços `<slug>.<domínio base>` das organizações, como `skeleton.com`. Vazio, o recurso fica desligado e toda organização continua só em `/o/<uuid>/`. O valor é só o domínio (sem esquema, caminho, porta ou curinga) e exige `SKELETON_PUBLIC_URL`, de onde vêm o esquema e a porta dos endereços; o check `skeleton.E038` reprova os dois erros.
- **Domínio base dedicado:** todo host abaixo dele recebe o cookie de sessão, compartilhado entre o domínio principal e os endereços das organizações ([sessão entre hosts](authentication-sso.md#sessão-entre-o-domínio-principal-e-os-endereços-de-organização)). Não delegue subdomínios dele a terceiros nem hospede outra aplicação ali. Configurar o domínio base troca o nome do cookie de sessão para `skeleton_session`: todos entram de novo uma vez. Use um `SKELETON_PUBLIC_URL` dentro do domínio base (`https://app.skeleton.com`), senão a sessão não é compartilhada (`skeleton.W039`).
- `SKELETON_ORGANIZATION_CUSTOM_DOMAINS` (catálogo da instância, booleano, padrão desligado; só vale com o domínio base): permite que o administrador de uma organização cadastre até 3 domínios próprios em `/o/<org>/settings/domains/` e os comprove com o TXT `_skeleton-site.<domínio>` ([#488](https://github.com/alaorneto/skeleton/issues/488)). A reverificação periódica é a de `manage.py verify_domains --recheck` ([reverificação de domínios](#reverificação-de-domínios)): três falhas seguidas marcam o domínio como falho e removem a posse.
- Com o domínio base, `.<domínio base>` entra sozinho no fim de `ALLOWED_HOSTS` (nos settings de produção também): não o acrescente em `DJANGO_ALLOWED_HOSTS`. A primeira entrada continua sendo o host de saúde padrão.
- Ligado o endereço de uma organização, os emails **novos** (notificações, convites, links de ingresso, aprovação de pedido) levam links para `<slug>.<domínio base>`; desligá-lo faz os novos voltarem ao domínio principal, e os links já enviados para o endereço desligado passam a responder 404 ([notificações](notifications.md#email-imediato)).

#### Domínios próprios (#489)

Com `SKELETON_ORGANIZATION_CUSTOM_DOMAINS` ligado (e o domínio base configurado), um domínio próprio comprovado (#488) de organização com endereço ativo atende exatamente como `<slug>.<domínio base>`: URLs limpas, marca e os mesmos bloqueios. `ALLOWED_HOSTS` vira `SiteAllowedHosts` (`skeleton/tenancy/site.py`), uma lista cujos hosts estáticos continuam os de `DJANGO_ALLOWED_HOSTS` e à qual a validação do Django acrescenta, ao percorrê-la, os domínios com posse viva (`SiteDomainClaim`). Nunca entra curinga nem padrão com ponto inicial, e `"*"` continua recusado em produção.

- **Atraso:** os domínios ficam num cache por processo de 30 s (`custom_hosts`): comprovar ou revogar um domínio leva até 30 s para valer em cada processo.
- **Banco fora do ar:** os hosts estáticos continuam aceitos; os domínios próprios só enquanto houver um valor bom de menos de 5 minutos, depois nenhum (falha fechada), sem erro 500 na validação do host. Um host aceito pelo cache mas que deixou de ser reconhecido na classificação recebe o 404 neutro, nunca a plataforma.
- **Sessão:** no domínio próprio o cookie de sessão é só daquele host; o login é local e OIDC/SAML não são oferecidos ([login no endereço da organização](authentication-sso.md#login-no-endereço-da-organização)).
- **HSTS:** a resposta num domínio próprio leva `Strict-Transport-Security: max-age=<SECURE_HSTS_SECONDS>` sem `includeSubDomains` nem `preload`, para não forçar HTTPS nos outros subdomínios do cliente; no domínio base o cabeçalho continua o de sempre.
- **Proxy e TLS:** o proxy repassa o cabeçalho `Host` original e emite um certificado por domínio próprio; a aplicação não emite certificado. Para emissão sob demanda, o proxy pergunta `GET http://<DJANGO_HEALTH_HOST>/health/site-host/?domain=<host>`: 200 (corpo vazio) para um endereço de organização ativo, por slug ou por domínio próprio comprovado; 404 para todo o resto. A rota só responde no host de saúde, fica fora do redirecionamento HTTPS (`SECURE_REDIRECT_EXEMPT`) e só diz sim ou não. Sem emissão automática, o domínio comprova, mas o navegador mostra erro de certificado até o operador emitir.
- **Links:** emails de uma organização com domínio próprio comprovado usam esse domínio (o mais antigo, se houver mais de um).

### Provedor OIDC para clientes nativos (#603)

O provedor ([autenticação](authentication-sso.md#provedor-oidc-para-clientes-nativos-603)) só liga com uma chave RSA de assinatura, que nunca vai para o repositório nem para o SQLite da instância:

- `IDP_OIDC_PRIVATE_KEY`: o PEM da chave, no ambiente; ou `IDP_OIDC_PRIVATE_KEY_FILE`: o caminho de um arquivo PEM provisionado pelo operador com modo 0600 (os dois juntos são recusados na partida);
- `IDP_OIDC_PRIVATE_KEYS_FILE` (opcional): um JSON `[{"pem": "...", "not_before": "<ISO 8601>", "expires_at": "<ISO 8601>"}]` para a rotação.

```sh
openssl genpkey -algorithm RSA -pkeyopt rsa_keygen_bits:2048 -out /etc/skeleton/oidc-key.pem
chmod 600 /etc/skeleton/oidc-key.pem   # IDP_OIDC_PRIVATE_KEY_FILE=/etc/skeleton/oidc-key.pem
```

**Rotação:** publique a chave nova antes de ela assinar, com `not_before` no futuro (o JWKS já a anuncia, e os clientes a guardam); a partir de `not_before` ela assina os tokens novos; mantenha a antiga até o fim da validade do último id_token que ela assinou e então dê a ela um `expires_at` ou retire-a. A chave assina só id_tokens (os access tokens são opacos), então a troca não derruba sessões de dispositivos.

**Host canônico:** defina `SKELETON_PUBLIC_URL` com a origem pública (`https://app.example.com`): ela é o issuer e a origem de todos os endpoints do discovery, também atrás do proxy e para quem chega por um endereço de organização. Sem ela, o issuer vem da requisição, e o proxy precisa repassar o `Host` original e `X-Forwarded-Proto` (`SECURE_PROXY_SSL_HEADER` dos settings de produção). Aplique as migrations (`allauth_idp_oidc` e `sk_accounts.0010`) em todos os ambientes antes de registrar o primeiro cliente com `manage.py native_client register`.

### Primeira instalação pelo setup (#146)

Uma instância nova não precisa de `.env` nem de comandos: enquanto faltar alguma chave exigida (`secret_key`, `allowed_hosts`, `database_url`) no ambiente e no SQLite da instância, o WSGI (`config.wsgi`, perfil de produção) e o `manage.py runserver` sobem em **modo setup** (`config.settings.setup`). Eles também continuam em modo setup enquanto um setup começado não for concluído: os passos "Instância" e "Banco runtime" já gravam as três chaves, e um reinício antes da conclusão volta ao setup no passo em que parou, em vez de subir com settings de produção ainda sem cache, email e o resto (#453). Conta como começado o setup que registrou algum passo; um token emitido sozinho não conta. Quem começar o setup pela tela e decidir configurar a instância de outro jeito sai com `SKELETON_SETUP=off`. Nesse modo não há alias de banco, a sessão é um cookie assinado com uma chave derivada da chave mestra, e só `/setup/` e as sondas de saúde respondem; qualquer outra rota leva a `/setup/`. `SKELETON_SETUP=off` desliga a troca (ferramentas e instâncias configuradas por outro meio). O entrypoint ASGI (`config/asgi.py`) não entra em modo setup: usa `config.settings.production` direto. O runtime suportado é WSGI/Gunicorn ([arquitetura](architecture.md)).

1. **Token:** no primeiro start, o processo gera um token de uso único (24 h), grava só o SHA-256 no SQLite, imprime o token uma vez no log e o escreve em `setup-token` (0600, ao lado do SQLite). Token errado, reutilizado ou expirado é recusado (403), com limite de 5 tentativas por IP em 15 minutos (429), contadas por processo. O token certo é conferido antes do limite e entra mesmo depois de tentativas erradas do mesmo endereço (atrás de um proxy, todos os clientes compartilham um IP); cada recusa gera o evento de segurança `setup.token_rejected`, sem o token (#290). Um reinício sem token aberto gera outro. Sem reiniciar, `manage.py instance setup-token` (no Compose, `docker compose exec web python manage.py instance setup-token`) revoga os tokens abertos e emite um novo, impresso só na saída do comando e gravado em `setup-token`; depois do setup concluído, o comando recusa e não emite nada (#466). A sessão do setup é um cookie assinado renovado a cada requisição, que expira depois de 2 horas sem uso (`SESSION_SAVE_EVERY_REQUEST`); sem sessão, as etapas levam ao token com o aviso de sessão expirada.
2. **Instância:** hosts, origens CSRF, proxy e endereço público. O campo de hosts já vem com o host do pedido (sem a porta) e a confiança no proxy vem marcada quando o pedido traz `X-Forwarded-Proto`. Com as origens CSRF em branco e o endereço público informado, o setup grava o endereço público como origem confiável, o que evita o erro de CSRF mais comum depois do setup (#463). O fuso horário padrão (`default_timezone`) vem com `America/Sao_Paulo` selecionado (#467). O endereço público vem sugerido com a origem do pedido, que links e provedores de pagamento precisam (W037). O setup gera `SECRET_KEY` e os keyrings MFA, de provedores, de integração e de cobrança (`BILLING_ENCRYPTION_KEYS`, sem o qual `skeleton.E037` impede a partida com a cobrança ligada, #654), cifrados no SQLite e nunca exibidos.
3. **Banco runtime:** servidor, porta, nome do banco, usuário (`skeleton_runtime`) e senha em campos separados; o setup monta a URL com percent-encoding (`build_dsn` em `skeleton/setup/database.py`), então a senha pode ter qualquer caractere. A URL completa continua aceita no bloco "Avançado" (para `sslmode` e outros parâmetros) e, quando preenchida, os campos são ignorados. A role é recusada se for superuser, tiver `BYPASSRLS`, for dona do schema `skeleton` ou tiver `CREATE` nele, ou se o schema não existir (provisione com `deploy/postgres/bootstrap.sql`); cada recusa diz a role a usar (#464).
4. **Schema:** pede só usuário (`skeleton_owner`) e senha; banco, host e porta vêm da etapa anterior e aparecem na tela (a URL completa vai no bloco "Avançado"). A credencial owner (dona do schema, mesmo banco, outra role) roda `migrate`, `sync_table_catalog` e o check do schema **só na memória dessa requisição**: ela não vai para o SQLite, o log nem a sessão.

   Uma falha de conexão nas duas etapas aparece no campo a corrigir, com uma mensagem escrita pelo setup; a mensagem do driver, o usuário e a senha nunca aparecem, e o log `skeleton.setup` registra só `setup.database_connect_failed code=... error_class=... sqlstate=...`. Antes do driver, o setup testa a resolução do nome e a conexão TCP (5 s), o que separa as causas de rede:

   | Código | Campo | Causa |
   |---|---|---|
   | `dns` | Servidor | o nome não resolve (no Docker, use o nome do serviço, não `localhost`) |
   | `refused` | Porta | nada escuta no endereço e porta, ou um firewall recusa |
   | `timeout`, `network` | Servidor | o servidor não respondeu em 5 s ou não é alcançável |
   | `auth` | Senha | usuário ou senha recusados (SQLSTATE `28P01`/`28000`, ou o texto do servidor quando o psycopg não traz SQLSTATE, como nas falhas de conexão) |
   | `database_missing` | Nome do banco | o banco não existe (`3D000`) |
   | `too_many_connections` | — | excesso de conexões (`53300`) |
   | `connect_failed` | — | outra falha; a tela mostra a classe do erro |
5. **Email** (com envio de teste), **cache e arquivos** (validações do E024 e pasta gravável; só a seção da opção escolhida, pasta local ou S3, aparece, por CSS, #729) e **administrador e organização** (`bootstrap_identity`, senha pelos validadores do Django; reenviar a etapa com o mesmo e-mail não altera a senha, e a tela avisa disso). O setup aplica as regras do perfil com que a instância vai subir depois dele, que `select_settings_module` registra em `SKELETON_SETUP_TARGET` (#457): pelo WSGI de produção, o email precisa ser SMTP (`smtp+tls://`, `smtp+ssl://` ou `smtp://`) e o cache precisa ser um Redis compartilhado (`redis://` ou `rediss://`), porque `config.settings.production` não sobe sem os dois; só com os perfis `development` e `test` valem os backends de email de desenvolvimento e o cache local (`target_is_strict` em `skeleton/setup/mode.py`; um perfil desconhecido conta como produção, #463). As telas mostram como obrigatório o que o perfil de destino exige. Na etapa "E-mail", servidor, porta, segurança (STARTTLS, SSL/TLS ou nenhuma, que viram `smtp+tls://`, `smtp+ssl://` e `smtp://`), usuário e senha vão em campos separados, com usuário e senha codificados na URL; a URL completa fica no bloco "Avançado" e, em produção, só um esquema SMTP é aceito nela. Fora de produção, "Configurar depois" segue sem gravar `email_url`: a etapa fica como "depois" (`skipped` no estado do setup), não bloqueia a conclusão, e a tela "Concluir" lembra de gravar `email_url` e `default_from_email` em `/platform/instance/` (#465). Uma falha do envio de teste aparece no campo a corrigir, classificada pela classe da exceção (autenticação, remetente, destinatário, segurança, servidor não encontrado, porta recusada, tempo esgotado, conexão fechada); a resposta do servidor SMTP nunca é exibida, e o log registra só a classe, sem traceback para essas falhas conhecidas. Na etapa "Cache e arquivos", o endereço do Redis (`redis://redis:6379/0`) e a senha vão em campos separados, e o campo opcional "Prefixo das chaves no cache" (`cache_key_prefix`, sugerido a partir do primeiro host da instância) separa as chaves de instâncias que usam o mesmo Redis (#675): o endereço com `@` é recusado, e a senha é codificada (percent-encoding) e inserida na URL gravada em `cache_url`, cifrada; ela nunca é reexibida. Uma instância que concluiu o setup sem Redis antes disso grava o cache com `manage.py instance set cache_url --stdin`.
6. **Conclusão:** antes de marcar o setup como concluído, "Conferir e concluir" carrega o perfil de destino num processo filho (o mesmo interpretador, sem shell, com `SKELETON_SETUP=off`, 60 s de limite) e roda `manage.py check` (`skeleton/setup/preflight.py`, #467). Se a instância não subiria (falta de Redis, SMTP, hosts), a tela responde 400 com o motivo e o setup continua aberto para corrigir; a saída mostrada e registrada no log tem todo trecho `esquema://…` trocado por `[URL]` e é cortada em 300 caracteres. Se não for possível iniciar o processo filho, o setup conclui com um aviso. Depois de concluído e até o processo reiniciar, toda rota fora de `/health/` e `/static/`, inclusive `/setup/`, responde 503 com `Retry-After: 5` e uma página de espera que se atualiza sozinha, sem formulário: nenhuma view do setup roda mais. Depois do reinício, em modo normal, `/setup/` responde 404, porque a rota não existe. A tela de conclusão leva ao login sozinha quando o reinício é automático e, com reinício manual, lista os comandos (`docker compose restart web worker scheduler`, `systemctl restart`, `runserver`). O pedido de reinício: `SKELETON_SETUP_RESTART=exit` encerra o processo para a política de reinício do container (o código de saída 3 é o `WORKER_BOOT_ERROR` do Gunicorn, que faz o mestre encerrar o servidor inteiro, não só aquele worker), o Gunicorn recebe `SIGHUP` e o `runserver` recarrega. Com `gunicorn --preload`, o `SIGHUP` não recarrega os settings: os workers novos herdam os do setup, e o serviço precisa ser reiniciado por inteiro. Sem supervisor conhecido, a página pede para reiniciar o serviço. Uma falha inesperada nas etapas de schema, email ou administrador mostra na tela só a classe do erro (as falhas de conexão com o banco são classificadas, como no item 3); o log `skeleton.setup` registra a etapa e o traceback, sem nenhum valor do formulário (#290).

**Retomar o setup (#466).** O que já foi salvo continua no SQLite da instância: ao voltar a uma etapa salva, os campos vêm preenchidos com os valores não secretos gravados (hosts, origem, banco, servidor SMTP, Redis sem a senha, armazenamento), e um campo de senha em branco mantém o segredo já gravado; senhas e URLs com credencial nunca vão para o HTML. Um valor gravado ilegível só deixa o campo em branco. As etapas "Tabelas do banco" e "Administrador" exigem as anteriores (banco; banco e tabelas) e, quando falta uma, levam a ela com a explicação; as demais podem ser feitas em qualquer ordem.

Todas as telas são formulários comuns com CSRF, sem JavaScript. A página inicial lista o que ter em mãos e leva à primeira etapa pendente; cada etapa mostra "Etapa N de 6", um parágrafo de introdução, a marca "(opcional)" nos campos não obrigatórios, o botão "Voltar" e, depois de salva, a mensagem de sucesso na etapa seguinte. Em modo setup, `SKELETON_SETUP_ALLOWED_HOSTS` (padrão `localhost,127.0.0.1`) define os hosts aceitos e `SKELETON_SETUP_SECURE_COOKIES` controla os cookies secure (padrão: ligado fora de `DJANGO_DEBUG`).

Atrás de um proxy que termina TLS, o Django em modo setup vê a requisição como `http://`, enquanto o navegador manda `Origin: https://<host>`; sem mais nada, todo POST do setup, a começar pelo do token, responde 403 de CSRF. A confiança no proxy gravada no passo **Instância** (`DJANGO_TRUST_PROXY`) só vale depois do setup, então o próprio setup recebe a declaração por uma variável de bootstrap: `SKELETON_SETUP_TRUST_PROXY=true` liga `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` **só em modo setup** (#442). O padrão é o `DJANGO_TRUST_PROXY` do ambiente ou do SQLite, senão desligado; o `X-Forwarded-Proto` nunca é confiado sem uma das duas. Valem as mesmas ressalvas do `DJANGO_TRUST_PROXY`: ligue só se o proxy remover ou substituir o `X-Forwarded-Proto` recebido do cliente e se o backend não for acessível direto, senão qualquer cliente forja `https`. Ela não grava nada na instância: no passo **Instância**, ligue também a confiança no proxy para o modo normal. A alternativa, sem confiar no proxy, é pôr a origem exata em `DJANGO_CSRF_TRUSTED_ORIGINS` no ambiente, que então vence o SQLite também depois do setup.

### Alterar a configuração depois do setup (#147)

`/platform/instance/` lista cada chave do catálogo com a origem efetiva (ambiente, SQLite da instância, padrão) e o valor; segredos aparecem só como definidos ou não. Só o administrador da plataforma acessa (403 para os demais, MFA quando configurada), e cada alteração exige autenticação recente. Chaves vindas do ambiente ficam somente leitura (409 ao tentar gravar). As chaves obrigatórias só podem ser substituídas: sem o valor no ambiente, "Voltar ao padrão" não aparece e o POST responde 409, porque o próximo reinício cairia no modo setup, que já foi concluído e responde 404 (#267). A URL do banco passa pela mesma validação de privilégios do setup. Cada mudança entra no `change_log` do SQLite (chave, ação e `platform:<usuário>`) e gera o evento de segurança `instance.setting_changed`, sempre sem o valor. As mudanças valem depois do reinício; o botão "Pedir reinício" usa o mesmo mecanismo da conclusão do setup.

Docker usa Python 3.14-slim, fixado por versão de patch e digest do índice multi-arquitetura nos dois Dockerfiles (o Dependabot propõe a troca, #309), bibliotecas XML/xmlsec, usuário não-root e Gunicorn WSGI. O contexto de build exclui `var/` (SQLite e chave mestra da instância, PostgreSQL local), `.private/`, `.sso-fixtures/` e `*.sqlite3`, `*.pem` e `*.key` em qualquer profundidade, e nenhum estágio grava o contexto numa camada: o estágio `project-assets` lê `templates/` por bind mount, o que exige BuildKit (padrão desde o Docker 23; o builder legado falha no `RUN --mount`, #248). Collectstatic é parte do build; WhiteNoise segue SecurityMiddleware. Produção usa CompressedManifestStaticFilesStorage e STATIC_ROOT=staticfiles; dev usa runserver/staticfiles. Não inicie migrations em cada worker.

O `compose.yaml` é **um nó de app** de uma instância em alta disponibilidade (#685): o agente de deployment e os serviços `web`, `worker` e `scheduler`, sem PostgreSQL nem Redis, que ficam no [servidor de dados](#provisionamento-postgresql) (`deploy/data-server/compose.yaml`, um projeto por instância). Cada nó roda este arquivo como projeto próprio, no próprio host, atrás de um balanceador que checa `/health/ready/`; o guia completo, com a arquitetura e os roteiros, está em [alta disponibilidade](high-availability.md). Para avaliar num host só, com PostgreSQL e Redis juntos, use o `compose.sandbox.yaml`, que continua completo. Nenhum serviço monta o socket Docker, e a credencial owner existe só no serviço `agent` de um nó candidato.

| Serviço | Papel |
|---|---|
| `agent` | Imagem do alvo `agent` do `Dockerfile`. O nó é candidato (com `SKELETON_MIGRATION_DATABASE_URL`, pode virar o primário que migra) ou seguidor (`SKELETON_AGENT_ROLE=follower`, com `SKELETON_COORDINATION_DATABASE_URL` da role `skeleton_agent` e sem a owner: um seguidor com ela não parte, `follower_has_owner`). O bootstrap escreve a chave `cluster` do `agent.json` a partir de `SKELETON_NODE_ID`, `SKELETON_CLUSTER_URL`, `SKELETON_CLUSTER_PREFERRED` e dos arquivos do canal em `SKELETON_CLUSTER_DIR` (`ca.crt`, `node.crt`, `node.key` e `secret`), e o marcador de drenagem em `/control/drain`. O canal entre agentes é publicado em `SKELETON_CLUSTER_BIND:SKELETON_CLUSTER_PORT` (padrão `127.0.0.1:9443`; use o endereço privado do nó). Volumes: `agent_private`, `agent_state` (com dois ou mais candidatos, um caminho compartilhado em `SKELETON_AGENT_STATE_SOURCE`), `releases`, `control` e `control_state` (só leitura). |
| `web`, `worker`, `scheduler` | Imagem de runtime com o [inicializador](#serviços-em-containers-sem-socket-docker), que roda com `--wait-for-agent`: o serviço só sobe quando o agente confere que o nó roda o release do cluster. Montam `releases` e `control` só para leitura, `control_state` e os anexos (`SKELETON_FILES_SOURCE`, um caminho de volume compartilhado; ou S3 na configuração). Recebem `SKELETON_INSTANCE_MODE=cluster`, `SKELETON_NODE_ID` e `SKELETON_DRAIN_MARKER`. O Gunicorn usa `SKELETON_WEB_WORKERS` (padrão 3) e `SKELETON_WEB_THREADS` (padrão 1; mais de 1 usa o worker `gthread`). |

A configuração da instância vem do ambiente, a mesma em todos os nós (modo cluster, #673): `SKELETON_INSTANCE_ENV_FILE` (obrigatória) nomeia o arquivo privado gerado por `manage.py instance export-env`, carregado com `env_file` em todos os serviços, inclusive no agente, que a repassa aos perfis dele. Ali ficam a URL do banco (com `sslmode=verify-full` até o servidor de dados), o cache com `SKELETON_CACHE_KEY_PREFIX`, o email, as chaves e os anexos em S3 ou `SKELETON_FILES_SHARED=true`. Some ao que o servidor de dados exige: nós × (workers × threads do Gunicorn + worker + scheduler) + agente cabem no `max_connections` do PostgreSQL, ou use o PgBouncer do servidor de dados para a role runtime.

1. **Servidor de dados:** um projeto `deploy/data-server/compose.yaml` por instância.
2. **Configuração:** rode o setup numa cópia `standalone` (por exemplo o sandbox, apontado para o servidor de dados) e `manage.py instance export-env --output instancia.env`; guarde o arquivo no cofre de segredos e entregue-o a todos os nós.
3. **Primeiro nó:** um candidato com `SKELETON_CLUSTER_PREFERRED=true`. O agente pega a trava do primário, instala a geração 0 a partir do release publicado (ou `SKELETON_CORE_VERSION`), publica o release do cluster e sobe os serviços. Crie o administrador com `manage.py bootstrap_skeleton` dentro do `web`.
4. **Demais nós:** cada um entra sozinho: o agente instala, pelo canal entre agentes, o release que o cluster roda e só então sobe os serviços.
5. **Depois:** atualizações e apps pela tela [`/platform/updates/`](updates.md) de qualquer nó, que encaminha ao primário; uma configuração nova se aplica mudando o arquivo de ambiente e rodando o reinício em rolagem.

```sh
docker compose -p no-a --env-file /srv/skeleton/no-a.env up -d   # um nó; o .env do projeto tem as variáveis acima
docker compose -p no-a exec agent python -m deployment_agent --config /agent/private/agent.json cluster status
```

O healthcheck do `web` (`python -m skeleton.api.healthcheck`) consulta `/health/live/` na porta local sem iniciar o Django e escolhe o Host assim: `localhost` em modo setup; depois, `DJANGO_HEALTH_HOST` se estiver definido (ambiente ou SQLite da instância), senão o primeiro host de `DJANGO_ALLOWED_HOSTS`. Definir `DJANGO_HEALTH_HOST` só é preciso quando o primeiro host da lista não serve para a sonda (por exemplo, um curinga `.example.org`); ele tem de estar na allowlist. No `compose.sandbox.yaml`, a URL do web usa os hostnames `postgres`/`redis` da rede Compose; no `compose.yaml`, os endereços do servidor de dados (num host só, `host.docker.internal` alcança o host).

Tudo vem ligado por padrão: `SKELETON_BUNDLED_APPS` (`calendar,ledger`), `SKELETON_ENABLE_EXAMPLE_APP` e `SKELETON_BILLING_ENABLED`, cada um desligável pela variável; a cobrança ligada não cobra nada sem produtos, preços e conta de provedor.

**Endurecimento (#318).** `agent`, `web`, `worker` e `scheduler` herdam o bloco `x-hardened`: `read_only: true`, `cap_drop: ["ALL"]`, `security_opt: ["no-new-privileges:true"]` e um `tmpfs` em `/tmp` (512 MiB, para uploads de pacotes e anexos e o heartbeat do Gunicorn). Graváveis são só os volumes montados sem `:ro` e o `/tmp`: quem usa `PROMETHEUS_MULTIPROC_DIR` monta esse diretório como volume; um projeto derivado que grava em outro caminho precisa montar um volume para ele. O Gunicorn sobe com `--no-control-socket` em todos os comandos (imagens, arquivos Compose e unit systemd): desde a versão 25.1 ele cria por padrão um socket de controle em `$HOME/.gunicorn/`, o que falha na raiz somente leitura e sob `ProtectHome`; o projeto não usa o `gunicornc`, e o agente controla o web por sinais e pelo supervisor (#441). A senha do Redis chega pelo ambiente do container e não aparece no `command` renderizado (`docker compose config`); o servidor continua passando pelo `docker-entrypoint.sh` da imagem, que troca root pelo usuário `redis`. A app de exemplo entra na imagem por padrão; `docker build --build-arg INCLUDE_EXAMPLES=false .` deixa `examples/` só com o `__init__.py`.

Configure proxy HTTPS em frente ao web. Se ele termina TLS, habilite DJANGO_TRUST_PROXY=true somente se remover/substituir X-Forwarded-Proto recebido do cliente; não confie em cabeçalho público. Defina DJANGO_ALLOWED_HOSTS e DJANGO_CSRF_TRUSTED_ORIGINS com hosts/origens exatos. Essas chaves (e DJANGO_TRUST_PROXY, falso por padrão) são da configuração da instância: no setup, em `/platform/instance/` ou, como sobrescrita, no `.env.instance`; o `compose.yaml` não as define. Para o próprio setup atrás desse proxy, `SKELETON_SETUP_TRUST_PROXY=true` no `web` (padrão `false` no `compose.yaml`; nas units systemd, no `runtime.env`), com a mesma ressalva ([setup](#primeira-instalação-pelo-setup-146)). Não abra CORS/domínios de cookies wildcard. Para SSO no container, monte JSON privado read-only e injete SSO_PROVIDER_CONFIG_PATH + referências env; Compose base não provisiona IdP de produção.

Rate limiting de IP usa a topologia explícita `DJANGO_TRUSTED_PROXY_COUNT` (número de proxies confiáveis no X-Forwarded-For) **ou** `DJANGO_TRUSTED_CLIENT_IP_HEADER` (por exemplo X-Real-IP sobrescrito pelo proxy). Não configure ambos. Exigem DJANGO_TRUST_PROXY=true; defaults 0/sem header ignoram cabeçalhos forjados e usam REMOTE_ADDR. Bloqueie acesso direto ao backend quando confiar no proxy e remova/substitua cabeçalhos recebidos do cliente; confiar em TLS/proto não habilita automaticamente confiança em IP. Dois clientes atrás do proxy não devem compartilhar buckets de login/reset.

### Content-Security-Policy

Toda resposta do web e do setup leva `Content-Security-Policy` com a política `SKELETON_CSP` de `skeleton/conf/base.py` (#266): `default-src`, `script-src`, `font-src`, `connect-src` e `base-uri` só `'self'`; `style-src 'self'` mais um nonce por resposta, usado só pela paleta da marca da organização; `img-src 'self' data:` (o QR code do TOTP é uma imagem `data:`); `object-src 'none'` e `frame-ancestors 'none'`. Não há `'unsafe-inline'` nem `'unsafe-eval'`. Não há `form-action`: o login por SSO começa com um POST para o próprio site que redireciona ao provedor de identidade, e o Chromium aplica `form-action` também ao destino do redirect. O download de anexo mantém a própria política (`sandbox`), que o middleware não sobrescreve.

Templates do núcleo e das apps não usam `<script>` inline, atributo `style=` nem handlers `on*=`: script e estilo vêm de arquivos estáticos, dados passam por `json_script` e cores vindas de dado entram por `data-swatch`, aplicadas pelo `site.js`. Templates de email não passam pelo navegador e continuam com estilo inline.

Para liberar uma origem a mais, estenda a política no overlay de settings em vez de reescrevê-la:

```python
from skeleton.conf.base import SKELETON_CSP

SECURE_CSP = {**SKELETON_CSP, "img-src": [*SKELETON_CSP["img-src"], "https://cdn.example.com"]}
```

Para uma view só, use `django.views.decorators.csp.csp_override` (ou `csp_report_only_override` para observar antes). Ao introduzir uma origem nova, publique primeiro em `SECURE_CSP_REPORT_ONLY` e confira o console do navegador nas telas afetadas.


## Provisionamento PostgreSQL

**Pré-requisitos:** operador autorizado, banco novo vazio, cliente `psql` e DSN administrativo privado. O bootstrap prepara roles/schema, mas não cria o banco nem salva senhas. Não o trate como migração de uma instalação existente.

```sh
# ADMIN_DSN referencia banco recém-criado; segredo via pgpass/secret store.
psql "$ADMIN_DSN" -v ON_ERROR_STOP=1 -f deploy/postgres/bootstrap.sql
psql "$ADMIN_DSN"
```

O bootstrap cria a extensão `pg_trgm` quando ela está disponível ([#181](https://github.com/alaorneto/skeleton/issues/181)). Ela é opcional e confiável a partir do PostgreSQL 13. Com ela, o picker tolera erro de digitação; sem ela, procura só por prefixo. A busca textual das listas não precisa de extensão: a função `skeleton.immutable_unaccent` vem da migration `sk_system.0008`. Numa instância que já existe, rode de novo o bloco do `pg_trgm` do bootstrap como operador e depois `manage.py migrate`. O índice do picker é criado no tipo que a extensão permite no momento da migration.

**Limites de espera da role runtime (#318).** O bootstrap define, só para `skeleton_runtime`, `lock_timeout = '10s'` (um comando que espera um bloqueio por mais que isso falha com SQLSTATE `55P03`) e `idle_in_transaction_session_timeout = '60s'` (a sessão ociosa dentro de uma transação aberta é encerrada). Uma transação parada segurando, por exemplo, o bloqueio da organização deixa de bloquear sem prazo as demais mudanças daquela organização. `statement_timeout` não é definido, e `skeleton_owner` fica sem limites (migrations e restauração podem esperar). O erro chega ao Django como `OperationalError` e, numa request, vira 500 com rollback: nada fica gravado pela metade. Como o bootstrap só roda em banco novo, numa instalação existente o operador aplica, com a credencial administrativa, `ALTER ROLE skeleton_runtime SET lock_timeout = '10s';` e `ALTER ROLE skeleton_runtime SET idle_in_transaction_session_timeout = '60s';`, e reinicia web, worker e agendador (valem para conexões novas). Para desfazer: `ALTER ROLE skeleton_runtime RESET lock_timeout;` e o mesmo com o outro parâmetro.

No psql interativo, use `\password skeleton_owner` e `\password skeleton_runtime`; nunca senha na CLI/log/SQL versionado. Roles existentes também precisam ter atributos revisados pelo operador.

**Servidor de dados (#679).** Para tirar o PostgreSQL e o Redis dos nós de app, o `deploy/data-server/compose.yaml` sobe, num servidor à parte, **um projeto Compose por instância**: `docker compose -p <instância> --env-file /srv/data/<instância>.env -f deploy/data-server/compose.yaml up -d`, com as variáveis de `deploy/data-server/instance.env.example` (arquivo 0600, nunca versionado). Cada instância tem o próprio cluster PostgreSQL (a imagem `deploy/coolify/postgres.Dockerfile`, com o bootstrap revisado), o próprio Redis (senha, `--maxmemory` com `volatile-lru`, para nunca despejar um marcador de antirreuso do TOTP) e, com o perfil `pooler`, um PgBouncer em modo transação só para a role runtime. O PgBouncer chega ao PostgreSQL pelo socket local de um volume que só ele divide (a única entrada `local` da role runtime no `pg_hba`) e aplica aos próprios clientes a mesma regra do PostgreSQL: TLS obrigatório, SCRAM e só `DATA_CLIENT_CIDR` (o entrypoint escreve `pgbouncer_hba.conf` e a lista de usuários, 0600 para o usuário do PgBouncer; a senha nunca fica no ambiente do container dele). Ele lê `server.crt` e `server.key` de `DATA_TLS_DIR` com o próprio usuário (uid 70): deixe a chave legível para ele. **Nunca ponha duas instâncias no mesmo cluster:** as roles do núcleo têm nome fixo e são globais no cluster, então as duas dividiriam a role runtime e a senha dela; `skeleton.W051` (com `check --database default`) avisa quando a role runtime pode conectar em outro banco do cluster. O PostgreSQL publica a porta só em `DATA_BIND_ADDRESS`, aceita as roles da instância só por TLS (`DATA_TLS_DIR` com `server.crt` e `server.key`) e só da rede dos nós (`DATA_CLIENT_CIDR`, que o entrypoint `deploy/data-server/postgres-entrypoint.sh` escreve no `pg_hba.conf`), com SCRAM; o init revoga o `CONNECT` público no banco `postgres` e define a senha de `skeleton_agent` quando `SKELETON_AGENT_DB_PASSWORD` está definida. Nos nós, use `sslmode=verify-full` na URL. O servidor de dados sozinho não é alta disponibilidade: réplica com failover e Redis com Sentinel atrás de um endereço estável ficam por conta do operador ([alta disponibilidade](high-availability.md)). O `compose.sandbox.yaml` continua com o próprio PostgreSQL e Redis.

**Failover e pooler (#676).** Para o PostgreSQL com réplica e failover, liste os nós na URL e peça o primário: `postgres://skeleton_runtime:...@db-a:5432,db-b:5432/skeleton?target_session_attrs=read-write`. A libpq tenta os hosts em ordem; o setup aceita a lista quando ao menos um responde (no bloco "Avançado"), e o agente monta `PGHOST`/`PGPORT`/`PGTARGETSESSIONATTRS` em listas para o `pg_dump` e o `pg_restore`. Vários hosts sem `target_session_attrs` geram `skeleton.W050` (a conexão poderia cair numa réplica somente leitura). Com muitos nós de app, ponha um PgBouncer em modo transação (`pool_mode = transaction`) na frente da role runtime e ligue `DATABASE_TRANSACTION_POOLER=true`: os settings deixam de enviar `options=-c search_path=...` na partida (o PgBouncer recusa o parâmetro) e desligam os cursores do lado do servidor; o `search_path` passa a vir do `ALTER ROLE skeleton_runtime SET search_path` do bootstrap, e `skeleton.E050` (com `check --database default`) reprova a role sem ele. O contexto de organização usa `set_config(..., true)`, local à transação, e por isso funciona com o pooler. A owner (migrations) e o agente conectam sempre direto no PostgreSQL. `DATABASE_CONN_MAX_AGE` (padrão 60) controla o reuso de conexões por processo; atrás do pooler, `0` é o usual. Conta de conexões sem pooler: nós × (workers × threads do Gunicorn + worker + scheduler) + agente ≤ `max_connections`.

| Role | Privilégios/finalidade |
|---|---|
| skeleton_owner | LOGIN, NOSUPERUSER/NOCREATEDB/NOCREATEROLE, BYPASSRLS; ownership/schema/migrations offline |
| skeleton_runtime | LOGIN, NOSUPERUSER/NOCREATEDB/NOCREATEROLE/NOBYPASSRLS; sem ownership/membership privilegiada/CREATE/TRIGGER/TRUNCATE |
| skeleton_audit_writer | NOLOGIN/NOBYPASSRLS; dona das funções SECURITY DEFINER e INSERT audit, não escrita de negócio |
| skeleton_agent | LOGIN/NOBYPASSRLS; só o schema `skeleton_deploy` (coordenação dos agentes de um cluster, #678): lê o primário e informa o estado do próprio nó. Sem `USAGE` em `skeleton`; o web nunca a recebe. Defina a senha com `\password skeleton_agent` só se houver seguidores |

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

`/health/live/` só vivacidade; `/health/ready/` usa SELECT 1 e uma gravação no cache, sem secrets/schema/grants. Somente esses paths exatos são atendidos antes de sessão/autenticação/contexto transacional; DB parado mantém live=200 e ready=503 JSON. O cache não reprova a prontidão (#675): sem Redis, `ready` responde 200 com `{"status": "ok", "degraded": ["cache"]}` e o evento `health.probe_degraded`, porque todas as réplicas dividem o mesmo Redis e tirá-las do balanceador derrubaria o site inteiro. Enquanto o Redis estiver fora, o login, o MFA e o provedor OIDC falham (o allauth guarda no cache o limite de tentativas, o antirreuso dos códigos TOTP e os códigos de autorização); quem já está logado continua navegando, porque as sessões ficam no banco. Host continua validado; outros paths não recebem bypass. Health não substitui check de infraestrutura ou teste de isolamento. Proteja interfaces de operação na rede/proxy.

Leia as respostas como sinais diferentes: `live=200` significa que o processo responde; `ready=200` significa que o banco respondeu ao probe (e o cache, sem `degraded`). Nenhuma delas prova isolamento entre organizações, SMTP, login ou versão correta do código. Para releases gerenciados, confira também `/health/release/` e os checks descritos abaixo.

O email é configurado por `EMAIL_URL` e vira `MAILERS["default"]` do Django 6.1 ([#127](https://github.com/alaorneto/skeleton/issues/127)); o núcleo não define mais as settings `EMAIL_*` por conexão, que o 6.1 deprecou. O conversor (`skeleton/conf/mailers.py`) usa o parser do django-environ e aceita:

| `EMAIL_URL` | Backend e opções |
|---|---|
| `smtp://usuário:senha@host:porta` | SMTP, sem TLS |
| `smtp+tls://...` ou `smtps://...` | SMTP com STARTTLS (`use_tls`) |
| `smtp+ssl://...` | SMTP sobre SSL (`use_ssl`) |
| `consolemail://` | Console (só desenvolvimento: imprime links privados) |
| `filemail:///caminho` | Arquivos no diretório indicado |
| `memorymail://` e `dummymail://` | Memória e descarte (testes) |

No SMTP, a query aceita `timeout` (segundos, inteiro positivo), `ssl_keyfile` e `ssl_certfile`. Esquema desconhecido, parâmetro desconhecido, SMTP sem host e `filemail://` sem caminho são recusados com `ImproperlyConfigured` na partida, em vez de ignorados. Usuário e senha com caracteres especiais vão codificados na URL (`%40` para `@`). A senha fica só no ambiente (ou no SQLite cifrado da instância) e sai redigida no relatório de erro do Django, porque a chave `password` de `MAILERS` é filtrada. Produção continua recusando qualquer backend que não seja SMTP.

O SMTP tem prazo padrão de 30 s por operação (`EMAIL_TIMEOUT`, em segundos, no ambiente, ou `?timeout=` na URL): um servidor mudo nunca prende o job de email do worker (#297). `EMAIL_TIMEOUT` continua sendo uma variável de ambiente, não uma setting: o valor entra nas `OPTIONS` do SMTP.

Projetos derivados não podem misturar as settings antigas (`EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, `EMAIL_USE_SSL`, `EMAIL_TIMEOUT`, `EMAIL_SSL_KEYFILE`, `EMAIL_SSL_CERTFILE`, `EMAIL_FILE_PATH`) com `MAILERS`: o Django recusa a mistura. Para migrar um overlay, apague essas settings e use `EMAIL_URL`, ou redefina `MAILERS` inteiro. `DEFAULT_FROM_EMAIL`, `SERVER_EMAIL` e `EMAIL_SUBJECT_PREFIX` não mudam.

Redis compartilhado é necessário a rate limits entre workers; configure autenticação e rede privada. O probe usa REDISCLI_AUTH com expansão de ambiente, não senha literal/argv. SMTP entrega após commit de mensagens já materializadas e pré-validadas. Falha real é registrada por classe/UUID sem segredo e não vira 500 enganoso após dados confirmados: API recebe `X-Email-Delivery: failed`, HTML recebe aviso, e permanece um caminho explícito de reenvio. Convite reenviado revoga o antigo por tombstone e emite UUID/token novos na mesma transação, com revisão obrigatória; convite consumido não pode ser reenviado. Não há retry/outbox nem promessa de entrega distribuída/transacional. Isso vale para emails com token (allauth e convites). Os emails de **notificação** são outra coisa: saem por job (`notifications.email`), com retentativa e estado em `NotificationDelivery`, e por isso exigem um worker (`manage.py run_worker`) e o agendador de jobs periódicos rodando; sem worker, ficam na fila. `SKELETON_DEFAULT_LANGUAGE` (padrão `pt-br`) e `SKELETON_DEFAULT_TIMEZONE` (padrão `UTC`) ficam no catálogo da instância e valem para quem não escolheu um idioma ou fuso no perfil, nem tem um padrão na organização ([#170](https://github.com/alaorneto/skeleton/issues/170)). O setup grava o fuso escolhido na etapa "Instância" (`America/Sao_Paulo` já vem selecionado, #467); numa instância já instalada com `UTC`, ajuste com `manage.py instance set default_timezone America/Sao_Paulo` ou em `/platform/instance/` e reinicie. A exportação CSV ([exportação e importação](data-transfer.md)) roda no worker e respeita `SKELETON_EXPORT_MAX_ROWS` (padrão 100.000), `SKELETON_IMPORT_MAX_ROWS` (padrão 10.000) e `SKELETON_TRANSFER_RETENTION_DAYS` (padrão 7), todos no catálogo da instância. Defina `SKELETON_PUBLIC_URL` (a origem pública, como `https://app.example.com`) para que os links desses emails sejam absolutos. Sem uma origem válida (esquema e host, sem caminho), o `check --deploy` avisa com `skeleton.W035` (#279). A mesma origem vale para os convites emitidos sem requisição (por um job ou comando): sem ela, o convite é recusado com um erro que cita `SKELETON_PUBLIC_URL` ([#169](https://github.com/alaorneto/skeleton/issues/169)). Veja [notificações](notifications.md#email-imediato). Com a [cobrança](billing.md#provedores-de-pagamento) ligada, configure no painel de cada provedor o webhook `https://<origem pública>/billing/webhooks/<provedor>/<sysid da conta>/` (o endereço aparece na administração da plataforma), defina `BILLING_ENCRYPTION_KEYS` e mantenha o proxy encaminhando o corpo intacto: a assinatura é conferida sobre os bytes recebidos. Ainda com a cobrança ligada, os prazos de inadimplência (`SKELETON_BILLING_GRACE_DAYS`, `_SUSPEND_AFTER_DAYS`, `_CANCEL_AFTER_DAYS`, `_RETRY_SCHEDULE_DAYS` e `_PENDING_PAYMENT_HOLD_DAYS`) ficam no catálogo da instância, e o `billing.cycle` precisa do worker e do agendador. O **web push** é opcional: gere a chave VAPID com `manage.py generate_vapid_keys /caminho/privado/vapid.pem` (arquivo novo, 0600, fora do repositório e do diretório estático; o comando imprime a chave pública) e defina `WEBPUSH_VAPID_PRIVATE_KEY_FILE`, `WEBPUSH_VAPID_SUBJECT` (`mailto:` ou `https:`) e, se preciso, `WEBPUSH_ALLOWED_HOSTS`. As assinaturas exigem `INTEGRATION_ENCRYPTION_KEYS`. Trocar a chave VAPID invalida as assinaturas existentes, e cada pessoa precisa ativar o push de novo. Sem a chave, a seção de push não aparece e `/sw.js` responde 404.

Os logs da aplicação e do Django usam `SecurityJSONFormatter`, com um evento de conclusão `http.request` após resultado transacional/callbacks e UUID gerado pelo servidor, sem URL/body/cookies. Outcomes distinguem success, validation_error, conflict, access_denied, not_found e server_error; falha de entrega preserva o status de persistência e registra reason_code. status_code do Django é mapeado para status; error_class e target_sysid são seguros, sem mensagem/traceback bruto. Registros com exceção trazem `error_stack`: até 25 posições `arquivo:linha:função`, da mais externa para a mais interna, sem mensagem, código-fonte ou variáveis locais. Um readiness com falha registra `health.probe_failed` com `reason_code` `database_unavailable` ou `cache_unavailable`; a resposta HTTP continua sendo só 503. O agente de deployment escreve no stderr uma linha JSON `agent.internal_error`, no mesmo formato, antes de responder `agent_internal_error`; a thread que executa ou recupera uma operação faz o mesmo para qualquer exceção que não seja um código conhecido (#244). O comando Gunicorn da imagem não habilita access log bruto. Proxy, ingress, APM e capturas de HTTP também precisam redigir caminhos/query de convite, reset e callbacks (as integrações do núcleo com Prometheus, OpenTelemetry e Sentry já não enviam caminhos, ver [Observabilidade](#observabilidade-87)); não acrescente `--access-logfile -` sem formatter seguro. O backend de email console é somente desenvolvimento e imprime links privados por definição.

**Marca e tema da instância:** o nome do produto, as imagens e o tema ficam no PostgreSQL e valem em até 5 s em todos os processos, sem reinício ([aparência](appearance.md)). Um tema cujo CSS personalizado esconda a interface não tranca o administrador (as páginas de `/platform/appearance/` carregam o tema sem o CSS livre), e no servidor há a saída de emergência: `python manage.py appearance show` mostra a marca (nunca as chaves das imagens) e `python manage.py appearance reset-theme` volta ao tema do núcleo, auditado com o motivo `appearance.reset_theme`. `/brand/` é público e cacheável (`/brand/theme.css?v=<versão>` é imutável); se o proxy tiver cache próprio, não cacheie `/brand/theme.css` sem o `?v=` por mais de 5 minutos. Um tema empacotado (`.zip`, [pacote de tema](appearance.md#pacote-de-tema)) entra na implantação automatizada com `python manage.py appearance install-package <arquivo>`: idempotente (o mesmo pacote não grava nada, uma versão maior atualiza) e nunca ativa o tema; ative-o uma vez pela tela. Um tema trazido por uma app sai do catálogo quando a app é desinstalada; se era o ativo, vale o tema do núcleo até outro ser ativado. A limpeza das imagens substituídas e dos arquivos de pacotes removidos (`appearance.orphans`) depende do worker e do agendador.

**Limite de corpo no proxy:** configure no proxy um limite de corpo (`client_max_body_size` no Nginx, ou equivalente) próximo de `SKELETON_UPLOAD_MAX_BYTES` em todas as rotas e igual ou maior que `SKELETON_PACKAGE_MAX_BYTES` só em `/platform/updates/upload/`. O Django já interrompe o corpo multipart no limite (#242); o proxy evita que o corpo chegue ao processo.

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
| `skeleton_http_request_duration_seconds` (histograma) | `method`, `route`, `status` | Cada request, medido pelo middleware. `route` é o **padrão** resolvido (`<org:organization_id>tables/<str:table_key>/`: desde #479 as rotas web de organização usam o conversor `org`, antes `o/<uuid:organization_id>/…`), nunca o caminho; sem rota, `unresolved`; os probes usam `health/live/` e `health/ready/` |
| `skeleton_jobs` | `job_type`, `state` | Jobs por tipo registrado e estado; tipo não registrado entra como `other` |
| `skeleton_delivery_jobs` | `channel` (`webhook`, `email`, `push`), `state` | Jobs de entrega de webhook e de notificação |
| `skeleton_jobs_due`, `skeleton_jobs_oldest_due_lag_seconds`, `skeleton_jobs_dead` | — | `queue_health()`, o mesmo de `check_jobs` e de `/platform/jobs/` |
| `skeleton_job_workers` | `state` (`live`, `stale`) | Heartbeats dos workers |
| `skeleton_queue_metrics_up` | — | 1 quando a coleta leu a fila; 0 quando o banco falhou (o resto da coleta continua) |
| `skeleton_flow_instances` | `flow_key`, `status` | Instâncias de fluxo por fluxo e estado, somadas em todas as organizações (#420) |
| `skeleton_flow_tasks_open`, `skeleton_flow_tasks_overdue` | `flow_key` | Tarefas de fluxo abertas e as abertas com prazo vencido |
| `skeleton_flow_failed_service_steps` | `flow_key` | Etapas automáticas (de serviço) com falha, à espera de repetição ou cancelamento |
| `skeleton_flow_metrics_up` | — | 1 quando a coleta leu as tabelas de fluxo; 0 quando falhou (o resto da coleta continua) |

As métricas da fila são calculadas **na coleta** e cada processo reutiliza o resultado por `SKELETON_METRICS_QUEUE_TTL_S` segundos (padrão 15; uma falha de leitura nunca é guardada, #275), por consultas agregadas de leitura num `system_context` sem organização (as tabelas de jobs são globais). O worker não mantém contadores. Cada coleta faz algumas consultas agregadas sobre `Job` e `WorkerHeartbeat`; um intervalo de 15 a 60 s basta. Veja [jobs](jobs.md#métricas-da-fila).

**Gunicorn com vários workers (modo multiprocess).** Cada processo tem o próprio histograma de requests. Para somá-los, defina `PROMETHEUS_MULTIPROC_DIR` com um diretório privado e vazio, gravável pelo usuário do web, **antes** de o gunicorn iniciar, e esvazie-o a cada início do serviço:

```sh
export PROMETHEUS_MULTIPROC_DIR=/run/skeleton-metrics
rm -rf "$PROMETHEUS_MULTIPROC_DIR" && install -d -m 0700 -o skeleton "$PROMETHEUS_MULTIPROC_DIR"
gunicorn config.wsgi:application --workers 3 --timeout 120 --no-control-socket
```

O check de deploy `skeleton.W034` avisa quando as métricas estão ligadas fora de `DEBUG` sem a variável, e `skeleton.E034` reprova uma variável que não aponta para um diretório gravável; com um processo só atendendo o site, silencie o aviso com `SILENCED_SYSTEM_CHECKS = ["skeleton.W034"]` (#275). Com a variável, a coleta lê os arquivos de todos os processos (`MultiProcessCollector`) e as métricas da fila continuam vindo do banco. Só há histogramas por processo, sem gauges, então não é preciso o hook `child_exit`. Sem a variável, cada scrape vê apenas o processo que o atendeu. Em systemd, use `RuntimeDirectory=skeleton-metrics`, que o recria vazio a cada início. O worker de jobs não expõe métricas.

As métricas de fluxo seguem o mesmo cache. As tabelas de fluxo são de tenant (RLS vale também no contexto de sistema), então a coleta abre um `system_context` por organização ativa, até 2.000, e soma os resultados; o único rótulo é a chave do fluxo, declarada em código. As falhas de etapas automáticas são lidas do banco, não contadas no worker.

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

- **Request:** um span `SERVER` por request, com nome `<método> <padrão de rota>` (`GET <org:organization_id>`), método, rota, status e `skeleton.request_id`, o mesmo UUID do cabeçalho `X-Request-ID` e do log `http.request`. O contexto W3C (`traceparent`) de entrada é respeitado.
- **Job:** um span `CONSUMER` `job <code>` por execução no worker, com `skeleton.job_id`, `skeleton.job_type`, `skeleton.job_queue`, `skeleton.job_attempt`, `skeleton.job_state` e, na falha, `error.type` (a classe). Cada execução tem `skeleton.request_id` próprio, que também vai nos logs do job.
- **SQL:** um span `CLIENT` por statement, filho do request ou do job, com `db.system`, `db.name`, a operação (`SELECT`, `INSERT`...) e o `skeleton.request_id` de quem o executou.

Nenhum span leva URL, query string, host, IP ou user agent do cliente, texto SQL, parâmetros SQL, email, nome, corpo, cookie ou cabeçalho. A garantia não depende das instrumentações: o único exportador é `RedactingSpanExporter`, que reconstrói cada span com uma lista fixa de atributos, reduz o nome dos spans de SQL à palavra-chave inicial, mantém dos eventos de exceção só `exception.type` (sem mensagem nem stack trace) e remove a descrição do status. Os comentários SQL (`sqlcommenter`) e a captura de parâmetros ficam desligados. Atributos que você acrescentar num span próprio também passam por essa lista: fora dela, são descartados.

O exportador fala com o coletor que o operador configurou; não é saída controlada por tenant, por isso não passa por `skeleton.common.outbound_http`. Ponha o coletor numa rede privada.

Fora do escopo: o coletor e o backend de traces.

### Sentry (#177)

Instale o extra `[sentry]` e defina `SKELETON_SENTRY_DSN` (setting sensível da instância, cifrado no SQLite) com o DSN do projeto, `https://<chave>@<host>/<projeto>`. Sem o DSN, `sentry_sdk` não é importado nem inicializado; a variável `SENTRY_DSN` do próprio SDK é ignorada, porque o núcleo só chama `sentry_sdk.init` com `SKELETON_SENTRY_DSN`. `SENTRY_ENVIRONMENT` e `SENTRY_RELEASE` continuam lidos pelo SDK do ambiente.

O SDK é iniciado com `send_default_pii=False`, `max_request_body_size="never"` (o corpo nunca é lido), sem variáveis locais nem linhas de código-fonte, sem sessões e sem performance monitoring (traces são do OpenTelemetry). A integração do Django agrupa os erros pelo padrão de rota (`transaction_style="url"`). O nome da transação só sai quando o SDK o construiu do código (`transaction_info.source` `route`, `component` ou `task`) e não traz UUID, `@` nem `?`; uma requisição a rota inexistente, cujo nome seria o caminho bruto (`source` `url`), sai sem transação (#326).

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

Um ambiente de sandbox e testes numa VPS com [Coolify](https://coolify.io). Como todo deployment, ele roda o agente (#649): instala só releases publicados (não segue mais a `main`) e é atualizado pela tela `/platform/updates/`, com manutenção coordenada, backup antes da migration e recuperação. Tudo vem ligado: as apps first-party, a app de exemplo e a cobrança. Use só dados de teste.

O `compose.sandbox.yaml` da raiz é a instalação completa de um host só, para avaliação e testes: `agent`, `web`, `worker` e `scheduler` como no `compose.yaml`, mais o próprio PostgreSQL e o próprio Redis (que no `compose.yaml` de produção ficam no servidor de dados, #685), num nó só, com a configuração no SQLite da instância e o setup pela tela. Em relação ao `compose.yaml`, difere também em três pontos:

| Ponto | `compose.sandbox.yaml` |
|---|---|
| Portas | Nenhum serviço publica porta no host; o proxy do Coolify chega ao `web` na 8000 pela rede interna. |
| Funcionalidades | Ligadas de forma fixa (`calendar,ledger`, app de exemplo e cobrança), sem variável para desligar. |
| Proxy | `SKELETON_SETUP_TRUST_PROXY` e cookies seguros ligados para o setup atrás do TLS do Coolify; o agente confere a saúde com o Host `SANDBOX_HOST`. |

**Aviso de versão nova sem agente (#502).** Todo deployment suportado, este sandbox inclusive, roda o agente e vê as versões novas em `/platform/updates/`. Fora dele (desenvolvimento local, um projeto derivado que ainda não adotou o agente), o job abaixo pode ao menos avisar que saiu versão nova. É **opt-in**, porque a consulta revela ao GitHub o endereço IP da instalação: `SKELETON_UPDATE_REPOSITORY` (`dono/nome`; vazio, o padrão, desliga; também configurável na instância), `SKELETON_UPDATE_GITHUB_TOKEN_FILE` (opcional, só em variável de ambiente: arquivo com um token de leitura para repositório privado) e `SKELETON_UPDATE_CHECK_HOURS` (intervalo, padrão 24, mínimo 1). Com o repositório definido e sem `SKELETON_AGENT_SOCKET`, o job periódico global `system.update_check` faz uma única `GET` a `https://api.github.com/repos/<dono/nome>/releases/latest` (host fixo no código, saída por `skeleton.common.outbound_http`, `User-Agent: django-skeleton` sem versão nem identificação da instalação, redirecionamento não seguido), lê só `tag_name` e guarda no cache do Django, por 3 intervalos, a versão e o link `https://github.com/<dono/nome>/releases/tag/<tag>` montado localmente. Quem tem `updates.operate` vê nas páginas de `/platform/` (inclusive as de atualização) a faixa "Há uma versão nova do Skeleton (<versão>). Esta instalação não tem agente de deployment…", com o link "Ver a release". O aviso é **só informativo**: não verifica assinatura, não baixa nem instala nada, e a atualização continua sendo o processo de deploy do operador. Uma falha (404, 403, 429, 3xx, 5xx, corpo inválido) só deixa um registro de log com o código e mantém o resultado anterior. O cache precisa ser compartilhado entre o worker e o web (o Redis de produção); com cache local por processo, em desenvolvimento, o web não vê o que o worker gravou.

No resto, ele segue o `compose.yaml`: o agente e os serviços de aplicação (`web`, `worker` e `scheduler`) usam a mesma âncora `x-hardened` (sem capabilities, sem ganho de privilégio e raiz somente leitura, [#318](https://github.com/alaorneto/skeleton/issues/318)), o Redis recebe a senha pelo ambiente do container e não pelo comando renderizado, e o Gunicorn usa o mesmo `--timeout 120` (#298) e `--no-control-socket` (#441) da imagem.

### Criar o recurso

1. **DNS:** um registro A do host do sandbox (por exemplo `sandbox.example.com`) para o IP da VPS.
2. **Projeto:** no Coolify, crie um projeto e um environment `sandbox`.
3. **Recurso:** no environment, *+ New* → *Public Repository* (repositório privado: *Private Repository (with GitHub App)*), a URL do repositório e o branch `sandbox` ([abaixo](#o-branch-sandbox)), nunca a `main`. *Build Pack* `Docker Compose`, *Base Directory* `/` e *Docker Compose Location* `/compose.sandbox.yaml`.
4. **Variáveis** (aba *Environment Variables*). Gere cada senha com `openssl rand -hex 32`, só letras e dígitos, porque elas entram em URLs sem codificação:

   | Variável | Uso |
   |---|---|
   | `SANDBOX_HOST` | Host público, sem esquema: `sandbox.example.com`. O compose acrescenta `localhost` aos hosts do setup, porque é o Host que o healthcheck manda em modo setup (#436) |
   | `POSTGRES_PASSWORD` | Administrador do PostgreSQL; só o container do banco a recebe |
   | `SKELETON_OWNER_DB_PASSWORD` | Role owner; só o `agent` e o init do banco a recebem |
   | `SKELETON_RUNTIME_DB_PASSWORD` | Role runtime; digitada no setup |
   | `REDIS_PASSWORD` | `requirepass` do Redis |

5. **Domínio:** na configuração do serviço `web`, informe `https://<SANDBOX_HOST>:8000`. O sufixo `:8000` é a porta do container, não uma porta pública; o Coolify emite o certificado e o Traefik termina o TLS.
6. **Auto Deploy:** opcional. Ligado, o Coolify reconstrói as imagens quando o branch `sandbox` anda, o que só acontece numa release final nova; o release ativo continua o mesmo até a atualização pela tela.
7. *Deploy*. Na primeira vez, o `agent` instala o release publicado mais novo (ou `SKELETON_CORE_VERSION`) e cria o schema, e o `web` sobe em modo setup.

### O branch `sandbox`

O Coolify constrói as imagens a partir do branch do recurso, e a imagem não é só embalagem: o agente (`deployment_agent` e o wheel do núcleo dentro da imagem `agent`), o inicializador da imagem de runtime, o `Dockerfile`, o `.dockerignore` e o `compose.sandbox.yaml` vêm do branch. O código da aplicação, não: ele vem do release assinado que o agente instala. Apontado para a `main`, um *Redeploy* pegaria trabalho ainda não publicado (#724).

Por isso o recurso usa o branch `sandbox`, que só aponta para a última versão final publicada. O workflow "Artefatos de release" (`.github/workflows/release-artifacts.yml`, job "Branch do sandbox na última release") o move para o commit da tag depois de anexar os artefatos e o índice assinados, então o agente do sandbox nunca vê um release sem índice. Ele só anda para frente (fast-forward; um movimento que não é fast-forward falha o job) e fica parado numa pré-release ou num hotfix de uma linha antiga, que não são a release mais recente. Não faça push nele à mão; um ruleset que só deixe o `github-actions[bot]` atualizá-lo é a proteção recomendada.

Antes da primeira release com este job, o branch não existe. Crie-o na última tag, por exemplo `git push origin v0.5.1^{commit}:refs/heads/sandbox`, ou espere a próxima versão final.

### Primeiro acesso

O token de uso único aparece nos logs do serviço `web` no Coolify. Abra `https://<SANDBOX_HOST>/setup/` e preencha o [setup](#primeira-instalação-pelo-setup-146) com:

- **Instância:** host `<SANDBOX_HOST>`, origem CSRF `https://<SANDBOX_HOST>` e confiança no proxy ligada (o Traefik substitui o `X-Forwarded-Proto` recebido de fora). Antes desse passo, o setup já vê a requisição como `https://` porque o compose liga `SKELETON_SETUP_TRUST_PROXY`; sem isso o POST do token responderia 403 de CSRF (#442). A origem CSRF fica no SQLite, não no ambiente, e pode ser alterada em `/platform/instance/`. Um sandbox configurado antes disso, com a origem só no ambiente (#439), continua aceitando os formulários sem ela: com a confiança no proxy ligada, o Django vê `https://<SANDBOX_HOST>` e o `Origin` do próprio host já confere.
- **Banco runtime:** `postgres://skeleton_runtime:<SKELETON_RUNTIME_DB_PASSWORD>@postgres:5432/skeleton`.
- **Schema:** nada a digitar. O agente já aplicou as migrations; o setup só confere com a role runtime e nunca pede a owner.
- **Cache:** `redis://:<REDIS_PASSWORD>@redis:6379/0`.
- **Arquivos:** backend local em `/files`, o volume persistente `files`.

Na conclusão, o processo sai, a política de reinício o traz de volta em modo normal, e o `worker` e o `scheduler` passam a ficar saudáveis.

### Atualizar o sandbox

O backup diário do sandbox e a recuperação depois da perda do host estão em [backup e recuperação](backup-recovery.md#backup-do-sandbox).

O sandbox não recebe deploy a cada merge (o job `deploy-sandbox` saiu do CI, #654). Versões novas do core e apps chegam pela tela `/platform/updates/`, que consulta o repositório oficial: escolha a versão, confira o plano e confirme cada passo ([atualizações](updates.md)). Um *Redeploy* no Coolify só reconstrói as imagens do agente e de runtime a partir do [branch `sandbox`](#o-branch-sandbox); o release ativo continua o mesmo.

**Sandbox criado antes do agente.** O volume `postgres_data` já tem o schema e o `instance` a configuração, mas não há estado do agente, e a primeira partida sobre um banco já migrado não é coberta pelos testes. Como o sandbox só tem dados de teste, apague os volumes do recurso (`postgres_data`, `instance` e os do agente) antes do primeiro *Deploy* com este arquivo e refaça o [primeiro acesso](#primeiro-acesso).

**Limites:** o init do PostgreSQL só roda com o volume vazio. Trocar uma senha depois exige `ALTER ROLE` no banco e a atualização da variável e da configuração da instância. Se uma migration falha, o `migrate` termina com erro e o `web` com o código novo não sobe. Confira os logs do `migrate` no Coolify. Migrations já aplicadas antes da falha continuam no banco, porque o sandbox não tem o backup do agente.

## Distribuição do core e adoção de projetos derivados

O `pyproject.toml` é a fonte única da versão do `django-skeleton`. O wheel contém somente `skeleton`, incluindo migrations históricas, templates/static, defaults reutilizáveis e inventário técnico. `config`, `applications`, `examples`, `deployment_agent`, `manage.py`, bancos e secrets não pertencem à distribuição. Os imports históricos e os labels `sk_accounts`, `sk_tenancy`, `sk_access`, `sk_system` e `sk_audit` permanecem estáveis.

Os entrypoints de `config/settings` importam `skeleton.conf.base` e aplicam os profiles de `skeleton.conf.profiles`; o overlay de negócio continua em `config/settings/project.py`. O núcleo calcula `BASE_DIR` pelo entrypoint do projeto ou por `SKELETON_PROJECT_ROOT`, nunca pelo diretório site-packages. Templates de branding podem ficar em `<projeto>/templates`; templates do core são descobertos pelos apps instalados. `config/urls.py` usa `skeleton.conf.urls.application_urls("config.urls_project")`, conservando o hook de rotas do derivado.

### Construir e publicar

**Pré-requisitos:** versão definida em `pyproject.toml` (é ela o `<versão>` dos nomes de arquivo abaixo), dependências fixadas, manifesto revisado e chave privada de um publicador aprovado. **Publisher** é quem assina o artefato; aprovação da chave é decisão do operador, não algo que o próprio pacote possa conceder. A assinatura demonstra procedência e integridade, não ausência de código malicioso.

```sh
# Ambiente de build com requirements-dev.txt atualizado e hashes:
.venv/bin/python -m build --wheel --no-isolation
.venv/bin/python manage.py build_core_package \
  --wheel dist/django_skeleton-<versão>-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-<versão>.skapp
# Equivalente independente de settings/Django startup:
skeleton-core build --wheel dist/django_skeleton-<versão>-py3-none-any.whl \
  --manifest /caminho/privado/core-manifest.json \
  --private-key /caminho/privado/publisher.pem \
  --output /caminho/privado/core-<versão>.skapp
```

**Manifesto gerado (#494).** Em vez de escrever o manifesto à mão, gere-o do wheel: `skeleton-core manifest` lê a versão, o `Requires-Python` e o pin exato do Django do `METADATA` do wheel (sem executá-lo) e calcula `from_versions` pela política de linhas. A faixa do Django é a da linha do pin (`Django==6.1.3` vira `>=6.1,<6.2`); um wheel sem pin exato é recusado ("Core wheel must pin Django to derive compatibility."). As dependências não entram no manifesto do core: viajam por alvo no [pacote de release](#pacote-de-release). O arquivo de saída nunca é sobrescrito.

```sh
skeleton-core manifest --wheel dist/django_skeleton-<versão>-py3-none-any.whl \
  --publisher <publisher> --output /caminho/privado/core-manifest.json \
  --known-version 0.3.1 --known-version 0.3.2 --earliest-source 0.1.0 \
  [--release-notes-file notas.md] [--build "<procedência>"]
```

**Política de versões de origem.** Um release aceita ser instalado sobre a própria linha e sobre a linha imediatamente anterior: `from_versions` (e `compatibility.skeleton`, que é a mesma faixa) vai do `.0` da linha anterior já publicada até a própria versão. `--known-version` lista as versões já publicadas; `--earliest-source` é o piso usado só quando nenhuma linha anterior é conhecida (o primeiro release automatizado). Um piso acima da versão construída é recusado ("Earliest source X is above the version Y."), e a validação do manifesto de core recusa `from_versions` ou `compatibility.skeleton` que não contenham a própria versão, antes de assinar (#650): uma faixa vazia seria publicada e não serviria a nenhuma instalação. O piso da receita oficial (`deploy/release/recipe.json`) é a `0.1.0`, a primeira release publicada. Quem instala confere de novo: o agente recusa pular uma linha (`version_jump_forbidden`).

| Versão | Versões conhecidas | `--earliest-source` | `from_versions` |
|---|---|---|---|
| 0.3.1 | — | 0.1.0 | `>=0.1.0,<=0.3.1` (o primeiro release automatizado) |
| 0.3.2 | 0.3.1 | 0.1.0 | `>=0.1.0,<=0.3.2` |
| 0.4.0 | 0.3.1, 0.3.2 | 0.1.0 | `>=0.3.0,<=0.4.0` |
| 1.0.0 | 0.4.0, 0.7.2 | — | `>=0.7.0,<=1.0.0` |
| 1.0.1 | 0.7.2, 1.0.0 | — | `>=0.7.0,<=1.0.1` |
| 0.4.0 | — | — | `>=0.4.0,<=0.4.0` (só a própria versão) |

Para o passo a passo de quem opera uma instalação (por arquivo, pelo GitHub, forks, agente, avisos e solução de problemas), veja o [guia de atualização](updates.md). A lista abaixo descreve o formato do manifesto core, que `skeleton-core manifest` gera:

O manifesto core identifica o artefato e limita como instalá-lo:

- Use `manifest_version=2`, `kind="core"` e `component_id="django-skeleton"`, com nome, descrição, versão e publisher. Formato 1 continua aceito para verificação de artefatos existentes, sem reescrever sua assinatura.
- Declare intervalos explícitos de compatibilidade `skeleton/python/django`, dependências, conflitos opcionais e migrations (`leaves`, `from_versions`, labels opcionais).
- O arquivo de entrada usa `files=[]`, `python_dependencies` e lifecycle com `data_retention="retain"`, `maintenance=true`, `backup=true`.
- Declare o wheel principal em `python_dependencies`: `name="django-skeleton"`, versão, `wheel="wheels/<nome>.whl"` e `sha256`. O builder recalcula o hash e gera o inventário completo antes de assinar; a versão do wheel deve equivaler à do manifesto.
- `release_notes` e `build` registram instruções/notas e procedência. Declare wheels adicionais e forneça-os por `--dependency-wheel`; nunca use sdist (distribuição de fontes que exige build na instalação).

A assinatura segue o mesmo contrato e trust store dos pacotes de apps. O pacote não pode trazer uma segunda chave para autorizar a si mesmo.

#### Pacote de release

O pacote do core leva só o wheel do core: as dependências Python e o lock ficam por conta do operador. O **pacote de release** (#492) resolve isso num arquivo único por release do core: um `.skapp` de `kind: "release"` que embrulha o pacote do core **sem alterá-lo** e acrescenta, por **alvo** (interpretador e plataforma, como `cp314-manylinux_2_28_x86_64`), o lock com hashes e os wheels que ele fixa, mais o wheel do agente. É verificado com o mesmo trust store dos pacotes de componente; o código está em `skeleton/system/packaging/release.py`.

```
manifest.json, manifest.sig
packages/django-skeleton-<versão>.skapp           pacote do core, intacto
wheels/<arquivo>.whl                              dependências; um wheel puro aparece uma vez para todos os alvos
locks/<id do alvo>.txt                            lock com --hash=sha256 de cada wheel do alvo
agent/skeleton_deployment_agent-<versão>-py3-none-any.whl   (opcional)
```

O manifesto (JSON canônico, contador próprio `release_format: 1`, que não é o `manifest_version` dos pacotes de componente) declara `kind`, `component_id="django-skeleton"`, `name`, `version`, `publisher`, `core` (`path` e `sha256`), `targets` (cada um com `id`, `environment` de quatro chaves — `implementation_name`, `python_version`, `platform_machine`, `sys_platform` —, `lock` e `wheels`), o inventário `files` (`path`, `size`, `sha256`, `role` coerente com o diretório) e, opcionais, `agent`, `release_notes` e `build`. A validação recusa campo desconhecido, membro a mais, a menos ou alterado, alvo repetido, dois wheels da mesma distribuição num alvo, wheel fora de qualquer alvo ou ausente do lock do alvo, o core ou o agente entre os wheels de um alvo, e um wheel do agente de outra distribuição ou versão. O core embutido é verificado como pacote: **mesmo publicador e mesma versão** do pacote de release, de modo que um publicador aprovado para apps não consegue embrulhar o core de outro. `validate_package` continua recusando um pacote de release (leitores antigos falham de forma limpa).

```sh
# receita.json: {"name": "Skeleton", "release_notes": "", "build": "",
#   "targets": [{"id": "cp314-manylinux_2_28_x86_64",
#                "environment": {"implementation_name": "cpython", "python_version": "3.14",
#                                "platform_machine": "x86_64", "sys_platform": "linux"},
#                "lock": "locks/x86_64.txt", "wheels": "wheels-x86_64"}]}
# Caminhos relativos à receita; "wheels" é um diretório e entram todos os *.whl dele.
.venv/bin/python -m skeleton.system.packages release-build   --core /caminho/privado/core-<versão>.skapp --recipe /caminho/privado/receita.json   --agent-wheel dist/skeleton_deployment_agent-<versão>-py3-none-any.whl   --private-key /caminho/privado/publisher.pem --output /caminho/privado/skeleton-release-<versão>.skapp
.venv/bin/python -m skeleton.system.packages validate /caminho/privado/skeleton-release-<versão>.skapp   --trust-store /caminho/privado/publishers.json
```

`release-build` só produz o arquivo se a mesma chave assinou o core ("Release and core must be signed by the same publisher key."), nunca sobrescreve o destino e gera arquivos byte a byte iguais para as mesmas entradas. `inspect` e `validate` reconhecem os dois tipos de arquivo; sem trust store, `inspect` diz `"trusted": false`. Como nos demais pacotes, a assinatura prova procedência, não ausência de código malicioso.

#### Publicação automática nos releases do GitHub

Cada GitHub Release recebe, sem intervenção, os artefatos assinados (#496). Depois de publicar a versão, o workflow "Versão e release" dispara `.github/workflows/release-artifacts.yml` (`release.dispatch_workflow` em `.github/dev-workflow.toml`, com os inputs `version` e `tag`), que constrói os wheels a partir da tag, baixa os wheels das dependências do lock e anexa:

| Asset | O que é |
|---|---|
| `django-skeleton-<versão>.skapp` | Pacote do core (pacote de componente). Não entra no índice. |
| `skeleton-<versão>.release.skapp` | [Pacote de release](#pacote-de-release): a entrada do core no índice. |
| `django_skeleton-<versão>-py3-none-any.whl` | Wheel do núcleo, que o CI de uma app em repositório próprio instala numa versão fixa (#521); o SHA-256 também vai para o corpo da release. |
| `skeleton_deployment_agent-<versão>-py3-none-any.whl` | Wheel do agente, para a instalação manual. |
| `skeleton-index.json` | Índice assinado e acumulado, com validade; `artifact` é `<tag>/<asset>`, por exemplo `v0.6.0/skeleton-0.6.0.release.skapp`. |
| `SHA256SUMS` | SHA-256 de cada asset (conveniência; a confiança vem das assinaturas). |

O índice novo acumula o do release anterior (`package index --previous`) e, quando a release "latest" do GitHub é outra (um hotfix numa linha antiga), o índice também é enviado para ela, porque o agente lê o índice de `releases/latest`. No dia 1 de cada mês o mesmo workflow renova a validade do índice da release "latest" (mesmas entradas, geração e validade novas). A receita `deploy/release/recipe.json` declara o nome, o piso do primeiro release automatizado (`earliest_source`), a validade do índice em dias (`index_validity_days`) e os alvos: para cada um, o ambiente (quatro chaves), o lock (`requirements.txt` para o alvo oficial `cp314-manylinux_2_28_x86_64`) e as opções do `pip download` (o pip não expande `manylinux_2_28` para as marcas mais antigas, então a receita lista cada plataforma). Um alvo novo é uma entrada nova com o próprio lock; uma dependência sem wheel para o alvo faz a publicação falhar, e nunca se publica sdist.

`tools/release_artifacts.py` é o que o workflow roda, e roda igual localmente:

```sh
# Ensaio, com chave efêmera e trust.json gerado na saída (o job build do CI faz isso a cada PR):
.venv/bin/python tools/release_artifacts.py build --ephemeral-key --version <versão> --tag v<versão> \
  --core-wheel dist/django_skeleton-<versão>-py3-none-any.whl \
  --agent-wheel dist-agent/skeleton_deployment_agent-<versão>-py3-none-any.whl \
  --verify-install --out /tmp/release-rehearsal
# Publicação, com a chave do publicador:
.venv/bin/python tools/release_artifacts.py build --version <versão> --tag v<versão> \
  --publisher <publisher> --private-key /caminho/privado/release.key --out /tmp/release \
  [--previous-index skeleton-index.json] --verify-install
.venv/bin/python tools/release_artifacts.py refresh-index --publisher <publisher> \
  --private-key /caminho/privado/release.key --previous-index skeleton-index.json --out /tmp/index
```

`build` confere que a tag nomeia a versão e que os wheels têm essa versão (senão falha, sem publicar nada), gera o manifesto do core pela política de linhas com as versões do índice anterior, monta o pacote do core, o pacote de release e o índice, e verifica tudo com a chave pública derivada; um pacote de release acima de 128 MiB (o limite padrão de upload do agente e da tela) é recusado. Com `--verify-install`, num interpretador que casa com um alvo da receita, instala sem rede num virtualenv temporário o lock do alvo, o wheel do core e o do agente, e roda `pip check` e `skeleton-deployment-agent --help`: um pin do agente (`deployment_agent/pyproject.toml`) que diverge do lock falha a publicação, e não a instalação de alguém. Nenhuma saída imprime a chave.

**Configuração do repositório (passo do dono, uma vez).** Crie o ambiente `release` em Settings → Environments, de preferência restrito a tags `v*`, com:

- o segredo `RELEASE_SIGNING_KEY`: a chave privada Ed25519 gerada por `python -m skeleton.system.packages keygen --private-key release.key --public-key release.pub.pem` (o conteúdo PEM inteiro);
- a variável `RELEASE_PUBLISHER`: o id do publicador (por exemplo `skeleton`), o mesmo que os operadores usam em `approved_publishers`.

Publique a chave pública (`release.pub.pem`) no guia de atualização, para os operadores a aprovarem. Sem o segredo, o job de publicação falha dizendo o que configurar e a renovação mensal termina com sucesso, sem publicar. A chave existe só como segredo do ambiente: o workflow a grava num arquivo temporário com `umask 077` e o apaga no fim, mesmo quando um passo falha; o workflow roda só por `workflow_dispatch` e `schedule`, nunca em `pull_request`, e tem `permissions: {}` no topo, `contents: write` só nos jobs que publicam.

**Num fork.** Gere a sua própria chave, configure o ambiente `release` com o seu `RELEASE_PUBLISHER` (um id diferente do publicador oficial) e divulgue a sua chave pública. As instalações que seguem o fork aprovam essa chave em `approved_publishers`; a chave do repositório original não assina os releases do fork. **Rotação:** se a chave for perdida ou vazar, gere outra, publique com outro id de publicador e peça aos operadores que troquem a entrada em `approved_publishers`. O agendamento mensal é desligado pelo GitHub em repositórios sem atividade por 60 dias: a validade de 120 dias dá margem, mas confira que o workflow continua rodando.

O projeto derivado instala o wheel fixado em ambiente isolado e mantém um lock final com hashes para core, apps e dependências. O Dockerfile do template constrói esse wheel em stage separado, instala dependências dos locks com `--require-hashes`, instala o wheel já produzido sem resolver dependências e copia apenas os arquivos de projeto para o runtime. Não sobra uma cópia `skeleton/` em `/app` ocultando a distribuição instalada. A imagem do agente para releases gerenciados usa o lock final offline e wheels aprovados do release.

### Cutover de uma cópia do core

Faça backup e ensaie em clone offline antes de qualquer cutover. Identifique um release base **assinado e conhecido**; não use o HEAD corrente como evidência de equivalência de um projeto histórico. O **trust store** (lista de chaves confiáveis) é um JSON privado `publisher-key-id -> caminho PEM público` aprovado pelo operador. Ele contém referências a chaves públicas, não a chave privada de assinatura; uma chave fornecida pelo pacote não estabelece confiança. Em todos os comandos (`package …`, `skeleton-core adopt|adoption-plan`, `adopt_core_distribution`) um caminho PEM relativo é resolvido a partir da pasta do próprio trust store, nunca do artefato nem do diretório corrente (#255). Os limites de artefato são: 4096 membros no artefato externo, 32768 membros em cada wheel e 65536 na soma das wheels de um artefato ou wheelhouse; um membro corrompido falha com `PackageError`. A verificação lê o arquivo com `O_NOFOLLOW` e por isso exige Linux ou macOS; em outro sistema ela recusa com uma mensagem clara, em vez de abrir sem a proteção.

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

**Custo do `/inventory`** (#281). A conferência de drift que a página de atualizações mostra é informativa: o agente guarda o resultado em memória por `drift_check_interval` segundos (padrão 300; `0` refaz o hash a cada chamada) e devolve `drift_checked_at`, o instante da conferência usada. O cache cai quando o release ativo muda (adoção, fim de operação, recuperação), e um ponteiro ativo divergente nunca é guardado. Prévia, execução, `finish`, recuperação e a partida do daemon continuam conferindo o release inteiro, sem cache. A lista traz as 50 operações escritas mais recentemente, em ordem de criação, e só esses arquivos do journal são lidos; uma operação antiga recuperada hoje aparece nela. A recusa por operação pendente lê um índice derivado (`operations-index.json`), que se reconstrói sozinho quando um arquivo do journal muda.

Estes três registros respondem a perguntas distintas:

| Registro | Pergunta respondida | Limite |
|---|---|---|
| Geração | “Qual estado global este plano espera substituir?” É o número de estado do deployment, não a versão SemVer da app. | Uma geração antiga invalida o plano, mesmo que a versão pareça compatível. |
| Inventário | “Quais componentes, arquivos e históricos pertencem a este release?” É o JSON privado que orienta o startup. | Editá-lo não instala código nem prova que os workers carregaram os bytes declarados. |
| Journal | “O que a operação tentou, confirmou e precisa recuperar?” É o registro durável externo ao banco da aplicação. | Sobrevive ao restore do banco; não recria escritas de negócio perdidas. |

**Staging** é a preparação isolada de um novo release antes de ativá-lo. O release ativo permanece congelado; pacotes aprovados reconstituem o novo ambiente, em vez de mesclar código antigo e novo. Os detalhes a seguir explicam como o inventário determina o startup e como os probes confirmam o código realmente carregado.

`SKELETON_DEPLOYMENT_INVENTORY` aponta para o JSON privado gerado pelo agente antes do startup. Ele declara geração, core, apps installed/project-local/schema_retained, snapshots de schema e arquivos históricos de migrations. Settings não consultam banco para decidir `INSTALLED_APPS`: só apps `installed` são agregadas ao overlay; apps locais continuam em `PROJECT_APPS` e precisam ter manifesto/caminho coerentes com o inventário. Apps `schema_retained` não têm modelos, registry, menus ou URLs ativos. Os snapshots retidos ficam em `SKELETON_RETAINED_SCHEMA_PATHS`, preferencialmente com `path` e SHA-256.

Somente o profile `migrate` adiciona os arquivos históricos: packages isolados `archive_<label>` com `__init__.py` sem código, migrations originais byte-exact e nenhum `models.py`/`apps.py`. Um AppConfig técnico preserva o label original sem importar models/ready de negócio; `MIGRATION_MODULES` aponta para as migrations reais. O agente aprova o grafo oficial e os imports históricos antes de permitir retirada. Não existe placeholder para simular migration aplicada.

`SKELETON_MAINTENANCE_MARKER` é um arquivo externo controlado pelo operador/agente. Sua existência faz as requests de aplicação e admin responderem 503 antes de sessão/autenticação/contexto/DB, com a [resposta de manutenção](maintenance.md#a-resposta): página HTML no navegador e `{"code": "maintenance", ...}` na API, com `Retry-After: 60` (#446). A aplicação não tem outro modo de manutenção: a manutenção planejada é feita na infraestrutura ([manutenção](maintenance.md), #674). O marcador não libera uma view de status dependente do banco durante migrations. Live/ready mantêm seu contrato atual; `/health/release/` é GET/HEAD, valida Host, não consulta DB e informa somente geração, versão/origem e digest do core. Para um release gerenciado, cada worker confere uma vez no startup os hashes de `core.files` contra o wheel efetivamente carregado: versão/origem ou bytes divergentes retornam 503 `release_mismatch`, não sucesso por eco do inventário. Esses probes são internos e devem ficar restritos na rede/proxy.

A base source explicitamente adotada pode ser confirmada para recuperação somente com origem, versão e hashes iguais à base assinada; todo release preparado pelo agente usa origem `installed`. Staging não copia apps gerenciadas antigas para fazer merge: reconstitui seus arquivos a partir dos pacotes verificados. Só diretórios da nova cópia de projeto recebem permissão de escrita para preparação; o release ativo congelado permanece intacto.

O canal de controle independente usa `SKELETON_AGENT_SOCKET` e `SKELETON_AGENT_TOKEN_FILE`; o token/socket de API do agente não são o socket Docker nem credenciais de migration. `SKELETON_PACKAGE_MAX_BYTES` limita o upload web (default 128 MiB). O agente fornece status autenticado e limitado por operação durante manutenção; o web não recebe shell, escrita no release ativo ou credencial owner.

Nos arquivos Compose do repositório, a credencial owner fica só no serviço `agent` (`SKELETON_MIGRATION_DATABASE_URL`), que migra com ela; o inicializador ainda tira `MIGRATION_DATABASE_URL` do ambiente de cada serviço. Inventário, marcador e socket ficam nos volumes `releases` e `control`, que os serviços de aplicação montam só para leitura. Secrets e estado mutável permanecem fora dos builds/releases.

## Agente independente de deployment

A versão do agente é a de `deployment_agent/pyproject.toml`, igual à do núcleo. O CI constrói o wheel do agente, instala-o num virtualenv limpo com o lock e executa `skeleton-deployment-agent --help` (#309); os pins de execução dele coincidem com o lock auditado. O executável é `skeleton-deployment-agent --config /caminho/agent.json <comando>`; no checkout, o equivalente é `.venv/bin/python -m deployment_agent --config /caminho/agent.json <comando>`. Ele **não** roda dentro do Gunicorn. Instale-o em virtualenv próprio, fora de `project_root`/`releases_root`, fixando a distribuição do agente e suas dependências. A partir dos artefatos previamente produzidos e aprovados:

```sh
python3.14 -m venv /opt/skeleton-agent/venv
# agent.lock é o lock com hashes da distribuição independente e dependências;
# o wheelhouse deve conter também o wheel do próprio agente.
/opt/skeleton-agent/venv/bin/python -m pip install --no-index \
  --only-binary=:all: --find-links /srv/agent-wheelhouse \
  --require-hashes -r /srv/agent.lock
```

Nos containers, a imagem do alvo `agent` do `Dockerfile` já traz o wheel do agente, o do core, o cliente PostgreSQL e a chave do publicador oficial; o roteiro abaixo é o da instalação com systemd. O pacote independente é definido por `deployment_agent/pyproject.toml`; seu entrypoint não exige inicializar Django. A atualização do core gerenciado não atualiza esse virtualenv. O operador mantém `python`, `pg_dump`, `pg_restore`, `psql` e, para Compose, Docker disponíveis com versões compatíveis.

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

Todos os paths são absolutos; publisher IDs precisam corresponder aos manifests/índices assinados. `project_paths` é uma allowlist de overlays preservados: inclua `templates`, `examples` ou assets locais usados pelo derivado. Não inclua `.`, core, virtualenv, dados, secrets ou metadata gerada. `requirements_lock` tem pins `==` e SHA-256; disponibilize todos os wheels compatíveis, incluindo dependências transitivas e extras, em `wheelhouse`. O core selecionado vem de `.skapp` assinado; não use `pip` online no release. Um wheel faltante bloqueia o preview. Com um [pacote de release](#pacote-de-release) que traz o alvo do servidor, o lock e os wheels do core vêm do publicador e o `wheelhouse` só precisa das dependências do projeto (ambiente do publicador, abaixo).

Chaves opcionais das atualizações (#491), com o padrão e o efeito:

| Chave | Padrão | Efeito |
|---|---|---|
| `allow_version_jumps` | `false` | Emergência: deixa o core pular linhas minor (`version_jump_forbidden`). |
| `project_requirements_lock` | ausente | Lock das dependências só do projeto, somado ao do publicador. |
| `agent_root` | ausente | Liga a auto-atualização do agente (`current`, `versions/`, `state.json`). |
| `agent_publishers` | `[]` | Publicadores aprovados que podem fornecer o agente além do do core instalado. |
| `github` | `api_url` `https://api.github.com`, `download_hosts` `[".githubusercontent.com"]` | Host da API, hosts de download e `token_file` das fontes GitHub. |
| `catalogue_sources[].github` | — | Fonte GitHub `dono/nome`, com `publisher` obrigatório e `private` opcional. |
| `update_check_interval` | `21600` | Segundos entre as consultas automáticas às fontes; `0` desliga. |

Exemplo das chaves opcionais (acrescente ao JSON acima só o que usar; os caminhos são fictícios):

```json
{
  "agent_root": "/opt/skeleton-agent",
  "agent_publishers": [],
  "project_requirements_lock": "/srv/skeleton/project-requirements.txt",
  "github": {"token_file": "/srv/skeleton/private/github-token"},
  "catalogue_sources": [{"id": "skeleton", "github": "alaorneto/skeleton", "publisher": "platform-release-key"}],
  "update_check_interval": 21600
}
```

**Chaves opcionais** (#246). A forma é conferida na partida do agente, com um código por chave: `recovery_paths` (lista de caminhos absolutos, `invalid_recovery_path`), `smoke_token_file` (caminho absoluto, `invalid_smoke_token_path`), `python`, `pg_dump`, `pg_restore`, `psql` e `docker` (caminhos absolutos, `invalid_tool_path`), `socket_gid` (inteiro não negativo, `invalid_socket_gid`), `status_http` (`port` de 1 a 65535, `public_url` `http`/`https`, `host` `127.0.0.1` ou `::1`; `insecure_status_listener`), `catalogue_sources` (lista de objetos com `id`, `invalid_catalogue_sources`) e `command_timeout`, `health_timeout`, `catalogue_timeout`, `max_upload_bytes`, `minimum_free_bytes` (número positivo, `invalid_numeric_setting`). Uma instalação com chave opcional malformada, que antes subia, passa a ser recusada na partida com o código específico.

**Ambiente do publicador (#495).** Quando o core chega num [pacote de release](#pacote-de-release) que traz o alvo deste servidor, o lock e os wheels das dependências do core vêm do publicador, não do `requirements_lock` nem do `wheelhouse` do operador: o agente registra em `environments/<digest do core>.json` (no armazenamento privado, 0600) de qual pacote de release enviado tirá-los, e a cada montagem revalida a assinatura desse pacote em `uploads/` e lê os membros do snapshot verificado; nada fica extraído entre operações. A chave opcional `project_requirements_lock` (caminho absoluto, `invalid_config_path`) é o lock com hashes das dependências **só do projeto derivado**: ele é somado ao do publicador; uma distribuição que o publicador já fixa em outra versão falha com 409 `dependency_version_conflict`. Os wheels dessas dependências continuam no `wheelhouse`. Sem pacote de release, ou com um pacote que não traz o alvo deste servidor, valem o `requirements_lock` e o `wheelhouse` como antes.

**Auto-atualização do agente (#497).** A chave opcional `agent_root` (caminho absoluto de um diretório existente, `invalid_agent_root`) liga a auto-atualização; sem ela, a atualização do agente continua manual. `agent_publishers` (lista de ids, todos em `approved_publishers`; senão `invalid_agent_publishers`) lista os publicadores autorizados a fornecer o agente **além** do publicador do core instalado. O diretório pertence ao usuário do agente (`skeleton-agent`, 0755 ou mais fechado) e nunca é gravável por `skeleton-web` nem por outros usuários: é o código do processo que guarda a credencial owner.

```
<agent_root>/
  current -> versions/0.6.0-1a2b3c4d5e6f      link simbólico relativo
  state.json                                  {"current", "previous", "version", "release", "switched_at"}
  versions/<versão>-<12 primeiros caracteres do digest do pacote de release>/
    venv/  wheels/  requirements.lock  READY
```

A unit de referência (`deploy/systemd/skeleton-agent.service`) executa `/opt/skeleton-agent/current/venv/bin/skeleton-deployment-agent` com `Restart=on-failure`: depois de trocar o link, o agente sai com o código 75 e o systemd o sobe de novo, já na versão nova.

**Nota de migração.** Quem mantém dependências do projeto no `requirements_lock` deve movê-las para `project_requirements_lock` **antes** do primeiro pacote de release: com o ambiente do publicador, o `requirements_lock` deixa de ser lido para o core, e essas dependências apareceriam em "Dependências que deixam de ser instaladas" na prévia.

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

**Feed da agenda:** o link de assinatura (`/me/apps/skeleton_calendar/feed/<token>.ics`) leva o token no caminho, porque clientes de calendário não enviam cabeçalhos. O link de um calendário só (`/me/apps/skeleton_calendar/calendar-feed/<token>.ics`, #669) também. Configure o proxy para **não registrar** esses caminhos (ou redigir o segmento depois de `feed/` e de `calendar-feed/`), como faria com uma senha: quem lê o log lê a agenda, ou o calendário do link, até o token expirar ou ser revogado em `/profile/tokens/`. O Django não registra o caminho no log de requisições do núcleo além do status.

As apps first-party (`skeleton/contrib`, como a agenda) vêm no wheel do núcleo e não passam pelo agente nem por `.skapp`. O operador as instala por nome ([extensão](extending.md#apps-first-party-do-núcleo-skeletoncontrib)):

| Passo | O que fazer | Resultado |
|---|---|---|
| Instalar | `SKELETON_BUNDLED_APPS=calendar` (ou `calendar,ledger`, com o livro contábil, #542) no ambiente ou em `/platform/instance/`, reinício e `manage.py migrate` | Depois do `migrate`, `ComponentInstallation` registra a app com `kind=bundled` e `state=bundled`. A disponibilidade nasce `selected` sem concessão: a app fica indisponível e desativada em todas as organizações até a plataforma liberar e cada organização ativar |
| Retirar com dados | Tire o nome de `SKELETON_BUNDLED_APPS`, ponha em `SKELETON_BUNDLED_RETAINED`, reinicie e rode `manage.py sync_bundled_apps` | O estado vira `schema_retained`; as tabelas ficam, validadas pelo snapshot congelado do wheel, e a ativação de cada organização é desligada. Nada é apagado |
| Reinstalar | Volte o nome para `SKELETON_BUNDLED_APPS`, tire de `SKELETON_BUNDLED_RETAINED`, reinicie e rode `migrate` | O estado volta a `bundled` com os mesmos UUIDs, registros e configurações; cada organização reativa a app |

`check_skeleton_schema` aponta `bundled_divergence` quando a app sai de `SKELETON_BUNDLED_APPS` sem entrar em `SKELETON_BUNDLED_RETAINED` (as tabelas ficariam sem classificação), quando volta sem `sync_bundled_apps` ou quando está instalada sem registro (falta `migrate`). Um nome fora do mapa fixo, ou o mesmo nome nas duas listas, para o startup.

**Livro contábil** (#517). Instale com `ledger` em `SKELETON_BUNDLED_APPS`, reinício e `migrate`; libere a app na plataforma e ative por organização. O job diário `ledger.recurrences` gera os títulos recorrentes e precisa do worker e do agendador. Retirar com `SKELETON_BUNDLED_RETAINED=ledger` e `sync_bundled_apps` preserva livros, lançamentos selados, títulos e baixas (os selos e o guard continuam valendo nas tabelas retidas); reinstalar devolve os mesmos registros, e cada organização reativa a app. Nada do livro é apagado pela retirada.

**Apps em desenvolvimento** (#518). `SKELETON_DEV_APPS` carrega apps de um checkout local e só existe nos perfis de desenvolvimento e teste: os perfis de produção, migração, build e setup param o startup quando a variável não está vazia. Em produção uma app só entra por pacote assinado instalado pelo agente, ou em `PROJECT_APPS`; não defina a variável no ambiente de uma instalação.

**Apps gerenciadas que dependem de uma first-party** (#254). A dependência (`"org.skeleton.calendar": ">=0.5.0,<0.6.0"`) só é satisfeita quando a first-party está em `SKELETON_BUNDLED_APPS`, na versão do core. O agente lê `SKELETON_BUNDLED_APPS` do arquivo de ambiente runtime (ou do SQLite da instância) e recusa o plano com `dependency_unsatisfied` quando ela falta; um nome fora do mapa é `unknown_bundled_app`. A ordem segue a dependência:

- **Instalar:** primeiro a first-party (nome em `SKELETON_BUNDLED_APPS`, reinício, `migrate`), depois o plano com a app que depende dela.
- **Retirar:** primeiro a app dependente (plano de remoção), depois a first-party. Tirar a first-party com um dependente instalado faz o startup parar com `Invalid private deployment inventory` (falha fechada).
- **Core novo com first-party nova:** o agente usa o mapa de first-party do `skeleton` instalado no ambiente dele. Num `core_update` que traz uma first-party nova, uma app do mesmo plano que dependa dela é recusada (`missing_dependency`); atualize o core e o agente primeiro e instale a app num segundo plano.
- **Antes de empacotar:** `package doctor <app> --inventory inventory.json --bundled-app calendar` (repetível) resolve contra as first-party do alvo; um nome desconhecido é `bundled_app_unknown`.

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

Organizações que admitem por domínio de email verificado (#28), e as que servem o endereço próprio num domínio comprovado (#488), dependem do registro TXT continuar publicado; o mesmo comando reverifica os dois. Agende `manage.py verify_domains --recheck` uma vez por dia com cron ou um systemd timer, com o mesmo ambiente do `web`. O comando consulta o DNS fora das transações (3 s por domínio) e imprime só contagens; três falhas seguidas suspendem a admissão por aquele domínio ([autenticação](authentication-sso.md#domínio-de-email-verificado)).

### Worker de jobs e agendador

O worker (`manage.py run_worker`) e o agendador de periódicos (`manage.py run_scheduler`) são serviços operados como o web (#40). Notificações por email e push, antivírus de anexos, cota, webhooks, consumidores de eventos e as retenções dependem deles. As units de exemplo estão em `deploy/systemd/skeleton-worker.service` e `deploy/systemd/skeleton-scheduler.service`; no Compose, são os serviços `worker` e `scheduler` de `compose.yaml`, com a mesma imagem, ambiente e volume da instância do `web`.

| Momento | O que o agente faz |
|---|---|
| `drain` (antes do backup e das migrations) | Executa o hook `drain` e depois `worker_stop`. O SIGTERM termina o job atual e sai sem reivindicar outro; dê ao worker um `TimeoutStopSec` maior que o `timeout_s` dos seus jobs |
| `start` (depois do cutover) | Executa o hook `restart` e depois `worker_restart`: o worker sobe com o código do release novo |
| Compose | Liste os serviços em `"compose": {"worker_services": ["worker", "scheduler"]}`. O agente gera para eles o mesmo override de imagem e ambiente runtime do web (recusa `MIGRATION_DATABASE_URL`), para-os com `compose stop` no `drain` e os recria com `compose up --force-recreate` no `start` |

### Primeira partida pelo agente (containers)

Num deployment em container, ninguém instala o core à mão (#652): na primeira partida, o agente instala uma **release publicada**. A imagem do agente é o alvo `agent` do `Dockerfile` (mesmo Python e bibliotecas do runtime, cliente PostgreSQL 18, o agente em `/opt/agent` e os arquivos do projeto em `/opt/project`), e o comando dela é `python -m deployment_agent.bootstrap`:

1. **Configuração**, escrita uma vez a partir das variáveis do container (um `agent.json` existente nunca é sobrescrito): `agent.json`, `runtime.env`, `migration.env` e `build.env` em `SKELETON_AGENT_PRIVATE` (0600) e o token de controle em `<SKELETON_CONTROL>/agent.token` (0640, gerado uma vez). Obrigatórias: `SKELETON_RUNTIME_DATABASE_URL` e `SKELETON_MIGRATION_DATABASE_URL` (só neste container). O publicador é o oficial, cuja chave pública está versionada em `deploy/release/publishers/skeleton.pub.pem` e vai na imagem do agente: `SKELETON_AGENT_PUBLISHER` (padrão `skeleton`), `SKELETON_AGENT_PUBLISHER_KEY` (padrão `/opt/agent/publishers/skeleton.pub.pem`, caminho absoluto de um PEM) e `SKELETON_AGENT_SOURCE` (padrão `alaorneto/skeleton`, fixado nesse publicador; `none` deixa sem fonte GitHub); um fork troca as três ([usar um fork](updates.md#usar-um-fork)). Opcionais: `SKELETON_CORE_VERSION` (a versão a instalar; padrão a mais nova publicada), `SKELETON_AGENT_RELEASE_FILE` (um pacote de release local, sem rede), `SKELETON_HEALTH_URL`, `SKELETON_PUBLIC_HOST`, os caminhos da instância e as chaves de funcionalidade: `SKELETON_BUNDLED_APPS` e `SKELETON_ENABLE_EXAMPLE_APP`, que mudam as apps instaladas, vão para todo perfil; `SKELETON_BILLING_ENABLED` vai só para o runtime, onde está o keyring da cobrança (no build e nas migrations o check `skeleton.E037` recusaria). Falta ou valor inseguro: `missing_bootstrap_variable`, `invalid_bootstrap_path`, `invalid_bootstrap_value`. A configuração gerada usa o backend diretório com os hooks `control` de [serviços em containers](#serviços-em-containers-sem-socket-docker).
2. **Instalação inicial** (`install_initial`, também `skeleton-deployment-agent --config … bootstrap [--release-file F] [--version V] [--source S]`), só quando ainda não há release ativo: o pacote de release vem do arquivo ou da fonte (a versão fixada ou a mais nova publicada), é verificado contra `approved_publishers` antes de qualquer extração e precisa trazer o alvo deste servidor (`release_package_required`, `release_target_unavailable`, `package_verification_failed`). O agente monta a geração 0 a partir de `project_root` com o core, o lock e os wheels do pacote; aplica as migrations e o catálogo com o owner; confere o schema; espelha o inventário no banco (`sync_deployment_state`); congela o release; e só então troca o ponteiro ativo. Antes do setup a instância não tem configuração runtime, então essa conferência usa o perfil migration, como a migração do próprio setup.
3. **`serve`**, em toda partida. Com release ativo, a partida não reinstala nada.

Uma partida interrompida não deixa ponteiro ativo: a próxima remove o release parcial (`0-bootstrap-*`) e recomeça (as migrations são idempotentes). Os containers de aplicação esperam (`waiting`) até haver o release e sobem sozinhos.

Com `SKELETON_SETUP_SCHEMA=agent` no web, a etapa "Tabelas do banco" do setup não pede a credencial owner: o agente já migrou, e a etapa só confere o schema com a role de aplicação (`check_deployed_schema`).

### Serviços em containers sem socket Docker

Em containers (Compose, Coolify), o agente controla `web`, `worker` e `scheduler` sem acesso ao Docker (#651): o socket Docker equivale a root no host, e o Coolify gera o próprio projeto Compose, que desfaria o que o agente recriasse. Cada container de aplicação parte pelo **inicializador** `python -m deployment_agent.launcher` (biblioteca padrão apenas), e os dois lados conversam por arquivos em volumes compartilhados:

| Arquivo | Quem grava | Conteúdo |
|---|---|---|
| `<control_root>/<serviço>/request.json` | só o agente (o container monta só leitura) | `{"id", "action", "release", "operation"}`, com `action` `stop` ou `restart`, gravado de forma atômica |
| `<state_root>/<serviço>/state.json` | só o inicializador daquele serviço (o agente monta só leitura) | `{"id", "state", "release", "pid"}`: o último pedido aplicado e o estado `running`, `stopped`, `waiting` (sem release ativo), `failed` (com `reason`) ou `exited` |

O inicializador resolve o ponteiro do release ativo e recusa um que saia da raiz de releases (`active_release_outside_root`) ou que não tenha `.venv` (`active_release_incomplete`); executa o comando com o virtualenv e o ambiente que o agente dá a um release (`SKELETON_PROJECT_ROOT`, `SKELETON_DEPLOYMENT_INVENTORY`, `STATIC_ROOT`, `PYTHONPATH`), trocando `{release}`, `{venv}` e `{python}` no comando, e tira `MIGRATION_DATABASE_URL` do ambiente do processo filho. Com `--wait-for-agent` (cluster, #681), ele não inicia o release do ponteiro por conta própria na partida do container: publica `waiting` (`reason: agent`) até o agente local pedir `restart`, o que o agente faz depois de conferir que o nó roda o release do cluster. Sem release ativo, espera (`waiting`) e sobe assim que houver um. Cada pedido é aplicado uma vez só (pelo `id`): `stop` manda SIGTERM, espera `--grace` segundos (depois SIGKILL) e fica parado, também depois de o container reiniciar; `restart` para o filho e sobe o release que o ponteiro indica agora. Um pedido malformado vai para o log e é ignorado. Um filho que termina sozinho encerra o inicializador com o código dele, e vale a política de reinício do container.

```sh
python -m deployment_agent.launcher --service worker --control /control --state /control-state \
  --releases-root /releases --active /releases/current --grace 300 \
  -- {python} manage.py run_worker --queues default
```

No agente, a chave opcional `service_control` (`control_root` e `state_root` absolutos e diferentes, `timeout` positivo em segundos, padrão `command_timeout`; senão `invalid_service_control`) liga o subcomando `control`, usado como hook fixo do backend diretório. Ele grava um pedido por serviço e só retorna quando todos confirmaram, ou falha com `service_control_timeout` ou `service_control_failed` (sem `service_control`, `service_control_not_configured`):

```json
{
  "service_control": {"control_root": "/control", "state_root": "/control-state", "timeout": 1800},
  "commands": {
    "drain": ["/opt/agent/bin/skeleton-deployment-agent", "--config", "/agent/agent.json", "control", "stop", "web"],
    "stop": ["/opt/agent/bin/skeleton-deployment-agent", "--config", "/agent/agent.json", "control", "stop", "web"],
    "restart": ["/opt/agent/bin/skeleton-deployment-agent", "--config", "/agent/agent.json", "control", "restart", "web", "--release", "{release}"],
    "worker_stop": ["/opt/agent/bin/skeleton-deployment-agent", "--config", "/agent/agent.json", "control", "stop", "worker", "scheduler"],
    "worker_restart": ["/opt/agent/bin/skeleton-deployment-agent", "--config", "/agent/agent.json", "control", "restart", "worker", "scheduler"]
  }
}
```

Ninguém recebe privilégio Docker: o agente só pede parar e reiniciar os serviços de aplicação, e o container de aplicação só lê o volume de releases e o próprio diretório de controle. Instalações com systemd continuam com os hooks de `systemctl`.

`worker_stop` e `worker_restart` são opcionais, mas vêm juntos: só um deles é recusado (`incomplete_worker_commands`). Um nome inválido ou repetido em `worker_services`, ou igual ao do web ou do migrator, é recusado (`invalid_compose_worker_services`). Sem eles, o worker só pausa pelo marcador de manutenção (heartbeat `paused`), o que não interrompe um job em execução.

**Recuperação manual** (agente indisponível, com o operador conduzindo): pare o worker e o agendador **antes** de `migrate` e suba-os depois do restart do web:

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

As units de exemplo, provisionadas pelo operador (não pelos pacotes), ficam em `deploy/systemd/`: `skeleton-web.service`, `skeleton-agent.service`, `skeleton-worker.service` e `skeleton-scheduler.service` (#318). Web, worker e agendador rodam com `NoNewPrivileges=yes`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, `ProtectKernelTunables`, `ProtectControlGroups` e `RestrictAddressFamilies`; com `ProtectSystem=strict` o sistema de arquivos inteiro é somente leitura, menos `ReadWritePaths`, que precisa listar exatamente o que o serviço grava (arquivos privados, SQLite e chave mestra da instância, `PROMETHEUS_MULTIPROC_DIR`; o `-` à frente ignora um caminho inexistente). A unit do agente tem só `ProtectHome`, `PrivateTmp` e `ProtectControlGroups`: os hooks chamam `sudo`, que não funciona com `NoNewPrivileges=yes` (e `PrivateDevices=`/`ProtectKernelTunables=` o implicam numa unit com `User=`), e o agente grava em `releases_root`, `storage_root` e no diretório do ponteiro ativo.

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

Ao receber `SIGTERM` (ou `SIGINT`), o agente para de aceitar requisições, espera a operação em curso chegar ao estado final e sai (#280). Os tratadores são instalados antes de o socket de controle existir: um sinal recebido enquanto ele é criado também leva a essa parada ordenada, sem deixar o socket no disco (#616). `TimeoutStopSec` precisa ser pelo menos o `command_timeout` configurado (padrão 1800 s). Se o supervisor matar o processo antes, a operação aparece como `interrupted` na próxima partida, com a manutenção ativa, e segue o procedimento de recuperação. O socket nasce no máximo `0660` (a `umask` é restringida só durante a criação), e um cabeçalho `Authorization` com qualquer byte responde 401, nunca 500.

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

O vínculo aprovado reúne `plan_id`, `plan_digest`, `generation`, `operation_id`, `actor_id` e `request_id`. Reenviar exatamente esse vínculo retorna a mesma operação; trocar valores conflita. Se a confirmação de recebimento se perder, consulte o status da operação. Um reenvio explícito deve conservar os seis valores originais, sem novo preview, nova aprovação ou substituição pela geração atual. Na interface, o reenvio mantém o `request_id` lógico da aprovação; a nova request HTTP tem seu próprio identificador na auditoria. Sem vínculo original durável, consulte status/journal e falhe fechado: não invente outra aprovação. O reenvio aparece para uma entrega que falhou (`submission_failed`) e também para uma aprovação que ficou `approved`/`awaiting_agent` por mais de 2 minutos sem retorno do agente; uma aprovação recente não pode ser reenviada. A falha de entrega fica no log como `deployment.submission_failed`, só com o UUID da operação e a classe do erro. O web espera o agente no máximo 10 s nas leituras de estado (`inventory`, status da operação, ticket de acompanhamento) e 30 s na entrega do plano aprovado; upload, preview e catálogo usam o prazo do cliente, e a consulta do plano em preparação usa os mesmos 10 s das leituras. A página da operação consulta o agente sem travar a linha e só a trava para gravar o estado reconciliado (#250).

Um plano/geração antigo é rejeitado para uma nova submissão. O preview prepara ambiente isolado e carrega o grafo Django real, mas não aplica migrations. Hashes e estado das migrations são novamente conferidos na execução e após drenar os escritores.

**A prévia é assíncrona (#298).** `POST /preview` faz só as conferências rápidas (formato, alvos, journal, ponteiro ativo, deriva dos arquivos, preflight, regras por ação e dependências) e responde em poucos segundos com `{"id": "<uuid>", "state": "preparing"}`. A cópia do projeto, a extração dos pacotes, o virtualenv, o `pip install`, os checks, o `collectstatic` e, no Compose, a imagem rodam numa thread do agente, que toma o lock de novo e repete as conferências antes de começar. `GET /plans/<uuid>` responde `preparing`, `ready` (com o plano) ou `failed` (com `error` e, nos problemas de dependência, `details`); nunca devolve inventário, hashes dos arquivos nem o release anterior. Os erros de dependência continuam síncronos: `POST /preview` responde 409 `dependency_unsatisfied` com `details`, e a tela de sugestões é a mesma. Enquanto um plano está `preparing`, outra prévia, `submit` e `prune` recebem 409 `recovery_or_operation_pending`; `submit` de um plano `preparing` ou `failed` recebe 409 `plan_not_ready`. Uma preparação que falha remove o release parcial. Se o agente reiniciar no meio dela, o plano vira `failed` com `agent_interrupted` e o release parcial é apagado, sem marcador de manutenção: a preparação não toca o release ativo. Planos gravados antes desta versão, sem `state`, valem como prontos.

No admin, "Visualizar impacto" leva a uma página de acompanhamento que se atualiza sozinha (`<meta http-equiv="refresh">`, sem JavaScript) e só lê o estado; quando o plano fica pronto, "Continuar" (POST, com autenticação recente) cria o `DeploymentOperation` e abre a tela da operação. Reenviar "Continuar" para o mesmo plano reabre a mesma operação. A CLI `preview` consulta o plano a cada 2 s e imprime o plano final; uma falha sai no stderr com código 1.

**Compatibilidade:** o agente precisa estar na linha do núcleo de destino ou acima: a prévia recusa com `agent_update_required`, e o [guia de atualização](updates.md#atualizar-o-agente) mostra como atualizá-lo. Um núcleo novo com agente antigo de antes da prévia assíncrona recusa a prévia com "Deployment agent is older than this core."; um núcleo antigo com agente novo trata `{id, state}` como resposta inválida e mostra o erro, sem efeito colateral.

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

**Pacote de release no upload (#495).** `upload` (POST `/uploads`) aceita também um pacote de release. O agente o verifica inteiro (assinatura, inventário, locks e o core embutido, com o mesmo publicador e versão), guarda o pacote e o pacote do core que veio dentro em `uploads/` (0400; reenviar o mesmo arquivo devolve a mesma resposta) e, quando o pacote traz o alvo deste servidor, registra o ambiente do publicador. A resposta é `{"digest", "kind": "release", "manifest", "core": {"digest", "manifest"}, "target", "agent": {"version"}}`; `target` é `null` quando o pacote não traz a plataforma do servidor (ou o agente não consegue determiná-la). O plano usa o `core.digest`, como qualquer pacote de core; na tela, enviar um pacote de release abre "Preparar plano" já com a ação "Atualizar núcleo". Um pacote de componente responde como antes, mais `kind`. O mesmo core com outro conjunto de dependências é recusado com 409 `release_environment_conflict` (o publicador publica outra versão); um pacote não verificável responde `package_verification_failed` e nada fica guardado. O plano traz `dropped_dependencies`: as distribuições do `requirements.lock` do release ativo que o release novo deixa de instalar, mostradas na prévia como "Dependências que deixam de ser instaladas".

**Uma linha por vez (#493).** O agente recusa atualizar o core pulando uma linha minor, qualquer que seja o `from_versions` que o publicador declarou: de `0.5.2`, a prévia de um `core_update` para `0.7.0` falha com 409 `version_jump_forbidden`, mesmo que o pacote aceite `>=0.1.0`. A regra é do lado de quem instala, vem depois de `unsupported_core_upgrade` e `downgrade_forbidden` e vale só para o core; apps seguem só o `from_versions` delas. Correções podem ser puladas: de `0.5.2` para `0.6.4` passa direto; de `0.9.x` o próximo passo é `1.0.x`. A chave `allow_version_jumps: true` do arquivo do agente desliga a regra numa emergência (padrão `false`; um valor que não seja booleano recusa a configuração com `invalid_boolean_setting`); ela não existe na tela nem numa requisição.

**Caminho de atualização.** `check-updates` (POST `/catalog/check`) devolve, além de `sources` e `releases`, a chave `paths`: para o core e para cada app instalada que não seja `project-local`, `{component_id, kind, installed, latest, steps, blocked}`. `steps` é a sequência de versões a aplicar, um passo (prévia, confirmação, manutenção, backup, migrations e cutover) por vez: em cada ponto o agente escolhe a maior versão compatível que aceita a versão alcançada (e, no core, que está na mesma linha ou na seguinte). `blocked` diz por que o caminho não chega a `latest`: `missing_intermediate_release` (falta um release de uma linha intermediária) ou `incompatible_runtime` (nenhuma versão mais nova serve ao runtime). Para o resultado sem consultar a rede, `update-status` (GET `/updates/status`) lê a última consulta (`checked_at` e `sources`, `null` e `[]` antes da primeira) e recalcula `releases` e `paths` contra o inventário atual, a partir dos índices já verificados no cache, que são verificados de novo a cada leitura: um cache adulterado ou vencido não oferece nada e não derruba as outras fontes.

**Passo a passo pela tela (#500).** "Novas versões" mostra a última consulta (`update-status`, sem rede) e, para o core e cada app, a versão instalada, a mais nova e a lista ordenada de passos do caminho; só o primeiro passo tem o botão "Preparar este passo" (os seguintes aparecem como "Depois: <versão>"). "Verificar agora" consulta as fontes de novo e volta à página. Um bloqueio aparece em texto: `missing_intermediate_release` ("Falta publicar uma versão intermediária") ou `incompatible_runtime` (nenhuma versão mais nova serve ao Python ou ao núcleo desta instalação); com passos e bloqueio, o caminho para no último passo possível. A tabela completa continua num bloco recolhido, e o agente recusa o que estiver fora do caminho (`version_jump_forbidden`, mostrado em texto). Preparar um passo do core que exige um agente de linha mais nova mostra "Atualize o agente primeiro", com o botão "Atualizar agente" (quando `agent_root` está configurado) ou a instrução do `agent-install`; depois do reinício, prepare o passo de novo. Ao fim de uma operação bem-sucedida, a página da operação mostra o "Próximo passo" (ou que não há mais atualizações pendentes). Cada passo continua sendo uma operação completa: prévia, confirmação, manutenção, backup, migrations e cutover; não há aplicação automática. Buscar de novo uma versão que já está em `uploads/` não baixa nada: os bytes guardados passam pela mesma verificação do upload.

Na listagem, uma entrada do **core** é comparada só com o core instalado (`compatibility.skeleton`) e com o Python do runtime: o release do core traz o próprio Django, então a faixa `compatibility.django` dele não é comparada com o Django instalado hoje. A prévia continua conferindo tudo contra o runtime alvo. As entradas de apps seguem comparando as três faixas.

```sh
"$AGENT" --config "$CONFIG" update-status
```

Retirada não usa `migrate zero`: preserva schema, AppInstallation/configuração, ACLs/auditoria e o pacote assinado. As migrations históricas são copiadas byte-exact para archives sem modelos/rotas/hooks; apenas migrator carrega os labels. Preview rejeita imports de negócio retirado, histórico alterado e qualquer migration histórica retida ainda não aplicada. Reinstalação confere identidade/histórico/schema e deixa reativação organizacional explícita. Downgrade é recusado.

Atualização/reinstalação não transforma configuração de tenants: mudança incompatível de `runtime.configuration_fields` é bloqueada com `configuration_upgrade_requires_review`; `runtime.default_enabled` é ignorado em todo núcleo publicado (0.1.0 em diante). Publique um release com contrato compatível ou realize primeiro procedimento de evolução versionada revisado; o agente não substitui configuração persistida por defaults novos nem ativa organizações silenciosamente.

`catalogue_sources` é uma lista opcional de fontes fixas do operador, por exemplo `{"id":"platform-stable","path":"/srv/skeleton/catalog/index.json","channels":["stable"]}` ou `{"id":"platform-stable","url":"https://releases.example.org/index.json","channels":["stable"]}`. O índice é assinado, não expirado e monotônico; download HTTPS fica na mesma origem, sem redirects. Sem fontes configuradas a consulta retorna listas vazias, não uma alegação de atualização. Estados `available`, `incompatible`, `current` e source `unavailable` são distintos. Cada fonte tem no máximo uma de `path`, `url` ou `github` (duas juntas são `invalid_catalogue_sources` na carga; nenhuma deixa a fonte `unavailable` com `invalid_catalogue_source` na consulta).

**Fonte GitHub (#498).** `{"id": "skeleton", "github": "alaorneto/skeleton", "publisher": "skeleton", "channels": ["stable"], "private": false}` lê o índice `skeleton-index.json` do release mais recente do repositório (o oficial ou um fork) e baixa os pacotes pela API. `publisher` é obrigatório, tem de estar em `approved_publishers` e fica **fixado** na fonte: um índice assinado por outro publicador, mesmo aprovado para outra coisa, deixa a fonte `unavailable` com `catalogue_publisher_mismatch`. A integridade não depende do GitHub: o índice é assinado e cada entrada fixa o SHA-256 e a assinatura do pacote; um repositório comprometido sem a chave do publicador não instala nada. A seção opcional `github` da configuração ajusta a API e o download:

```json
"github": {"api_url": "https://api.github.com", "download_hosts": [".githubusercontent.com"], "token_file": "/srv/skeleton/private/github-token"}
```

`api_url` é HTTPS, sem usuário, senha, query nem fragmento; `download_hosts` é a lista de hosts para onde o download de um asset pode redirecionar (uma entrada com `.` na frente é sufixo); `token_file` é o arquivo privado (0600, do usuário do agente) com um token de escopo mínimo — leitura de conteúdo só dos repositórios consultados — usado só pelas fontes com `"private": true` e só nas requisições ao host de `api_url`. Erros de forma: `invalid_github_settings`; uma fonte `private` sem `token_file` é `invalid_catalogue_sources`. O cache de uma fonte GitHub é chaveado pelo repositório e pelo publicador, não pelo id: o piso de geração acompanha o repositório, e remover e recadastrar a fonte não o zera. No índice de uma fonte GitHub, `artifact` é sempre `<tag>/<asset>`; outra forma é `catalogue_artifact_path_rejected`.

Uma consulta custa duas requisições por fonte GitHub (o release mais recente e o índice) e um download mais duas; sem token, o GitHub limita a 60 requisições por hora por IP, e o agente não tenta de novo sozinho. Códigos novos: `github_release_not_found` (404 do release), `github_rate_limited` (403 ou 429 por limite), `github_token_missing`, `github_asset_url_rejected` (URL de asset fora do prefixo da API do repositório, ou redirecionamento que não é HTTPS, porta 443 e host da lista, ou um segundo redirecionamento), `source_index_missing` (o release ainda não tem o índice: logo depois de publicar, o workflow de artefatos leva alguns minutos) e `github_asset_missing`; o resto é `catalogue_unavailable`. GitHub Enterprise Server funciona pelas mesmas chaves, mas não é testado.

**Fontes cadastradas na tela (#499).** As fontes têm duas origens: o arquivo do agente (`catalogue_sources`, qualquer tipo) e o passo "Fontes" de `/platform/updates/`, onde quem tem `updates.operate` cadastra e remove repositórios do GitHub (`dono/nome`), escolhendo o publicador entre os ids de `approved_publishers`. A tela escolhe só **onde procurar**; **em quem confiar** continua no servidor: ela nunca cadastra chave pública, token, `api_url`, hosts de download, caminho nem URL, e as fontes do arquivo aparecem como somente leitura (sem o caminho nem a URL). O agente guarda as cadastradas em `sources.json` no armazenamento privado (0600, escrita atômica sob o lock de deployment), com um histórico das últimas 100 alterações (ator, data, ação), e as consulta depois das do arquivo. As recusas têm códigos próprios: `invalid_source`, `source_id_reserved` (id de uma fonte do arquivo), `publisher_not_approved`, `github_token_missing` (repositório privado sem `github.token_file`), `too_many_sources` (limite de 16) e `source_not_found`. Um `sources.json` corrompido, com um id repetido ou com um publicador que deixou de ser aprovado falha fechado com `invalid_managed_sources` (a consulta para até o operador corrigir ou apagar a entrada no arquivo). Remover uma fonte não apaga o cache do índice: o piso de geração do repositório sobrevive a recadastrá-lo. Para seguir um **fork**: o operador inclui a chave pública do publicador do fork em `approved_publishers`, e então a tela aceita o repositório do fork com esse publicador. `skeleton-deployment-agent --config ... sources` lista as fontes pelo shell; a API é `GET /sources`, `POST /sources/set` e `POST /sources/remove`.

**Consulta periódica e aviso (#501).** O agente consulta as fontes sozinho a cada `update_check_interval` segundos (padrão `21600`, 6 horas; `0` desliga; número não negativo, senão `invalid_numeric_setting`). A primeira consulta acontece 120 segundos depois de o agente subir, para que um agente em laço de reinício não martele a rede, e a consulta é pulada sem tocar na rede quando não há fontes, quando o deployment não foi inicializado e durante manutenção, operação ou preparação de plano. Ela só atualiza os índices em cache: nada é baixado nem aplicado. Para fontes GitHub sem token, não use intervalo menor que 1 hora (o limite é de 60 requisições por hora por IP). Nas páginas de `/platform/`, quem tem `updates.operate` vê uma faixa ("Há uma versão nova do núcleo…", "Há atualizações para N apps…") com o link "Ver o passo a passo", e a visão geral mostra o mesmo resumo ou "Tudo atualizado"; nas páginas de atualização a faixa não aparece. O resumo vem de `GET /updates/status` (prazo de 2 segundos) e fica 5 minutos no cache do Django (1 minuto depois de uma falha): o atraso máximo do aviso é o intervalo do agente mais 5 minutos. "Verificar agora" e o fim de uma operação recalculam o aviso. Agente indisponível ou lento só esconde a faixa; nenhuma página cai por causa dela.

**Saída de rede do agente.** O agente só contata o host de `api_url` (padrão `api.github.com`), os hosts de `download_hosts` no download (padrão `*.githubusercontent.com`) e os hosts das fontes `url`. Libere só esses no firewall de saída. O redirecionamento do download é seguido à mão, uma vez, numa requisição nova sem `Authorization`; a URL assinada do redirecionamento e o token nunca aparecem em erro, log, journal ou resposta.

API equivalente pelo socket (todas as chamadas exigem `Authorization: Bearer <token>`; POST JSON usa `Content-Type: application/json` e Content-Length):

| Método/path | Corpo |
|---|---|
| GET `/inventory` | nenhum |
| GET `/operations/<uuid>` | nenhum; inclui status_url quando habilitado |
| POST `/uploads` | bytes `.skapp`, `application/octet-stream` |
| POST `/preview` | `{"action":"install","artifacts":["<sha256>"],"app_ids":[]}`; responde `{"id":"<uuid>","state":"preparing"}` |
| GET `/plans/<uuid>` | nenhum; `state` `preparing`, `ready` (com `plan`) ou `failed` (com `error`/`details`) |
| POST `/submit` | `{"plan_id":"<uuid>","plan_digest":"<sha256>","generation":0,"operation_id":"<uuid>","actor_id":"<uuid>","request_id":"<uuid>"}` |
| POST `/status-ticket` | `{"operation_id":"<uuid>"}`; permite emitir link antes de submit |
| POST `/catalog/check` | `{}` |
| GET `/updates/status` | nenhum; última consulta e `paths` recalculado, sem rede |
| POST `/agent/update` | `{"release":"<sha256 do pacote de release>","actor_id":"<uuid>"}`; responde `{"state":"restarting"}` e o agente sai com 75; num cluster, `{"state":"rolling"}`, e o primário reinicia depois dos outros nós (#689) |
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

### Atualizar o agente

O agente informa a própria versão em `GET /inventory` (`agent.version`, `null` quando roda de um checkout de código, e `agent.self_update`, verdadeiro com `agent_root`), e "Versões instaladas" a mostra. Como o agente valida e instala o core, a prévia de um `core_update` para uma linha acima da do agente é recusada com 409 `agent_update_required` (os `details` dizem a versão do agente e a linha exigida); diferença só de patch não bloqueia, e um agente de versão desconhecida não é travado.

- **Pela tela.** Ao enviar (ou buscar) um pacote de release que traz um agente mais novo, a tela oferece "Atualizar agente" (POST com CSRF, `updates.operate` e autenticação recente; só o digest do pacote segue pelo socket, para `POST /agent/update` com `{"release", "actor_id"}`). O agente, sob o lock de deployment e sem operação pendente, revalida o pacote, exige que ele traga o agente (`release_without_agent`), que o publicador seja o do core instalado ou um de `agent_publishers` (403 `agent_publisher_not_allowed`), que a versão seja maior (`agent_already_current`, `agent_downgrade_forbidden`) e que haja alvo para o interpretador do próprio agente (`agent_target_unavailable`). Monta `versions/<nome>` sem rede (`pip install --no-index --only-binary=:all: --require-hashes` do lock do alvo mais o core e o agente, `pip check`) e roda `selfcheck` do agente novo com a mesma configuração; qualquer falha apaga o diretório parcial, mantém o agente atual e responde `agent_build_failed` (a saída do pip fica fora da resposta, da tela e do journal). Depois troca `current` atomicamente (`os.replace`), registra o evento em `agent-updates/` no armazenamento privado, responde `{"state": "restarting"}` e sai com 75. Recarregue a página em alguns segundos.
- **Pelo servidor.** `agent-install --release <pacote de release> [--root <diretório>]` instala ou atualiza a partir de um arquivo, com o serviço parado ou não, e imprime os próximos passos; não reinicia nada sozinho. `agent-rollback [--root <diretório>]` aponta `current` de volta para a versão anterior (recusa com `agent_rollback_unavailable` quando não há anterior pronta). Downgrade só existe por esse comando offline.

```sh
# Migração de uma instalação existente (/opt/skeleton-agent/venv): a primeira versão com este recurso
# é instalada como hoje; depois:
sudo -u skeleton-agent install -d -m 0755 /opt/skeleton-agent
sudo -u skeleton-agent /opt/skeleton-agent/venv/bin/skeleton-deployment-agent \
  --config /srv/skeleton/private/agent.json agent-install \
  --release /srv/skeleton/artifacts/skeleton-<versão>.release.skapp --root /opt/skeleton-agent
# Acrescente "agent_root": "/opt/skeleton-agent" ao agent.json e troque o ExecStart da unit para
# /opt/skeleton-agent/current/venv/bin/skeleton-deployment-agent ... serve
sudo systemctl daemon-reload && sudo systemctl restart skeleton-agent
"$AGENT" --config "$CONFIG" agent-rollback   # se o agente novo não subir; depois systemctl restart
```

**Regra de compatibilidade de formato.** O agente da linha N precisa conseguir validar o pacote de release da linha N+1 para se atualizar a partir dele. Por isso, um formato novo de manifesto, de pacote ou de índice só pode ser **emitido** a partir da linha seguinte àquela em que os leitores passaram a aceitá-lo. Limite aceito: se o agente novo não subir por um erro que o `selfcheck` não pegou, o systemd fica reiniciando; a volta é `agent-rollback` seguido de `systemctl restart skeleton-agent`. Um supervisor que não reinicia em saída diferente de zero não serve: use `Restart=on-failure` ou equivalente.

### Agente em vários nós (#678)

Com vários nós de app, cada nó roda o próprio agente, e um deles é o **primário**: o único que migra, faz backup e decide as fases de uma atualização. Os outros são **seguidores**: preparam e trocam o release do próprio nó. A chave `cluster` do `agent.json` liga o modo; sem ela, o agente é o de sempre, de um nó só.

```json
{
  "cluster": {
    "node_id": "node-a",
    "role": "candidate",
    "cluster_url": "https://node-a.internal:9443",
    "preferred": true
  }
}
```

- `node_id`: nome estável do nó (`[a-z0-9-]`, até 40 caracteres).
- `role`: `candidate` (pode ser primário; tem o perfil `migration`, com a credencial owner) ou `follower` (nunca é primário; recusa ter `MIGRATION_DATABASE_URL` ou `backup_env_file`: `follower_has_owner`). O seguidor declara `coordination_env_file`, um arquivo 0600 com `COORDINATION_DATABASE_URL` da role `skeleton_agent` (sem ele, `missing_coordination_credentials`).
- `cluster_url`: o endereço do canal entre agentes, na rede privada (`https://`).
- `preferred`: o candidato que começa na primeira partida; os outros esperam 60 s antes de tentar.

**Coordenação no PostgreSQL.** O estado do cluster fica no schema `skeleton_deploy` do banco da instância, criado pelo `deploy/postgres/bootstrap.sql` (dono `skeleton_owner`) junto da role `skeleton_agent`. Os candidatos criam as tabelas na primeira partida; a role runtime e o web nunca leem o schema. Numa instalação existente, aplique o bootstrap de novo (ele é idempotente) antes de ligar o modo cluster.

**Como o primário é definido.** É o candidato que segura a trava de sessão do PostgreSQL do cluster (`pg_try_advisory_lock`), numa conexão direta (nunca pelo PgBouncer), e a **época** atual da linha única `skeleton_deploy."primary"`. O PostgreSQL só solta a trava quando a sessão do primário termina, então nunca há dois primários enquanto o primeiro está conectado. Cada passo que muda o banco (migrations, catálogo, sincronização do estado, troca do release) confere antes que a conexão da trava continua viva e que a época é a dele; um primário que travou, perdeu a trava e voltou recebe `not_primary` e não executa o passo. Na primeira partida de um cluster novo, só o candidato que pega a trava instala a geração 0.

**Como o primário é reatribuído.**

- **Planejado:** com o cluster ocioso, `skeleton-deployment-agent --config ... cluster handover --to node-b` pede ao primário que solte a trava; o candidato indicado a pega (os outros esperam 30 s para dar a vez a ele) e a época sobe. Com operação `queued`, `running` ou `interrupted`, recusa (`cluster_busy`); destino que não é candidato vivo ou que já é o primário, `handover_target_invalid`.
- **Perda do primário:** a sessão dele cai (as conexões usam keepalives de TCP, e a queda é notada em cerca de 30 s), a trava fica livre e o primeiro candidato que tentar (a cada 15 s) vira o primário com a época seguinte. Servir tráfego não depende do primário: perdê-lo só suspende atualizações e instalação de apps. Se havia operação em curso, o novo primário a marca `interrupted` e recusa prévias e execuções (`recovery_or_operation_pending`) até o operador escolher `recover ... abort|restore|complete`, como num nó só.
- Enquanto não é o primário, o agente responde `not_primary` a prévias e execuções.

**`storage_root` compartilhado entre candidatos.** O journal, os planos, os uploads e os backups ficam em `storage_root`. Com dois ou mais candidatos, ele precisa ser um diretório compartilhado só entre eles (NFS, EFS, CephFS), escrito só por quem segura a trava: é o que deixa o novo primário retomar o journal do antigo. Cada candidato deixa uma marca em `storage_root/.cluster/<node_id>`; um candidato que não vê a marca de outro candidato vivo não tenta a trava (`cluster_storage_not_shared`, em `cluster status`). `releases_root` continua local em cada nó. Com um só candidato, `storage_root` pode ser local, mas perder esse nó exige restaurar o `storage_root` de backup antes de outro nó virar candidato.

**Canal entre agentes (#680).** Cada nó escuta em `cluster.listen` (um endereço privado da rede entre nós, nunca o proxy público) um HTTPS com TLS mútuo: o certificado do nó (`tls_cert`, `tls_key`) é emitido por uma CA privada do cluster (`tls_ca`), e o cliente confere o certificado do outro lado contra ela e contra o nome do `cluster_url`. Além do TLS, cada pedido leva no cabeçalho `X-Skeleton-Cluster` um HMAC-SHA256, com o segredo do cluster (`secret_file`, 0600, o mesmo em todos os nós, gerado uma vez com `cluster keygen --output <arquivo>`), sobre o método, o caminho, a hora, o nó, a época e o SHA-256 do corpo; assinatura errada, relógio a mais de 60 s ou nó que a coordenação não conhece recebem o mesmo 401 (os nós precisam de NTP). O canal só tem estas rotas: `GET /v1/health`; `GET /v1/artifacts/<sha256>` (no primário, só de um pacote referenciado por um plano ou pelo estado ativo; o seguidor confere o SHA-256 e depois a assinatura do publicador antes de usar, então o canal nunca decide que código roda); `POST /v1/uploads` e `POST /v1/forward` (no primário). O web de cada nó continua falando só com o socket Unix do próprio agente: no seguidor, o socket responde sozinho `GET /inventory` e `GET /cluster` e encaminha ao primário todo o resto (prévia, execução, envio de pacote, fontes, operações), com no máximo 4 pedidos encaminhados ao mesmo tempo; sem primário, `no_primary` (503); primário inalcançável, `primary_unreachable` (503). Uma execução aprovada pelo web de outro nó guarda no journal do primário a origem (`source: node:<id>`), que vem do canal autenticado e nunca do corpo do pedido. Sem os arquivos de TLS válidos ou sem o segredo, o canal não sobe (`invalid_cluster_channel`). Para a CA e os certificados:

```sh
openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 3650 -subj "/CN=skeleton-cluster-ca" -keyout ca.key -out ca.crt
openssl req -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -subj "/CN=node-a" -addext "subjectAltName=DNS:node-a.internal" -keyout node-a.key -out node-a.csr
openssl x509 -req -in node-a.csr -CA ca.crt -CAkey ca.key -CAcreateserial -days 825 -copy_extensions copy -out node-a.crt
```

Guarde `ca.key` fora dos nós; cada nó recebe `ca.crt`, o próprio par e o segredo, todos 0600 para a conta do agente.

**Atualização em vários nós (#681).** O primário executa a operação como num nó só e, em quatro pontos, anuncia uma fase na tabela `skeleton_deploy.rollout` e espera a resposta de cada nó que estava vivo quando a operação começou:

| Fase | O que cada seguidor faz | Se um nó falha ou não responde |
|---|---|---|
| `prepare` (antes de qualquer manutenção, até 30 min) | recebe pelo canal o release que o primário montou e congelou e o instala só se o `owned_snapshot` dele bater, arquivo por arquivo, com o `owned-files.json` do plano (`prepared`) | a operação termina `failed` com `follower_prepare_failed` (o detalhe traz o nó e o motivo), sem marcador nem migration |
| `drain` (depois do drain do primário, até 10 min) | grava o próprio marcador de manutenção e para os serviços locais (`drained`) | `follower_drain_failed`: fase `abort`; cada nó, o primário também, tira o marcador e volta a servir o release que tinha; nenhuma migration rodou e a próxima atualização pode começar |
| `cutover` (depois da troca e da saúde do primário, até 15 min) | troca o ponteiro, reinicia os serviços e confere a saúde local (`healthy`) | `follower_cutover_failed`: a operação fica `failed` **sob manutenção** em todos os nós; o operador escolhe `recover ... complete` depois de o nó voltar, ou `cluster forget <nó>` e então `complete`: o `complete` anuncia o cutover de novo (uma ordem nova, que o nó que falhou executa outra vez) e não espera um nó removido |
| `resume` (depois do commit) | tira o marcador (`resumed`) | registrado no log; o nó que não reabriu fica fora do balanceador pela própria sonda |

O seguidor nunca monta um release sozinho: montar exige o perfil `migration` (o grafo de migrations e a exportação da infraestrutura) e a saída estática não é reproduzível byte a byte entre hosts. Ele recebe o release pronto do primário, que já tem a credencial owner e por isso já é de confiança, e o confere contra o plano. Os nós precisam da mesma imagem e do mesmo `releases_root` (os caminhos do virtualenv são absolutos). Um nó que estava fora durante uma atualização, ou um nó novo, vê na partida que o cluster roda outro release (`skeleton_deploy."primary".release`, que o primário publica a cada commit), instala esse release e só então inicia os serviços: nos containers de aplicação de um cluster, o inicializador roda com `--wait-for-agent` e não sobe o ponteiro sozinho, então um nó atrasado nunca serve código velho contra o banco migrado.

**Atualização nó a nó e reinício em rolagem (#682).** A prévia diz se o plano pode trocar o release nó a nó, sem janela (`rollout_mode: "rolling"`), ou precisa da janela (`"window"`, com `rollout_reason`): só um patch da mesma linha `major.minor`, sem nenhuma migration e sem app instalada ou removida, roda em rolagem, porque o release antigo e o novo servem o mesmo banco. O operador pode forçar a janela no envio (`mode: "window"`); forçar a rolagem num plano que não pode é recusado (`rolling_not_eligible`), e num nó só a janela vale sempre. Na rolagem, todos os nós recebem o release primeiro, os checks rodam uma vez no primário e cada nó, o primário primeiro, sai do balanceador, para, troca, sobe e confere a saúde antes do próximo: o agente grava o marcador de drenagem (`drain_marker` no `agent.json`; no web, a variável de bootstrap `SKELETON_DRAIN_MARKER`, com o mesmo arquivo), e com ele `/health/ready/` responde 503 `{"status": "draining"}` enquanto o resto continua atendendo; depois de `drain_grace` segundos (padrão 20, de 0 a 600) o nó para. O balanceador precisa checar `/health/ready/` com intervalo menor que `drain_grace`. Um nó que falha volta ao release que tinha e a operação termina `failed` com `rolling_node_failed` (o detalhe traz o nó); os nós já trocados ficam no release novo, que serve o mesmo banco, e `recover ... abort` devolve o primário ao anterior. `cluster restart` (CLI do primário, ou `POST /cluster/restart` da API de controle) reinicia os serviços nó a nó com o mesmo release, para aplicar uma configuração nova sem tirar o site do ar; com operação ou recuperação em curso, recusa com `cluster_busy`.

**Versão dos agentes (#689).** Uma operação só começa com todos os nós vivos na mesma versão do agente: senão o primário recusa com `agent_versions_differ`, antes de qualquer fase. `POST /agent/update` num cluster (pedido em qualquer nó, encaminhado ao primário) confere o pacote como num nó só, grava a versão e o digest do pacote em `skeleton_deploy.agent_target` (só o primário, com a cerca da época) e responde `{"state": "rolling"}` sem reiniciar. Em segundo plano, o primário espera os outros nós vivos informarem a versão nova (até 15 min) e então monta a própria versão, troca `current`, registra o evento em `agent-updates/` e reinicia (75). Cada outro nó, no laço de coordenação e fora de uma operação, compara o alvo com a própria versão: com `agent_root`, baixa o pacote pelo canal (o primário serve o digest do alvo), confere o SHA-256, a assinatura, que o pacote é o do alvo e que o publicador é o do core instalado ou de `agent_publishers`, monta, troca e reinicia; uma falha fica no estado do nó (`cluster status`) e o mesmo alvo não é tentado de novo até o agente reiniciar. Sem `agent_root` (containers), o nó não muda nada e o estado mostra `agent_update_manual`: atualize a imagem de cada nó. Se o primário reiniciar e outro candidato assumir, o alvo continua gravado. A volta é `agent-rollback` em cada nó.

**Comandos.** `cluster status` mostra o primário, a época, a idade da última renovação e cada nó (papel, release ativo, fase, último sinal), sem segredos; `cluster forget <node_id>` remove um nó desativado (nunca o primário: `cannot_forget_primary`). A API de controle responde `GET /cluster` com o mesmo conteúdo (`{"mode": "single"}` num nó só).

### Backend Compose

Os arquivos Compose do repositório **não** usam este backend: o serviço `agent` roda o backend diretório e controla os serviços pelos [arquivos de controle](#serviços-em-containers-sem-socket-docker), sem socket Docker (#654). Este backend fica para quem roda o agente no host, com acesso ao Docker dele, e continua com os limites abaixo.

O backend **diretório** prepara um virtualenv por release, troca o ponteiro `current` e usa o supervisor configurado. O backend **Compose** prepara uma imagem por release, roda um container migrator separado e recria o serviço web. São duas formas de executar o mesmo contrato, não duas evidências equivalentes: um ensaio bem-sucedido em diretório não valida Docker, permissões de volumes ou recuperação Compose.

**Overrides temporários** (#245): cada comando do Compose recebe um override privado (0600, nome único em `storage_root`) com a imagem, o volume e o ambiente do perfil, inclusive a credencial owner no perfil `migration`, e o arquivo é removido assim que o comando termina, com sucesso ou falha. Overrides deixados por uma parada abrupta são apagados quando o daemon (`serve`) parte.

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

Esta seção é o backup que o agente faz antes de cada migration, para desfazer uma atualização. O backup de desastre, fora do servidor, os segredos a guardar e a recuperação de uma instância inteira estão em [backup e recuperação](backup-recovery.md).

**Backup** é a cópia consistente de banco e recursos necessários; **restore** é a reposição dessa cópia; **rollback** é retornar a um estado anterior. Só trocar arquivos ou o ponteiro de release não desfaz migrations ou escritas. A indisponibilidade planejada começa quando o tráfego é bloqueado e os escritores são drenados, e termina somente após verificar banco, release e workers.

**Conferência antes da manutenção** (#246). A prévia e o início da execução conferem, antes de criar o marcador de manutenção ou drenar o web, que cada caminho de `recovery_paths` existe, é absoluto e não é nem contém symlink (`invalid_recovery_path`, `recovery_path_symlink`), que `pg_dump`, `pg_restore` e `psql` (e o `docker`, no backend Compose: `docker_unavailable`) são executáveis (`backup_tool_unavailable`) e que o `smoke_token_file` passa pela mesma regra de permissão do token de controle (`insecure_private_file`). Um erro desses termina a operação `failed` sem manutenção: corrija a configuração e submeta de novo. As conferências dentro do backup continuam como segunda barreira.

**Logs da operação** (#244). Cada comando que uma operação executa (backup, `migrate`, `pip`, `docker`, hooks) grava a saída em `storage_root/logs/<operation_id>/NN-<código>.log`, com os últimos 256 KiB, e o evento `failed` do journal cita só o nome do arquivo (`"log": "03-migration_failed.log"`). Esses arquivos podem conter o que um comando imprimiu com a credencial owner no ambiente: o diretório é 0700 e os arquivos 0600, legíveis só pela conta do agente, e nem a API de controle nem a página de status mostram o conteúdo ou o nome. Ao investigar uma operação `failed`, leia o arquivo citado no último evento do journal (`storage_root/operations/<operation_id>.json`). Comandos fora de uma operação (prévia, ambiente alvo) não gravam log.

Antes de escolher recuperação, identifique no journal a primeira migration e a intenção de reabrir tráfego:

| Momento da falha | Ação segura e limite |
|---|---|
| Antes de iniciar migrations | `abort` pode verificar e reiniciar o release anterior. Não executar os outros caminhos em sequência. |
| Depois de iniciar migrations, ainda sem novas escritas | `restore` requer backup íntegro; alternativamente `complete` conclui somente o plano aprovado. Voltar só o código não basta. |
| Após intenção de liberar tráfego | Trate novas escritas como possíveis, mesmo sem confirmação de sucesso. Restore pode perdê-las; aprove antes um plano explícito de perda/reconciliação. |

**Resultado esperado:** recuperação confirmada por checks runtime e saúde do release, seguida da liberação controlada de tráfego. **Se falhar:** mantenha manutenção e evidências; não remova o marker manualmente, não habilite runtime privilegiado e não invente nós de migration.

**Barreira antes de recuperar:** confirme que o release/geração ativos ainda correspondem à origem ou ao destino da aprovação escolhida. Uma operação antiga não pode desfazer um deployment posterior: `operation_superseded` recusa esse caso antes de manutenção, drain ou restore. Se existir outra operação `queued`, `running` ou `interrupted`, resolva seu estado primeiro; `recovery_or_operation_pending` impede recuperar em paralelo. Não contorne essas recusas alterando inventário/journal nem escolhendo outra operação sem examinar seu vínculo.

Adicione `recovery_paths`, por exemplo `["/srv/skeleton/uploads","/srv/skeleton/private/mfa-keys.json"]`, para recursos mutáveis/chaves que precisam acompanhar o DB. Eles devem existir, ser externos a releases e não conter symlinks. Pare todos os escritores antes do snapshot. Não inclua diretório de estado/journal do agente, o marker ou o próprio backup em `recovery_paths`.

**Backup externo (PITR, #683).** Com o banco crescendo, o dump do schema inteiro alonga a janela de manutenção. Quem tem backup contínuo do PostgreSQL (arquivamento de WAL com pgBackRest ou WAL-G, ou o serviço gerenciado) declara `"backup": {"mode": "external", "acknowledge_continuous_backup": true}` no `agent.json` (sem a confirmação, `external_backup_not_acknowledged`). Então o passo de backup não faz dump: com a owner, cria o ponto de restauração `skeleton-<operação>` (`pg_create_restore_point`) e registra o nome, o LSN e a hora no `backup.json` (`"kind": "external"`), além de copiar os `recovery_paths`. `pg_create_restore_point` exige superusuário ou o `GRANT EXECUTE ON FUNCTION pg_create_restore_point(text) TO skeleton_owner` dado pelo administrador do banco: sem ele, a conferência prévia recusa antes de qualquer manutenção (`restore_point_not_permitted`); `"restore_point": false` dispensa o ponto e confia só no relógio do PITR. Com backup externo, `recover <op> restore` recusa (`external_restore_required`) e informa o ponto, o LSN e a hora; depois de restaurar o banco até esse ponto pelo seu PITR, rode `recover <op> abort --external-restored`: o agente confere que as migrations aplicadas voltaram às do release anterior (senão, `external_restore_incomplete`) e só então reabre com ele. Com arquivos no S3, ligue o versionamento do bucket: os `recovery_paths` não cobrem o armazenamento de objetos.

Antes da primeira migration, o journal registra `backup` apontando para `agent-state/backups/.../backup.json`: PostgreSQL custom dump do schema `skeleton` inteiro (negócio, tabelas técnicas, migrations e auditoria; o agente recusa outro backend), hashes e snapshots exatos dos recursos. Extensões/roles PostgreSQL são provisionadas separadamente; dados fora de `skeleton` exigem procedimento offline específico. Um backup parcial de crash é preservado; uma nova tentativa usa outro diretório. Backup/journal/uploads/plans permanecem privados e fora do DB da aplicação. Nunca apague o release anterior, os artefatos aprovados ou esses snapshots enquanto houver recuperação/retention pendente.

#### Retenção e limpeza

O agente nunca apaga nada sozinho: a limpeza é uma decisão do operador, offline, com o daemon parado ou ocioso (#245):

```sh
skeleton-deployment-agent --config /etc/skeleton/agent.json prune                       # só lista
skeleton-deployment-agent --config /etc/skeleton/agent.json prune --plan-days 7 --apply # remove
skeleton-deployment-agent --config /etc/skeleton/agent.json prune --backup-days 30 --apply
```

Sem `--apply`, nada muda. O comando é recusado com marcador de manutenção ou com operação `queued`, `running` ou `interrupted`, e nunca está na API de controle. Candidatos: prévias não submetidas mais velhas que `--plan-days` (plano e release), releases de operações `succeeded`, `restored` ou `cancelled` que não são a mais recente, e as imagens `<image_repository>:<release>` do Compose (uma falha ao remover a imagem só entra em `errors`). Backups e logs de operação (`backups/<operação>`, `logs/<operação>`) só saem com `--backup-days`, e só de operações concluídas. Ficam sempre: o release ativo, o `release` e o `previous_release` da última operação `succeeded`, o `release`, o `previous_release` e o backup de toda operação `failed` que `recover` ainda aceitaria (uma operação superada por um deployment posterior deixa de proteger), o `project_root`, o journal (`operations/`) e os artefatos `uploads/`. Um release cujo caminho está fora de `releases_root` não é apagado e aparece em `skipped`.

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
