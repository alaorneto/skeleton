# Backup e recuperação de desastre

Este guia diz o que guardar para que uma instância do Skeleton volte depois de um desastre (perda do servidor, do banco, dos anexos, do estado do agente ou de uma chave) e como recuperá-la, no **sandbox** (um host no Coolify, `compose.sandbox.yaml`) e na **produção** (nós de app de um cluster com `compose.yaml` e o servidor de dados de `deploy/data-server/compose.yaml`, [alta disponibilidade](high-availability.md)). Vale para uma instância com várias apps instaladas.

Ele não substitui o backup que o agente faz antes de cada migration ([backup, crash e recuperação](deployment.md#backup-crash-e-recuperação)): aquele desfaz uma atualização que falhou e mora no mesmo servidor. Este é o backup de desastre, fora do servidor.

## Princípios

1. **Banco e código na mesma versão.** O banco guarda o resultado das migrations do núcleo e de cada app. Restaurar o banco de um momento exige rodar o núcleo e as apps na versão daquele momento: o release ativo e os pacotes das apps vão no mesmo conjunto de backup que o banco, ou o operador reinstala exatamente as mesmas versões. Um banco de ontem com o código de hoje deixa as migrations de hoje faltando (o `check_skeleton_schema` recusa), e o inverso também.
2. **Um conjunto de backup é coerente.** Banco, anexos, configuração e estado do agente de um mesmo momento. No sandbox, os escritores param durante a cópia. Na produção, o ponto de restauração do banco, a versão dos anexos e o `storage_root` do agente são escolhidos para o mesmo momento.
3. **Os segredos ficam fora do backup dos dados.** Os chaveiros de criptografia, a chave mestra e as senhas vão para um cofre de segredos (gerenciador de senhas da equipe ou cofre do provedor), com pelo menos duas pessoas com acesso. Um backup sem as chaves não abre; as chaves sem o backup não servem. Nenhum valor de segredo aparece neste guia nem no repositório.
4. **Fora do servidor, cifrado e com retenção.** Cópia diária para outro lugar (outro provedor ou região), cifrada antes de sair do servidor (restic, borg, rclone crypt ou age), com retenção (por exemplo, 7 diários, 4 semanais e 6 mensais) e imutabilidade quando o destino oferecer (object lock).
5. **Backup que nunca foi restaurado não conta.** O [teste de restauração](#teste-periódico-de-restauração) é parte do plano.

Metas sugeridas (ajuste ao contrato do serviço):

| Ambiente | Perda máxima de dados (RPO) | Tempo para voltar (RTO) |
|---|---|---|
| Sandbox | 24 h (backup diário) | 4 h |
| Produção | 5 min (PITR do PostgreSQL) | 1 a 2 h para o banco; minutos para perder um nó |

## Onde o estado mora

### Sandbox (`compose.sandbox.yaml` no Coolify)

Tudo fica num host. No Coolify, o nome real de cada volume leva o identificador do recurso na frente (confira com `docker volume ls`).

| Volume | O que guarda | Backup |
|---|---|---|
| `postgres_data` | O banco `skeleton`: schema `skeleton` (núcleo, apps, auditoria, sessões, jobs, o estado de deployment confirmado) e `skeleton_deploy` (vazio sem cluster) | **Sim**, por dump lógico (`pg_dump` dos dois schemas); a cópia do diretório só com o PostgreSQL parado e na mesma versão principal (16). Roles, senhas e a extensão `pg_trgm` vêm do bootstrap e das variáveis |
| `instance` | O SQLite da configuração da instância (segredos cifrados) e a chave mestra `/instance/keys/master.key` | **Sim**; a chave mestra vai também para o cofre, e a configuração, exportada, também ([abaixo](#configuração-da-instância)) |
| `files` | Os anexos (`/files`): arquivos dos registros, das apps, das exportações CSV, os logos, ícones e pacotes de tema | **Sim** |
| `agent_state` | O estado do agente (`/agent/state`): `active.json` (o release ativo e a geração), o journal das operações, os planos, os **pacotes enviados** (core e apps, em `uploads/`), as fontes (`sources.json`), os backups de atualização e os logs | **Sim** |
| `releases` | Os releases montados (o virtualenv do núcleo e das apps de cada geração) e o ponteiro `current` | **Sim** (grande, mas é o que faz o `active.json` voltar sem remontar nada) |
| `agent_private` | `agent.json` e os arquivos de ambiente do agente (com a credencial owner), escritos na primeira partida a partir das variáveis do Coolify | Recomendado (cifrado); regenerável |
| `control`, `control_state` | Socket, token de controle e estado do inicializador | Não: o agente os recria |
| Redis (sem volume) | Cache, limites de taxa, antirreuso do TOTP, códigos OIDC | Não: tudo tem validade curta |

### Produção (cluster)

Os nós de app não guardam estado de aplicação; o estado fica no servidor de dados, no armazenamento de arquivos, no cofre e no `storage_root` compartilhado dos candidatos.

| Peça | Onde | Backup |
|---|---|---|
| Banco da instância | O PostgreSQL do projeto da instância no servidor de dados (`deploy/data-server/compose.yaml`) | **Sim**: backup contínuo (PITR) e dump lógico diário |
| Anexos | Bucket S3 (`SKELETON_FILES_BACKEND=s3`) ou o volume compartilhado declarado (`SKELETON_FILES_SHARED=true`) | **Sim**: versionamento do bucket e cópia para outro bucket/região, ou snapshot do volume |
| Configuração da instância | O arquivo de `SKELETON_INSTANCE_ENV_FILE` (gerado por `instance export-env`), igual em todos os nós | **Sim**, no cofre, a cada mudança |
| Estado do agente | `storage_root`, o diretório de `SKELETON_AGENT_STATE_SOURCE` compartilhado pelos candidatos | **Sim**, diário e depois de cada operação |
| Releases | Volume `releases` de cada nó | **Sim, só o do primário**, depois de cada operação bem-sucedida (para a perda total); os outros nós remontam a partir do primário |
| `agent_private` de cada nó | Gerado na partida a partir das variáveis do nó | Não (regenerável); as variáveis ficam no cofre |
| Artefatos do cluster | CA privada, certificados dos nós, segredo do canal, TLS do servidor de dados | **Sim**, no cofre |
| Redis | Servidor de dados | Não |
| Nós de app | Hosts | Não: um nó perdido é substituído ([adicionar um nó](high-availability.md#roteiros)) |

## Apps instaladas

Uma instância com várias apps não tem um backup por app: tudo o que as apps guardam passa pelo núcleo.

- **Dados:** as tabelas de cada app ficam no schema `skeleton`, junto com as do núcleo, e entram no mesmo dump e no mesmo PITR. Isso inclui o schema mantido de uma app desinstalada com os dados preservados (`schema_retained`).
- **Configuração e ativação por organização:** no banco (`AppInstallation`), com o resto.
- **Arquivos:** pelo armazenamento do núcleo (`files` ou o bucket).
- **Código:** cada app vem de um pacote `.skapp` assinado. O pacote enviado pela tela fica em `storage_root/uploads/` e só existe ali; o que veio de uma fonte (GitHub) pode ser baixado de novo enquanto o publicador mantiver aquela versão. As apps first-party (`skeleton/contrib`, `SKELETON_BUNDLED_APPS`) vêm com o núcleo. Por isso o estado do agente entra no backup.
- **Versões:** o `active.json` e o inventário confirmado no banco (`/platform/updates/`, "Inventário confirmado no banco") dizem a versão do núcleo e de cada app em cada geração. Restaure banco e código da mesma geração.
- **Segredos de uma app:** uma app guarda segredos cifrados com os chaveiros do núcleo (os de integrações, por exemplo). Se ela usa um serviço externo com credencial própria, a especificação dela (`documentation_root`) diz qual: acrescente essa credencial ao cofre e à [tabela de segredos](#segredos).
- **Restaurar uma app só não existe:** o banco é um só. Para recuperar os dados de uma app num ponto do passado, restaure o conjunto de backup numa instância à parte e traga os dados pela API de serviços ou pela exportação CSV ([transferência de dados](data-transfer.md)).

## Segredos

Cada segredo abaixo precisa estar no cofre, com o nome, o ambiente e a data da última troca. **Em negrito, os que tornam dados ilegíveis se forem perdidos**: sem eles, um backup restaurado não abre por inteiro, e não há como recuperar o conteúdo cifrado.

| Segredo | Onde vive | Ambiente | Se for perdido | Como regenerar |
|---|---|---|---|---|
| **Chave mestra da instância** (`/instance/keys/master.key`) | Volume `instance` | Sandbox (e toda instância sem modo cluster) | Os segredos do SQLite da instância ficam ilegíveis: a instância não sobe | Não se regenera: restaure o volume, ou reconfigure a instância a partir da [configuração exportada](#configuração-da-instância) |
| **`MFA_ENCRYPTION_KEYS`** | Configuração da instância | Os dois | Os autenticadores TOTP **e os códigos de recuperação** ficam ilegíveis: ninguém com MFA consegue entrar | Chave nova; o operador apaga os autenticadores (tabela `mfa_authenticator`, com a owner) e cada pessoa cadastra o MFA de novo no próximo acesso |
| **`IDENTITY_PROVIDER_ENCRYPTION_KEYS`** | Configuração da instância | Os dois | Os segredos dos provedores de SSO (client secret, chaves SAML) ficam ilegíveis | Chave nova e reconfiguração de cada provedor |
| **`INTEGRATION_ENCRYPTION_KEYS`** | Configuração da instância | Os dois | Os segredos dos webhooks e das integrações (e os de apps que usam esse chaveiro) ficam ilegíveis | Chave nova; recriar os segredos de cada webhook e integração |
| **`BILLING_ENCRYPTION_KEYS`** | Configuração da instância | Os dois, com cobrança ligada | As credenciais do provedor de pagamento ficam ilegíveis | Chave nova e as credenciais do provedor de novo |
| **Chave de cifra dos backups** (restic, borg, age) e a do repositório do PITR (pgBackRest, WAL-G) | Cofre | Os dois | Nenhum backup cifrado abre | Não se regenera |
| `DJANGO_SECRET_KEY` | Configuração da instância | Os dois | Todas as sessões caem e os links assinados (redefinição de senha, convites) deixam de valer; os dados continuam | Chave nova; todos entram de novo |
| `SKELETON_RUNTIME_DB_PASSWORD` (e dentro de `DATABASE_URL`) | Variável do Coolify ou do nó; configuração da instância | Os dois | A aplicação não conecta | `ALTER ROLE skeleton_runtime PASSWORD ...` com o administrador do banco e a URL nova na configuração |
| `SKELETON_OWNER_DB_PASSWORD` | Variável do Coolify ou do candidato; `agent_private/migration.env` | Os dois | O agente não migra | `ALTER ROLE skeleton_owner ...`, a variável nova e o `migration.env` apagado para o agente regravar na próxima partida |
| `SKELETON_AGENT_DB_PASSWORD` | Variável dos seguidores; `coordination.env` | Produção | Os seguidores não coordenam | `ALTER ROLE skeleton_agent ...` e a variável nova |
| `POSTGRES_PASSWORD` | Variável do Coolify; `.env` do servidor de dados | Os dois | Perde-se o acesso de administrador pela rede | Pelo socket local do container (`psql -U postgres`), `ALTER ROLE postgres ...` |
| `REDIS_PASSWORD` (e dentro de `CACHE_URL`) | Variável; configuração da instância | Os dois | O cache não conecta (login e MFA param) | Senha nova no Redis e na `CACHE_URL` |
| Senha do SMTP (dentro de `EMAIL_URL`) | Configuração da instância | Os dois | E-mails param | No provedor de e-mail |
| Credenciais do S3 e o nome do bucket | Ambiente dos nós | Produção (ou com `files_backend=s3`) | Os anexos ficam inacessíveis até trocar a credencial | No provedor do armazenamento |
| `IDP_OIDC_PRIVATE_KEY`/`IDP_OIDC_PRIVATE_KEYS` (ou os arquivos `_FILE`) | Ambiente ou arquivo privado | Os dois, com clientes nativos | Os tokens emitidos aos clientes nativos deixam de valer | Chave nova; os clientes entram de novo |
| Chave privada VAPID (`WEBPUSH_VAPID_PRIVATE_KEY_FILE`) | Arquivo privado | Com web push | As inscrições de push deixam de valer | Chave nova; cada pessoa permite o push de novo |
| `SKELETON_METRICS_TOKEN`, `SKELETON_SENTRY_DSN` | Configuração da instância | Opcionais | Métricas e relato de erros param | Novos valores |
| Segredo do canal do cluster (`cluster keygen`) | Arquivo `secret` de cada nó | Produção | Os nós não conversam | `cluster keygen` e o arquivo novo em todos os nós |
| Chave da CA privada do cluster (`ca.key`) | Fora dos nós, no cofre | Produção | Não se emite certificado para um nó novo | CA nova e certificados novos para todos os nós |
| Chave TLS do servidor de dados (`server.key` de `DATA_TLS_DIR`) | Servidor de dados | Produção | O PostgreSQL e o PgBouncer não sobem com TLS | Certificado novo; nos nós com `sslmode=verify-full`, a CA nova |
| Token do GitHub de uma fonte privada (`github.token_file` do `agent.json`) | Arquivo privado do agente | Com fonte privada | O agente não consulta a fonte | Token novo no GitHub |
| Chave pública de um publicador próprio (`SKELETON_AGENT_PUBLISHER_KEY`) | Imagem ou arquivo montado | Fork com publicador próprio | O agente recusa os pacotes | Não é segredo, mas guarde-a: sem ela nenhum pacote do fork instala |
| Variáveis do recurso no Coolify | Coolify | Sandbox | Um recurso novo não reabre os volumes restaurados | Do cofre |

Regras do cofre:

- **Chaveiros guardam todas as chaves em uso.** `MFA_ENCRYPTION_KEYS` e os outros chaveiros aceitam várias chaves: a primeira cifra e as outras ainda decifram dados antigos. Guarde o valor completo, não só a primeira chave, e nunca descarte uma chave antiga enquanto houver dado cifrado com ela.
- **Senhas que vão em URL** usam só letras e dígitos (`openssl rand -hex 32`), como pede o [sandbox](deployment.md#sandbox-no-coolify-369).
- **Troca:** ao trocar um segredo, grave o novo no cofre antes de aplicá-lo, e mantenha o antigo até o próximo backup sair com o novo.

### Configuração da instância

No sandbox (e em toda instância sem modo cluster), a configuração mora no SQLite da instância, cifrada pela chave mestra. Exporte-a para o cofre depois do setup e a cada mudança em `/platform/instance/`: o arquivo exportado traz todas as chaves com os segredos decifrados, e com ele uma instância nova sobe sem o SQLite e sem a chave mestra. Na produção, esse arquivo é a própria configuração (`SKELETON_INSTANCE_ENV_FILE`).

```sh
# No host do sandbox. O web roda o release ativo; o arquivo nasce 0600 e nunca vai ao terminal.
docker exec web-<recurso> sh -c 'cd /releases/current && SKELETON_PROJECT_ROOT=$PWD \
  SKELETON_DEPLOYMENT_INVENTORY=$PWD/inventory.json PYTHONPATH=$PWD \
  exec .venv/bin/python manage.py instance export-env --output /instance/instance-export.env'
docker cp web-<recurso>:/instance/instance-export.env ./instance-export.env
docker exec web-<recurso> rm /instance/instance-export.env
# Guarde ./instance-export.env no cofre e apague a cópia local.
```

**Resultado esperado:** um arquivo `CHAVE="valor"` com todas as chaves gravadas. **Se falhar:** `export_target_exists` diz que o arquivo já existe (apague-o antes); `instance_cluster_mode` diz que a instância já está em modo cluster (a configuração já vem do arquivo do ambiente).

## Backup do sandbox

Uma vez por dia, num horário de pouco uso, com o recurso parado por poucos minutos. Antes, confira que não há atualização em curso (a tela `/platform/updates/` sem operação aberta). O script roda no host do Coolify, como root, com o identificador do recurso (o sufixo dos nomes dos containers e volumes):

```sh
#!/bin/sh
# Backup diário do sandbox. RECURSO: o identificador do recurso no Coolify.
set -eu
RECURSO=<identificador>
DESTINO=/srv/backups/skeleton-sandbox/$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$DESTINO" && chmod 700 "$DESTINO"
# 1. Para quem escreve; o PostgreSQL continua no ar para o dump.
docker stop "web-$RECURSO" "worker-$RECURSO" "scheduler-$RECURSO" "agent-$RECURSO"
# 2. Os dois schemas da instância (a extensão pg_trgm, em public, vem do bootstrap), em formato custom.
docker exec "postgres-$RECURSO" pg_dump -U postgres -d skeleton --schema=skeleton --schema=skeleton_deploy \
  --format=custom > "$DESTINO/skeleton.dump"
# 3. Os volumes com estado, no mesmo momento.
for volume in instance files agent_state agent_private releases; do
  docker run --rm -v "${RECURSO}_${volume}:/data:ro" -v "$DESTINO:/backup" alpine \
    tar -C /data -czf "/backup/$volume.tar.gz" .
done
# 4. Volta ao ar: o agente primeiro.
docker start "agent-$RECURSO"
docker start "web-$RECURSO" "worker-$RECURSO" "scheduler-$RECURSO"
# 5. Cifre e copie para fora do host (restic, borg ou rclone crypt) e aplique a retenção.
```

Guarde junto de cada conjunto a versão do núcleo e das apps (a tela `/platform/updates/`, "Versões instaladas", ou o `inventory` do agente): ela diz com que código o conjunto foi feito.

**Resultado esperado:** um diretório por dia com `skeleton.dump` e um `.tar.gz` por volume, copiado e cifrado fora do host. **Se falhar:** o recurso pode ficar parado; rode o passo 4 à mão e confira os containers no Coolify. Um `pg_dump` com erro não deixa um conjunto válido: apague o diretório do dia e repita.

## Backup da produção

| O quê | Como | Frequência e retenção |
|---|---|---|
| Banco | Backup contínuo do PostgreSQL de cada instância (pgBackRest ou WAL-G, ou o serviço gerenciado): backup base e arquivamento do WAL fora do servidor de dados. No agente, `"backup": {"mode": "external", "acknowledge_continuous_backup": true}` marca um ponto de restauração antes de cada migration ([backup externo](deployment.md#backup-crash-e-recuperação)) | Base diária ou semanal; WAL contínuo; 14 a 30 dias |
| Banco (segunda linha) | Dump lógico no servidor de dados, dos schemas `skeleton` e `skeleton_deploy`: `docker exec <projeto>-postgres-1 pg_dump -U postgres -d skeleton --schema=skeleton --schema=skeleton_deploy --format=custom` | Diário; 7 diários e 4 semanais |
| Papéis do banco | `pg_dumpall -U postgres --globals-only --no-role-passwords`: as senhas vêm do cofre, e o `deploy/postgres/bootstrap.sql` recria as roles | Quando mudar |
| Anexos | Versionamento do bucket (com retenção das versões apagadas) e replicação para outro bucket ou região; no volume compartilhado, snapshot do sistema de arquivos | Contínuo; 30 dias de versões |
| Configuração da instância | O arquivo de `SKELETON_INSTANCE_ENV_FILE`, no cofre, versionado | A cada mudança |
| Estado do agente | Snapshot ou cópia do diretório de `SKELETON_AGENT_STATE_SOURCE` (journal, `uploads/` com os pacotes do core e das apps, planos, `sources.json`) | Diário e depois de cada operação; 30 dias |
| Releases do primário | `tar` do volume `releases` do nó que `cluster status` aponta como primário | Depois de cada operação bem-sucedida; os 2 últimos |
| Artefatos do cluster e do servidor de dados | CA, certificados, segredo do canal, `server.crt`/`server.key`, os `.env` dos projetos do servidor de dados | No cofre, a cada troca |

**Coerência:** o estado do agente e os releases do primário são copiados no fim de cada operação; o banco tem PITR contínuo. Para restaurar o conjunto inteiro, escolha no PITR o mesmo momento do estado do agente usado (ou o ponto `skeleton-<operação>` daquela operação).

## Recuperação

### Conferência depois de qualquer recuperação

1. `/health/ready/` responde 200 em todos os nós e `/health/release/` responde a mesma geração em todos.
2. Em `/platform/updates/`, "Versões instaladas" e "Inventário confirmado no banco" mostram as mesmas versões do núcleo e de cada app.
3. `manage.py check --deploy` e `manage.py check_skeleton_schema` sem erros, rodados no release ativo.
4. `manage.py check_jobs --max-lag 900` sem atraso depois de alguns minutos (o worker e o scheduler voltaram).
5. Uma pessoa entra com MFA; um anexo antigo abre; cada app instalada abre a tela inicial dela numa organização onde está ativa.
6. Num cluster, `cluster status` mostra um primário e todos os nós vivos.

Se um item falhar, mantenha o site fora do ar no proxy (Coolify ou balanceador) e volte ao passo de restauração; não troque a configuração para passar no check.

### Sandbox: perda do host

1. VPS nova com o Coolify; DNS do host apontando para ela.
2. Crie o recurso como em [criar o recurso](deployment.md#criar-o-recurso), com **os mesmos valores** das variáveis (do cofre): as senhas do banco precisam ser as do backup, porque a configuração restaurada as usa. Defina `SKELETON_CORE_VERSION` com a versão do núcleo do conjunto de backup, para a primeira partida não instalar outra.
3. *Deploy* uma vez e espere o `agent` ficar saudável: ele cria os volumes e um banco novo, que serão substituídos.
4. Pare `web`, `worker`, `scheduler` e `agent` (o PostgreSQL continua no ar).
5. Restaure cada volume do conjunto:

   ```sh
   for volume in instance files agent_state agent_private releases; do
     docker run --rm -v "${RECURSO}_${volume}:/data" -v "$CONJUNTO:/backup:ro" alpine \
       sh -c "find /data -mindepth 1 -delete && tar -C /data -xzf /backup/$volume.tar.gz"
   done
   ```

6. Restaure o banco, trocando os schemas criados no passo 3 pelos do backup:

   ```sh
   docker exec "postgres-$RECURSO" psql -U postgres -d skeleton -v ON_ERROR_STOP=1 \
     -c 'DROP SCHEMA IF EXISTS skeleton CASCADE' -c 'DROP SCHEMA IF EXISTS skeleton_deploy CASCADE'
   docker exec -i "postgres-$RECURSO" pg_restore -U postgres -d skeleton --exit-on-error < "$CONJUNTO/skeleton.dump"
   ```

7. Suba o `agent`, depois `web`, `worker` e `scheduler`, e faça a [conferência](#conferência-depois-de-qualquer-recuperação).

**Resultado esperado:** a instância no estado do backup, com as mesmas versões do núcleo e das apps, e as pessoas entrando com as senhas e o MFA de antes. **Se falhar:** com `active_pointer_conflict` no agente, o `agent_state` e o `releases` não são do mesmo conjunto: restaure os dois do mesmo diretório. Com o setup aparecendo em vez da instância, o volume `instance` não foi restaurado (ou a chave mestra não confere): restaure-o, ou use a [configuração exportada](#configuração-da-instância).

### Sandbox: banco corrompido ou dados apagados por engano

O host está bem. Pare `web`, `worker`, `scheduler` e `agent`, restaure o banco (passo 6 acima) e, se o conjunto for de antes da última atualização, também `agent_state`, `releases` e `files` do mesmo conjunto (passo 5). Suba e confira.

**Resultado esperado:** os dados como no backup; o que foi escrito depois dele se perde. **Se falhar:** `check_skeleton_schema` recusando o schema indica banco e código de versões diferentes: use o `agent_state` e o `releases` do mesmo conjunto do dump.

### Estado do agente perdido sem backup (último recurso)

Sem `agent_state` e `releases`, o agente da primeira partida instala o núcleo de novo sobre um banco já migrado, caminho que os testes não cobrem ([sandbox criado antes do agente](deployment.md#atualizar-o-sandbox)). Se não houver outra saída: defina `SKELETON_CORE_VERSION` com a versão exata do núcleo que o banco tem, faça o *Deploy* e reinstale cada app **na mesma versão** de antes (pela fonte, ou enviando de novo o pacote `.skapp`), antes de abrir o site. As apps cujo pacote só existia em `uploads/` precisam do pacote original do publicador.

### Chave mestra perdida (sandbox)

O SQLite da instância não abre. Restaure o volume `instance` do último conjunto. Sem ele, apague o SQLite e a chave, e grave a configuração exportada do cofre na instância nova (`manage.py instance set` para cada chave, ou o setup com os mesmos valores); com os mesmos `DJANGO_SECRET_KEY` e chaveiros, os dados e as sessões continuam valendo.

### Chaveiro de criptografia perdido

Não há recuperação do conteúdo cifrado com ele. Ponha uma chave nova no chaveiro e refaça o que ele protegia (MFA de cada pessoa, segredos do SSO, dos webhooks, das integrações e da cobrança), como na [tabela](#segredos). Sem o chaveiro do MFA, nem os códigos de recuperação abrem: com o site fora do ar, apague os autenticadores com a owner (`DELETE FROM skeleton.mfa_authenticator`), suba com a chave nova e avise as pessoas de que o MFA será cadastrado de novo no próximo acesso (o de administradores é obrigatório). Os cadastros anteriores não voltam.

### Produção: perda de um nó ou do primário

Nada a restaurar: o nó é substituído pelo roteiro [adicionar um nó](high-availability.md#roteiros), e o primário perdido é assumido por outro candidato ([perda do primário](high-availability.md#roteiros)).

### Produção: banco perdido ou corrompido

1. Tire o site do ar no balanceador ([manutenção pela infraestrutura](maintenance.md)) e pare `web`, `worker`, `scheduler` e `agent` em todos os nós.
2. Se o servidor de dados se perdeu, suba o projeto da instância num servidor novo com o mesmo `.env` (do cofre) e os mesmos certificados.
3. Restaure o banco para o momento escolhido: pelo PITR (pgBackRest ou WAL-G, até antes do incidente, ou até o ponto `skeleton-<operação>` de uma atualização), ou pelo dump lógico (`DROP SCHEMA` e `pg_restore`, como no sandbox).
4. Se o momento for anterior à última operação do agente, restaure também o `storage_root` e os releases do primário do mesmo momento (passos da perda total, abaixo). Se o incidente foi uma atualização que falhou, use o caminho do próprio agente: `recover <operação> restore`, ou o PITR seguido de `recover <operação> abort --external-restored` ([backup externo](deployment.md#backup-crash-e-recuperação)).
5. Suba os agentes (o candidato que deve ser o primário primeiro), depois os serviços de aplicação, e faça a [conferência](#conferência-depois-de-qualquer-recuperação). Rode `cluster forget <nó>` para os nós que não existem mais.

**Resultado esperado:** um primário, todos os nós na mesma geração do banco, o site de volta. **Se falhar:** nós em gerações diferentes ou `check_skeleton_schema` recusando dizem que banco e releases não são do mesmo momento: escolha outro ponto do PITR ou outro estado do agente, sempre os dois juntos.

### Produção: `storage_root` perdido

O site continua no ar (servir tráfego não depende do agente). Restaure o diretório do último snapshot com os agentes parados e suba-os de novo. Sem backup, as atualizações e as instalações de apps ficam impedidas até o operador reenviar os pacotes das apps instaladas e recriar as fontes; não tente uma atualização antes disso.

### Produção: perda total (nós e servidor de dados)

1. Servidor de dados novo, com o banco restaurado (acima).
2. Num nó candidato novo, com as mesmas variáveis do candidato de antes: restaure o `storage_root` e o volume `releases` do primário do mesmo momento, nos mesmos caminhos, com `SKELETON_CLUSTER_PREFERRED=true`, e suba só esse nó.
3. Confira esse nó (ele assume como primário e publica o release do cluster) e então suba os outros nós: eles instalam o release do primário e só depois iniciam os serviços.
4. Os anexos voltam do bucket (ou do volume) e a configuração, do cofre.

**Resultado esperado:** o cluster de volta na geração do backup. **Se falhar:** sem os releases do primário, é o [último recurso](#estado-do-agente-perdido-sem-backup-último-recurso), com a versão do núcleo e de cada app fixada.

### Anexos perdidos

No bucket, restaure as versões anteriores dos objetos apagados ou alterados (versionamento); no volume, o snapshot. Um anexo cujo arquivo falta responde 404, sem afetar o resto.

## Teste periódico de restauração

A cada trimestre, e depois de cada mudança neste plano:

1. Restaure o conjunto mais recente do sandbox (ou um ponto do PITR da produção) num servidor à parte, sem DNS público, seguindo o roteiro de perda total.
2. Faça a [conferência](#conferência-depois-de-qualquer-recuperação).
3. Registre a data, o conjunto usado, o tempo gasto (o RTO real), a idade dos dados restaurados (o RPO real) e o que precisou ser corrigido neste guia.
4. Apague o ambiente de teste: ele tem dados e segredos reais.
