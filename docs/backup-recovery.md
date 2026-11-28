# Backup e recuperação de desastre

Este guia define o que precisa ser salvo para recuperar uma instância do Skeleton após um desastre — perda de servidor, banco de dados, anexos, estado do agente ou chaves — e descreve os procedimentos de restauração tanto no **sandbox** (um único host no Coolify usando `compose.sandbox.yaml`) quanto na **produção** (nós de aplicação em cluster com `compose.yaml` e servidor de dados em `deploy/data-server/compose.yaml`, conforme [alta disponibilidade](high-availability.md)). As orientações cobrem instâncias com várias apps instaladas.

Este plano não substitui o backup automático que o agente executa antes de cada migration ([backup, crash e recuperação](deployment.md#backup-crash-e-recuperação)): aquele existe para reverter uma atualização malsucedida e fica no mesmo servidor. Aqui trata-se do backup de desastre, armazenado fora do servidor.

## Princípios

1. **Banco e código na mesma versão.** O banco reflete o histórico exato de migrations do núcleo e das apps. Restaurar o banco de um ponto no tempo exige executar o núcleo e as apps na versão correspondente: o release ativo e os pacotes das apps devem integrar o mesmo conjunto de backup, ou o operador precisa reinstalar exatamente as mesmas versões. Combinar banco antigo com código novo — ou o inverso — faz `check_skeleton_schema` recusar a partida.
2. **Coerência do conjunto.** Banco, anexos, configuração e estado do agente precisam corresponder ao mesmo instante. No sandbox, os processos escritores são parados durante a cópia. Na produção, o ponto de restauração do banco, a versão dos anexos e o `storage_root` do agente são alinhados para o mesmo momento.
3. **Segredos separados dos dados.** Chaveiros de criptografia, chave mestra e senhas ficam em um cofre de segredos (gerenciador de senhas da equipe ou cofre do provedor), com acesso de pelo menos duas pessoas. Um backup sem as chaves é ilegível; as chaves sem o backup não reconstroem dados. Nenhum valor real de segredo aparece neste guia nem no repositório.
4. **Cópia externa, cifrada e com retenção.** Faça cópia diária para outro local (outro provedor ou região), cifrada antes de sair do servidor (restic, borg, rclone crypt ou age), com política de retenção definida (por exemplo, 7 diários, 4 semanais e 6 mensais) e imutabilidade (object lock) sempre que disponível.
5. **Backup não testado não conta.** O [teste de restauração](#teste-periódico-de-restauração) faz parte obrigatória do plano.

Metas de referência (ajuste conforme o SLA do serviço):

| Ambiente | Perda máxima de dados (RPO) | Tempo para voltar (RTO) |
|---|---|---|
| Sandbox | 24 h (backup diário) | 4 h |
| Produção | 5 min (PITR do PostgreSQL) | 1 a 2 h para o banco; minutos para substituir um nó |

## Onde o estado mora

### Sandbox (`compose.sandbox.yaml` no Coolify)

Todos os componentes vivem em um único host. No Coolify, o nome efetivo de cada volume traz o identificador do recurso como prefixo (confira com `docker volume ls`).

| Volume | O que guarda | Backup |
|---|---|---|
| `postgres_data` | Banco `skeleton`: schemas `skeleton` (núcleo, apps, auditoria, sessões, jobs e estado de deployment confirmado) e `skeleton_deploy` (vazio fora de cluster) | **Sim**, por dump lógico (`pg_dump` dos dois schemas); cópia física do diretório só é válida com o PostgreSQL parado e na mesma versão principal (16). Roles, senhas e a extensão `pg_trgm` vêm do bootstrap e das variáveis |
| `instance` | SQLite da configuração da instância (com segredos cifrados) e a chave mestra `/instance/keys/master.key` | **Sim**; guarde também a chave mestra e a configuração exportada no cofre ([abaixo](#configuração-da-instância)) |
| `files` | Anexos em `/files`: arquivos de registros e apps, exportações CSV, logos, ícones e pacotes de tema | **Sim** |
| `agent_state` | Estado do agente em `/agent/state`: `active.json` (release ativo e geração), journal de operações, planos, **pacotes enviados** (core e apps em `uploads/`), fontes (`sources.json`), backups de atualização e logs | **Sim** |
| `releases` | Releases montados (virtualenv do núcleo e das apps de cada geração) e ponteiro `current` | **Sim** (volumoso, porém permite restaurar `active.json` sem reconstruir ambientes) |
| `agent_private` | `agent.json` e arquivos de ambiente do agente (incluindo a credencial owner), gerados na primeira partida pelas variáveis do Coolify | Recomendado (cifrado); regenerável |
| `agent_code` | Versões do próprio agente instaladas via auto-atualização (`agent_root`) | Não: na ausência dele, o container usa o agente da imagem até a próxima atualização pela tela |
| `control`, `control_state` | Socket, token de controle e estado do inicializador | Não: recriados automaticamente pelo agente |
| Redis (sem volume) | Cache, rate limits, antirreuso do TOTP e códigos OIDC | Não: dados efêmeros |

### Produção (cluster)

Os nós de aplicação são descartáveis: todo o estado persistente fica no servidor de dados, no armazenamento de arquivos, no cofre de segredos e no `storage_root` compartilhado entre os candidatos.

| Peça | Onde | Backup |
|---|---|---|
| Banco da instância | PostgreSQL do projeto da instância no servidor de dados (`deploy/data-server/compose.yaml`) | **Sim**: backup contínuo (PITR) e dump lógico diário |
| Anexos | Bucket S3 (`SKELETON_FILES_BACKEND=s3`) ou volume compartilhado (`SKELETON_FILES_SHARED=true`) | **Sim**: versionamento do bucket e replicação para outro bucket ou região, ou snapshot do volume |
| Configuração da instância | Arquivo apontado por `SKELETON_INSTANCE_ENV_FILE` (gerado por `instance export-env`), igual em todos os nós | **Sim**, no cofre, a cada alteração |
| Estado do agente | `storage_root`, diretório de `SKELETON_AGENT_STATE_SOURCE` compartilhado entre os candidatos | **Sim**, diário e ao final de cada operação |
| Releases | Volume `releases` de cada nó | **Sim, apenas o do primário**, após cada operação bem-sucedida (para casos de perda total); os demais nós sincronizam a partir dele |
| `agent_private` de cada nó | Gerado na partida pelas variáveis do nó | Não (regenerável); mantenha as variáveis no cofre |
| Artefatos do cluster | CA privada, certificados dos nós, segredo do canal e certificados TLS do servidor de dados | **Sim**, no cofre |
| Redis | Servidor de dados | Não |
| Nós de app | Hosts | Não: nós perdidos são substituídos ([adicionar um nó](high-availability.md#roteiros)) |

## Apps instaladas

Não existe backup separado por app: tudo o que as apps produzem passa pela infraestrutura do núcleo.

- **Dados:** as tabelas de cada app residem no schema `skeleton`, junto às do núcleo, entrando no mesmo dump e no mesmo PITR — inclusive tabelas mantidas de apps desinstaladas com preservação de dados (`schema_retained`).
- **Configuração e ativação por organização:** ficam no banco (`AppInstallation`), junto aos demais registros.
- **Arquivos:** usam o armazenamento do núcleo (`files` ou bucket S3).
- **Código:** cada app vem de um pacote `.skapp` assinado. Pacotes enviados manualmente existem apenas em `storage_root/uploads/`; pacotes oriundos de fontes (como GitHub) podem ser baixados novamente enquanto o publicador mantiver aquela versão. Apps first-party (`skeleton/contrib`, `SKELETON_BUNDLED_APPS`) acompanham o núcleo. Por isso, o estado do agente precisa constar no backup.
- **Versões:** `active.json` e o inventário confirmado no banco (`/platform/updates/`, seção "Inventário confirmado no banco") registram as versões exatas do núcleo e de cada app por geração. Restaure sempre banco e código da mesma geração.
- **Segredos de uma app:** segredos gravados pela app usam os chaveiros do núcleo (como o de integrações). Caso a app utilize credenciais próprias de serviços externos, consulte sua documentação (`documentation_root`) e inclua essas credenciais no cofre e na [tabela de segredos](#segredos).
- **Restauração isolada de uma app:** como o banco é único, não há restauração seletiva direta. Para recuperar dados passados de uma app específica, restaure o conjunto de backup em uma instância separada e extraia os dados pela API de serviços ou por exportação CSV ([transferência de dados](data-transfer.md)).

## Segredos

Mantenha todos os segredos abaixo no cofre, identificados por nome, ambiente e data da última troca. **Itens em negrito tornam dados permanentemente ilegíveis em caso de perda**: sem eles, um backup restaurado não abre por completo e o conteúdo cifrado não pode ser recuperado.

| Segredo | Onde vive | Ambiente | Se for perdido | Como regenerar |
|---|---|---|---|---|
| **Chave mestra da instância** (`/instance/keys/master.key`) | Volume `instance` | Sandbox (e toda instância fora do modo cluster) | Os segredos do SQLite ficam ilegíveis e a instância não sobe | Não se regenera: restaure o volume ou reconfigure a instância usando a [configuração exportada](#configuração-da-instância) |
| **`MFA_ENCRYPTION_KEYS`** | Configuração da instância | Ambos | Autenticadores TOTP **e códigos de recuperação** ficam ilegíveis; usuários com MFA não conseguem entrar | Gere nova chave, apague os autenticadores (`mfa_authenticator`, com a owner) e instrua cada pessoa a recadastrar o MFA no próximo login |
| **`IDENTITY_PROVIDER_ENCRYPTION_KEYS`** | Configuração da instância | Ambos | Segredos de provedores SSO (client secrets, chaves SAML) ficam ilegíveis | Gere nova chave e reconfigure cada provedor |
| **`INTEGRATION_ENCRYPTION_KEYS`** | Configuração da instância | Ambos | Segredos de webhooks, integrações e apps que usam esse chaveiro ficam ilegíveis | Gere nova chave e recrie os segredos de cada webhook e integração |
| **`BILLING_ENCRYPTION_KEYS`** | Configuração da instância | Ambos, com cobrança ligada | Credenciais dos provedores de pagamento ficam ilegíveis | Gere nova chave e recadastre as credenciais dos provedores |
| **Chave de cifra dos backups** (restic, borg, age) e do repositório PITR (pgBackRest, WAL-G) | Cofre | Ambos | Nenhum backup cifrado pode ser aberto | Não se regenera |
| `DJANGO_SECRET_KEY` | Configuração da instância | Ambos | Todas as sessões caem e links assinados (redefinição de senha, convites) expiram; dados permanecem intactos | Gere nova chave; usuários fazem login novamente |
| `SKELETON_RUNTIME_DB_PASSWORD` (e em `DATABASE_URL`) | Variável do Coolify ou do nó; configuração da instância | Ambos | A aplicação perde acesso ao banco | Execute `ALTER ROLE skeleton_runtime PASSWORD ...` como administrador do banco e atualize a URL na configuração |
| `SKELETON_OWNER_DB_PASSWORD` | Variável do Coolify ou do candidato; `agent_private/migration.env` | Ambos | O agente não consegue migrar | Execute `ALTER ROLE skeleton_owner ...`, atualize a variável e apague `migration.env` para o agente regravá-lo na partida |
| `SKELETON_AGENT_DB_PASSWORD` | Variável dos seguidores; `coordination.env` | Produção | Seguidores deixam de coordenar | Execute `ALTER ROLE skeleton_agent ...` e atualize a variável |
| `POSTGRES_PASSWORD` | Variável do Coolify; `.env` do servidor de dados | Ambos | Perde-se acesso administrativo pela rede | Pelo socket local do container (`psql -U postgres`), execute `ALTER ROLE postgres ...` |
| `REDIS_PASSWORD` (e em `CACHE_URL`) | Variável; configuração da instância | Ambos | O cache desconecta (login e MFA param) | Defina nova senha no Redis e atualize `CACHE_URL` |
| Senha do SMTP (em `EMAIL_URL`) | Configuração da instância | Ambos | Envio de e-mails para | Reemita no provedor de e-mail |
| Credenciais do S3 e nome do bucket | Ambiente dos nós | Produção (ou com `files_backend=s3`) | Anexos ficam inacessíveis até atualizar a credencial | Reemita no provedor de armazenamento |
| `IDP_OIDC_PRIVATE_KEY` / `IDP_OIDC_PRIVATE_KEYS` (ou arquivos `_FILE`) | Ambiente ou arquivo privado | Ambos, com clientes nativos | Tokens emitidos a clientes nativos perdem validade | Gere nova chave; clientes autenticam novamente |
| Chave privada VAPID (`WEBPUSH_VAPID_PRIVATE_KEY_FILE`) | Arquivo privado | Com web push | Inscrições de push existentes invalidam | Gere nova chave; cada pessoa reativa as notificações |
| `SKELETON_METRICS_TOKEN`, `SKELETON_SENTRY_DSN` | Configuração da instância | Opcionais | Métricas e relato de erros param | Defina novos valores |
| Segredo do canal do cluster (`cluster keygen`) | Arquivo `secret` de cada nó | Produção | Nós deixam de se comunicar | Execute `cluster keygen` e distribua o novo arquivo a todos os nós |
| Chave da CA privada do cluster (`ca.key`) | Fora dos nós, no cofre | Produção | Impossível emitir certificados para novos nós | Crie nova CA e reemita os certificados de todos os nós |
| Chave TLS do servidor de dados (`server.key` em `DATA_TLS_DIR`) | Servidor de dados | Produção | PostgreSQL e PgBouncer falham ao subir com TLS | Emita novo certificado; nos nós com `sslmode=verify-full`, atualize a CA |
| Token do GitHub de fonte privada (`github.token_file` no `agent.json`) | Arquivo privado do agente | Com fonte privada | O agente deixa de consultar a fonte | Gere novo token no GitHub |
| Chave pública de publicador próprio (`SKELETON_AGENT_PUBLISHER_KEY`) | Imagem ou arquivo montado | Fork com publicador próprio | O agente recusa pacotes do fork | Não é segredo, mas guarde-a: sem ela nenhum pacote do fork pode ser instalado |
| Variáveis do recurso no Coolify | Coolify | Sandbox | Um recurso recriado não consegue reabrir os volumes restaurados | Restaure a partir do cofre |

Boas práticas para o cofre:

- **Preserve todas as chaves de cada chaveiro.** `MFA_ENCRYPTION_KEYS` e os demais chaveiros aceitam múltiplas chaves: a primeira cifra novos dados e as antigas continuam decifrando registros anteriores. Salve sempre a lista inteira e nunca descarte uma chave antiga enquanto houver dados cifrados por ela.
- **Senhas embutidas em URLs** devem conter apenas letras e dígitos (`openssl rand -hex 32`), conforme especificado no [sandbox](deployment.md#sandbox-no-coolify-369).
- **Rotação segura:** salve o novo segredo no cofre antes de aplicá-lo e conserve o antigo até confirmar que o próximo backup foi gerado com o novo valor.

### Configuração da instância

No sandbox (e em qualquer instalação fora do modo cluster), a configuração vive no SQLite da instância cifrado pela chave mestra. Exporte-a para o cofre logo após o setup e após cada alteração em `/platform/instance/`: o arquivo resultante traz todas as chaves com os segredos decifrados, permitindo subir uma instância nova mesmo sem o SQLite nem a chave mestra. Na produção, esse mesmo formato corresponde diretamente ao arquivo `SKELETON_INSTANCE_ENV_FILE`.

```sh
# No host do sandbox. O web roda o release ativo; o arquivo nasce 0600 e nunca vai ao terminal.
docker exec web-<recurso> sh -c 'cd /releases/current && SKELETON_PROJECT_ROOT=$PWD \
  SKELETON_DEPLOYMENT_INVENTORY=$PWD/inventory.json PYTHONPATH=$PWD \
  exec .venv/bin/python manage.py instance export-env --output /instance/instance-export.env'
docker cp web-<recurso>:/instance/instance-export.env ./instance-export.env
docker exec web-<recurso> rm /instance/instance-export.env
# Guarde ./instance-export.env no cofre e apague a cópia local.
```

*Resultado esperado:* arquivo no formato `CHAVE="valor"` contendo todas as chaves gravadas. *Se falhar:* `export_target_exists` indica que o arquivo de destino já existe (remova-o antes); `instance_cluster_mode` indica que a instância já opera em modo cluster e lê a configuração diretamente do ambiente.

## Backup do sandbox

Execute uma vez por dia em horário de baixo movimento, parando o recurso por alguns minutos. Antes de iniciar, confirme em `/platform/updates/` que não há atualização em andamento. O script roda no host do Coolify, como root, informando o identificador do recurso (o sufixo usado nos nomes dos containers e volumes):

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

Anote junto de cada conjunto as versões do núcleo e das apps (consultadas em `/platform/updates/`, seção "Versões instaladas", ou via `inventory` do agente) para saber exatamente qual código acompanha aquele dump.

*Resultado esperado:* um diretório datado contendo `skeleton.dump` e um `.tar.gz` por volume, cifrado e enviado para fora do host. *Se falhar:* o recurso pode permanecer parado; execute o passo 4 manualmente e verifique os containers no Coolify. Se `pg_dump` retornar erro, descarte o diretório incompleto e repita o backup.

## Backup da produção

| O quê | Como | Frequência e retenção |
|---|---|---|
| Banco | Backup contínuo do PostgreSQL de cada instância (pgBackRest, WAL-G ou serviço gerenciado): backup base + arquivamento contínuo de WAL fora do servidor de dados. No agente, configure `"backup": {"mode": "external", "acknowledge_continuous_backup": true}` para registrar um ponto de restauração antes de cada migration ([backup externo](deployment.md#backup-crash-e-recuperação)) | Base diária ou semanal; WAL contínuo; 14 a 30 dias |
| Banco (segunda linha) | Dump lógico no servidor de dados dos schemas `skeleton` e `skeleton_deploy`: `docker exec <projeto>-postgres-1 pg_dump -U postgres -d skeleton --schema=skeleton --schema=skeleton_deploy --format=custom` | Diário; 7 diários e 4 semanais |
| Papéis do banco | `pg_dumpall -U postgres --globals-only --no-role-passwords` (as senhas ficam no cofre e `deploy/postgres/bootstrap.sql` recria as roles) | Sempre que houver mudança |
| Anexos | Versionamento do bucket (retendo versões excluídas) com replicação para outro bucket ou região; em volume compartilhado, snapshot do sistema de arquivos | Contínuo; 30 dias de versões |
| Configuração da instância | Arquivo de `SKELETON_INSTANCE_ENV_FILE`, versionado no cofre | A cada alteração |
| Estado do agente | Snapshot ou cópia do diretório `SKELETON_AGENT_STATE_SOURCE` (journal, `uploads/` com pacotes do core e das apps, planos e `sources.json`) | Diário e após cada operação; 30 dias |
| Releases do primário | Arquivo `tar` do volume `releases` do nó indicado como primário em `cluster status` | Após cada operação bem-sucedida; manter os 2 últimos |
| Artefatos do cluster e servidor de dados | CA, certificados, segredo do canal, `server.crt`, `server.key` e arquivos `.env` dos projetos do servidor de dados | No cofre, a cada troca |

**Coerência:** como o estado do agente e os releases do primário são copiados ao término de cada operação e o banco conta com PITR contínuo, para restaurar o conjunto inteiro escolha no PITR o mesmo instante da cópia do estado do agente (ou o ponto de restauração `skeleton-<operação>` daquela operação).

## Recuperação

### Conferência depois de qualquer recuperação

1. `/health/ready/` responde `200` em todos os nós e `/health/release/` informa a mesma geração em todos.
2. Em `/platform/updates/`, "Versões instaladas" e "Inventário confirmado no banco" exibem as mesmas versões do núcleo e de cada app.
3. `manage.py check --deploy` e `manage.py check_skeleton_schema` passam sem erros no release ativo.
4. `manage.py check_jobs --max-lag 900` não reporta atraso após alguns minutos (confirmando que worker e agendador estão operantes).
5. Um usuário entra com MFA; um anexo antigo abre normalmente; cada app instalada carrega sua tela inicial em uma organização onde está ativa.
6. Em cluster, `cluster status` exibe um primário e todos os nós vivos.

Se qualquer item falhar, mantenha o site fechado no proxy (Coolify ou balanceador) e revise a restauração — nunca altere configurações apenas para forçar a passagem de um check.

### Sandbox: perda do host

1. Provisione uma nova VPS com Coolify e aponte o DNS para ela.
2. Crie o recurso seguindo [criar o recurso](deployment.md#criar-o-recurso), informando **exatamente os mesmos valores** das variáveis salvas no cofre (a configuração restaurada depende das mesmas senhas de banco). Defina `SKELETON_CORE_VERSION` com a versão do núcleo daquele conjunto de backup para impedir que a primeira partida instale outra versão.
3. Execute *Deploy* uma vez e aguarde o `agent` ficar saudável — isso cria os volumes e um banco vazio que serão substituídos.
4. Pare `web`, `worker`, `scheduler` e `agent` (mantenha o PostgreSQL em execução).
5. Restaure cada volume a partir do conjunto de backup:

   ```sh
   for volume in instance files agent_state agent_private releases; do
     docker run --rm -v "${RECURSO}_${volume}:/data" -v "$CONJUNTO:/backup:ro" alpine \
       sh -c "find /data -mindepth 1 -delete && tar -C /data -xzf /backup/$volume.tar.gz"
   done
   ```

6. Restaure o banco, substituindo os schemas criados no passo 3 pelos do backup:

   ```sh
   docker exec "postgres-$RECURSO" psql -U postgres -d skeleton -v ON_ERROR_STOP=1 \
     -c 'DROP SCHEMA IF EXISTS skeleton CASCADE' -c 'DROP SCHEMA IF EXISTS skeleton_deploy CASCADE'
   docker exec -i "postgres-$RECURSO" pg_restore -U postgres -d skeleton --exit-on-error < "$CONJUNTO/skeleton.dump"
   ```

7. Inicie o `agent`, depois `web`, `worker` e `scheduler`, e execute a [conferência](#conferência-depois-de-qualquer-recuperação).

*Resultado esperado:* instância restaurada no estado exato do backup, com as mesmas versões de núcleo e apps e usuários entrando com suas senhas e MFA anteriores. *Se falhar:* `active_pointer_conflict` no agente indica que `agent_state` e `releases` vieram de conjuntos diferentes — restaure ambos do mesmo diretório. Se o setup reaparecer no lugar da instância, o volume `instance` não foi restaurado ou a chave mestra diverge — restaure o volume ou aplique a [configuração exportada](#configuração-da-instância).

### Sandbox: banco corrompido ou dados apagados por engano

Com o host intacto, pare `web`, `worker`, `scheduler` e `agent`, restaure o banco (passo 6 acima) e, caso o backup seja anterior à última atualização, restaure também `agent_state`, `releases` e `files` do mesmo conjunto (passo 5). Reinicie os serviços e faça a conferência.

*Resultado esperado:* dados restaurados ao ponto do backup (escritas posteriores são perdidas). *Se falhar:* recusa de `check_skeleton_schema` sinaliza descompasso entre banco e código — restaure `agent_state` e `releases` do mesmo conjunto do dump.

### Estado do agente perdido sem backup (último recurso)

Sem `agent_state` nem `releases`, o agente da primeira partida tentará instalar o núcleo novamente sobre um banco já migrado, cenário não coberto pelos testes ([sandbox criado antes do agente](deployment.md#atualizar-o-sandbox)).

Como último recurso: fixe `SKELETON_CORE_VERSION` na versão exata do núcleo registrada no banco, execute o *Deploy* e reinstale cada app **na mesma versão anterior** (pela fonte ou reenviando o pacote `.skapp`) antes de reabrir o site. Apps cujo pacote existia apenas em `uploads/` exigem o `.skapp` original do publicador.

*Resultado esperado:* "Versões instaladas" e "Inventário confirmado no banco" coincidem em núcleo e apps, e a [conferência](#conferência-depois-de-qualquer-recuperação) conclui sem erros. *Se falhar:* mantenha o site fechado; recusa de `check_skeleton_schema` indica divergência de versão (confira no inventário do banco qual versão de cada app estava instalada e reinstale exatamente aquela).

### Chave mestra perdida (sandbox)

Sem a chave mestra, o SQLite da instância não pode ser aberto. Restaure o volume `instance` do conjunto mais recente. Na ausência desse volume, remova o SQLite e a chave antiga e reaplique a configuração exportada do cofre (usando `manage.py instance set` para cada chave ou refazendo o setup com os mesmos valores): mantendo o mesmo `DJANGO_SECRET_KEY` e os mesmos chaveiros, dados e sessões permanecem válidos.

*Resultado esperado:* `manage.py instance status` sem pendências e instância operando fora do setup. *Se falhar:* se o setup solicitar chaves, algum item da configuração exportada ficou faltando; se também não houver configuração exportada nem backup do volume, siga o procedimento de perda de chaveiro abaixo.

### Chaveiro de criptografia perdido

Conteúdos cifrados por um chaveiro perdido não podem ser recuperados. Cadastre uma nova chave no chaveiro e recrie os recursos protegidos por ele (MFA de usuários, segredos de SSO, webhooks, integrações e cobrança), conforme a [tabela](#segredos).

No caso específico da perda de `MFA_ENCRYPTION_KEYS`, nem os códigos de recuperação abrem: com o site fora do ar, remova os autenticadores usando a role owner (`DELETE FROM skeleton.mfa_authenticator`), suba a instância com a nova chave e avise os usuários de que precisarão recadastrar o MFA no próximo login (obrigatório para administradores). Cadastros antigos não são recuperáveis.

*Resultado esperado:* instância operando com o novo chaveiro e cada recurso recriado funcionando (login com novo MFA, provedor SSO, webhook). *Se falhar:* erros de decifragem nos logs indicam algum registro ainda cifrado pela chave antiga — refaça o cadastro daquele item.

### Produção: perda de um nó ou do primário

Não exige restauração de dados: substitua o nó perdido seguindo [adicionar um nó](high-availability.md#roteiros). Se o primário cair, outro candidato assume a liderança automaticamente ([perda do primário](high-availability.md#roteiros)).

### Produção: banco perdido ou corrompido

1. Feche o site no balanceador ([manutenção pela infraestrutura](maintenance.md)) e pare `web`, `worker`, `scheduler` e `agent` em todos os nós.
2. Se o servidor de dados foi perdido, suba o projeto da instância em um novo servidor usando o mesmo `.env` do cofre e os mesmos certificados.
3. Restaure o banco para o ponto desejado: via PITR (pgBackRest ou WAL-G, até o instante anterior ao incidente ou até o ponto `skeleton-<operação>` de uma atualização) ou via dump lógico (`DROP SCHEMA` e `pg_restore`, como no sandbox).
4. Se o ponto restaurado preceder a última operação do agente, restaure também `storage_root` e os releases do primário referentes ao mesmo instante (conforme perda total, abaixo). Se o problema veio de uma atualização malsucedida, use o fluxo do próprio agente: `recover <operação> restore`, ou restaure via PITR seguida de `recover <operação> abort --external-restored` ([backup externo](deployment.md#backup-crash-e-recuperação)).
5. Suba os agentes (primeiro o candidato escolhido como primário), em seguida os serviços de aplicação, e execute a [conferência](#conferência-depois-de-qualquer-recuperação). Execute `cluster forget <nó>` para eventuais nós desativados.

*Resultado esperado:* um primário ativo, todos os nós alinhados na mesma geração do banco e site restaurado. *Se falhar:* nós em gerações distintas ou erro em `check_skeleton_schema` indicam desalinhamento entre banco e releases — selecione um ponto do PITR e um estado do agente correspondentes ao mesmo momento.

### Produção: `storage_root` perdido

O atendimento aos usuários não é interrompido, pois servir tráfego não depende do agente. Pare os agentes, restaure o diretório a partir do último snapshot e inicie os agentes novamente.

*Resultado esperado:* `cluster status` exibindo um primário e o `journal` preservando as operações anteriores. *Se falhar:* `cluster_storage_not_shared` aponta que os candidatos não estão montando o mesmo diretório — corrija a montagem antes de reiniciar os agentes. Na ausência de backup, atualizações e instalações ficam bloqueadas até que o operador reenvie os pacotes das apps instaladas e recadastre as fontes; não inicie atualizações antes de reconstituir esses arquivos.

### Produção: perda total (nós e servidor de dados)

1. Provisione um novo servidor de dados com o banco restaurado (conforme roteiro acima).
2. Em um novo nó candidato configurado com as mesmas variáveis anteriores, restaure `storage_root` e o volume `releases` do primário do mesmo instante nos mesmos caminhos, defina `SKELETON_CLUSTER_PREFERRED=true` e suba apenas esse nó.
3. Confirme que esse nó assumiu como primário e publicou o release do cluster; em seguida, suba os demais nós, que instalarão o release a partir do primário antes de iniciar os serviços.
4. Os anexos vêm do bucket S3 (ou do volume restaurado) e a configuração vem do cofre.

*Resultado esperado:* cluster operacional na mesma geração do backup. *Se falhar:* sem o backup dos releases do primário, recorra ao procedimento de [último recurso](#estado-do-agente-perdido-sem-backup-último-recurso) fixando as versões exatas do núcleo e de cada app.

### Anexos perdidos

No bucket S3, restaure as versões anteriores dos objetos apagados ou sobrescritos pelo versionamento; em volumes compartilhados, restaure o snapshot. Um anexo cujo arquivo esteja ausente responde `404` sem impactar o restante da aplicação.

*Resultado esperado:* anexos, imagens da marca e temas voltam a abrir normalmente. *Se falhar:* sem versionamento nem snapshot, o arquivo não é recuperável, mas o registro permanece no banco e o anexo pode ser reenviado.

## Teste periódico de restauração

Execute este exercício trimestralmente e sempre que o plano de backup for alterado:

1. Restaure o conjunto mais recente do sandbox (ou um ponto de PITR da produção) em um servidor isolado, sem DNS público, seguindo o roteiro de perda total.
2. Execute integralmente a [conferência](#conferência-depois-de-qualquer-recuperação).
3. Registre a data, o conjunto restaurado, o tempo total gasto (RTO real), a idade dos dados restaurados (RPO real) e eventuais ajustes necessários neste guia.
4. Destrua o ambiente de teste ao concluir, pois ele contém dados e segredos reais.
