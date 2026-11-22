# Alta disponibilidade

Este guia é para quem opera uma instância em vários nós de app, com o PostgreSQL e o Redis num servidor de dados à parte. Ele junta a arquitetura, as decisões e os roteiros; os detalhes de cada peça estão nos documentos citados. Para avaliar o sistema num host só, use o `compose.sandbox.yaml`, que traz tudo junto ([sandbox](deployment.md#sandbox-no-coolify-369)).

## Arquitetura

```
             Balanceador / proxy TLS (Host original; X-Forwarded-Proto substituído; checa /health/ready/)
                    |                         |                         |
            nó de app A               nó de app B               nó de app C
            agent (primário)          agent (seguidor)          agent (candidato)
            web, worker, scheduler    web, worker, scheduler    web, worker, scheduler
            releases/ local           releases/ local           releases/ local
                    \_________________________|_________________________/
                                              |
              servidor de dados (um projeto Compose por instância: deploy/data-server/)
              PostgreSQL da instância (+ réplica e failover)   Redis da instância (+ Sentinel)
              [opcional] PgBouncer em modo transação, só para a role runtime
              armazenamento de objetos compatível com S3 (anexos, temas, exportações)
```

| O quê | Onde fica | Por quê |
|---|---|---|
| Dados (usuários, negócio, auditoria, sessões, jobs) | PostgreSQL da instância | Fonte de verdade; o worker usa `SKIP LOCKED` e o agendador cria um job por janela, então vários nós rodam os dois ([jobs](jobs.md)) |
| Rate limit, antirreuso de códigos TOTP, códigos de autorização OIDC | Redis da instância, com `SKELETON_CACHE_KEY_PREFIX` | Precisam ser os mesmos em todos os nós |
| Anexos, pacotes de tema, exportações CSV | S3 privado, ou um volume compartilhado declarado com `SKELETON_FILES_SHARED=true` | Um nó lê o que outro gravou (`skeleton.E048`) |
| Configuração da instância | O ambiente de cada nó, o mesmo arquivo em todos (modo cluster) | Um SQLite por nó divergiria ([modo cluster](deployment.md#configuração-da-instância-143)) |
| Releases (código e virtualenv) | Local em cada nó, o mesmo caminho em todos | Cada nó roda o próprio processo; o agente instala o mesmo release em todos |
| Journal, planos, uploads e backups do agente | `storage_root`, compartilhado entre os candidatos | O candidato que assume lê o journal do primário perdido |
| Estado do cluster de agentes | Schema `skeleton_deploy` do PostgreSQL da instância | Quem é o primário, os nós e as fases de uma atualização |

Os caches por processo do núcleo (aparência, página inicial, domínios próprios) releem o PostgreSQL a cada 5 a 30 segundos: uma mudança chega a todos os nós nesse prazo, sem reinício.

## Servidor de dados

O `deploy/data-server/compose.yaml` sobe, por instância, o PostgreSQL (com o bootstrap revisado, TLS obrigatório e `pg_hba` só da rede dos nós), o Redis (senha, `--maxmemory` com `volatile-lru`) e, no perfil `pooler`, um PgBouncer em modo transação ([servidor de dados](deployment.md#provisionamento-postgresql)).

- **Um cluster PostgreSQL por instância, sempre.** As roles do núcleo (`skeleton_owner`, `skeleton_runtime`, `skeleton_audit_writer`, `skeleton_agent`) têm nome fixo, e roles são globais no cluster: duas instâncias no mesmo cluster dividiriam a role runtime e a senha dela. `skeleton.W051` avisa quando a role runtime pode conectar em outro banco do cluster. Um servidor de dados pode hospedar vários clusters, um por projeto Compose, cada um com portas, volumes e senhas próprios.
- **O servidor de dados sozinho não é alta disponibilidade.** Para o PostgreSQL, use um serviço gerenciado ou réplica em streaming com failover (Patroni, repmgr) e liste os nós na URL: `postgres://...@db-a:5432,db-b:5432/skeleton?target_session_attrs=read-write&sslmode=verify-full` (sem `target_session_attrs`, `skeleton.W050`). Para o Redis, réplica com Sentinel atrás de um endereço estável (um HAProxy que só encaminha ao nó com `role:master`), porque o cliente de cache do Django não fala Sentinel; ou um Redis gerenciado.
- **Conexões.** Nós × (workers × threads do Gunicorn + worker + scheduler) + agente precisam caber no `max_connections`. Acima disso, ponha o PgBouncer na frente da role runtime e ligue `DATABASE_TRANSACTION_POOLER=true` nos nós; a owner e os agentes conectam sempre direto ([pooler](deployment.md#provisionamento-postgresql)).

## Nós de app

Cada nó roda o `compose.yaml` como projeto próprio ([nó de app](deployment.md#settings-e-serviços)). O balanceador:

- repassa o `Host` original e substitui `X-Forwarded-Proto` (com `DJANGO_TRUST_PROXY=true` e `DJANGO_TRUSTED_PROXY_COUNT` coerente, [proxy](deployment.md#settings-e-serviços));
- checa `/health/ready/` com intervalo menor que o `drain_grace` do agente (padrão 20 s): a sonda responde 503 `draining` antes de uma troca nó a nó, e 503 quando o banco cai; sem Redis, responde 200 com `{"degraded": ["cache"]}` e o nó continua em rotação, porque todos dividem o mesmo Redis;
- não precisa de afinidade de sessão: as sessões ficam no PostgreSQL.

## Configuração

Em modo cluster (`SKELETON_INSTANCE_MODE=cluster`), a configuração vem só do ambiente, igual em todos os nós: o setup não roda, `/platform/instance/` fica só leitura e a CLI recusa gravar. Para começar, rode o setup numa cópia `standalone` apontada para o servidor de dados e exporte com `manage.py instance export-env --output instancia.env` (um arquivo 0600 com os segredos decifrados; guarde-o no cofre de segredos). Para mudar a configuração depois, mude o arquivo e rode o reinício em rolagem.

## Manutenção

A aplicação só tem o portão do deploy, que o agente escreve durante uma atualização. A manutenção planejada é feita no balanceador ou no Coolify: uma página de manutenção estática e os serviços parados ([manutenção](maintenance.md)).

## O agente em vários nós

Cada nó roda o próprio agente; um deles é o **primário**, o único que migra ([agente em vários nós](deployment.md#agente-em-vários-nós-678)).

- **Candidato e seguidor.** Um candidato tem a credencial owner e pode virar o primário; um seguidor tem só a role `skeleton_agent` e nunca vira primário.
- **Como o primário é definido.** É o candidato que segura a trava de sessão do PostgreSQL do cluster e a época atual da linha `skeleton_deploy."primary"`. A trava só se solta quando a sessão dele termina, então nunca há dois primários enquanto o primeiro está conectado; antes de cada passo que muda o banco, o primário confere a trava e a época, e um primário que travou e voltou recusa (`not_primary`).
- **Como o primário é reatribuído.** Com o cluster ocioso, `cluster handover --to <nó>`. Se o primário cai, a trava se solta em cerca de 30 s e o próximo candidato assume com a época seguinte; servir tráfego não depende do primário. Se havia uma operação em curso, o novo primário a marca `interrupted` e espera o `recover` do operador.
- **Canal entre agentes.** HTTPS com TLS mútuo contra uma CA privada do cluster e HMAC por pedido; o seguidor baixa do primário o release pronto e encaminha a ele os pedidos do web do próprio nó. O web de cada nó continua falando só com o socket Unix do agente local.
- **Atualização.** Com migration, uma janela de manutenção em todos os nós: cada nó recebe e confere o release, todos param, o primário faz backup e migrations, todos trocam e conferem a saúde. Sem migration (um patch da mesma linha, com as mesmas apps), a troca é nó a nó, sem janela ([atualizações](updates.md#atualizar-um-cluster-de-vários-nós)).
- **Backup.** Com o banco grande, use o backup contínuo do PostgreSQL e `"backup": {"mode": "external", ...}`: o agente marca um ponto de restauração em vez do dump ([backup externo](deployment.md#backup-crash-e-recuperação)). Ligue o versionamento do bucket S3 dos anexos.

## Roteiros

**Instalação nova.** (1) Suba o servidor de dados da instância. (2) Gere a configuração (setup `standalone` + `instance export-env`). (3) Gere a CA do canal, um certificado por nó e o segredo (`cluster keygen`). (4) Suba o primeiro nó como candidato com `SKELETON_CLUSTER_PREFERRED=true` e crie o administrador com `manage.py bootstrap_skeleton` no `web`. (5) Suba os demais nós; cada um entra sozinho no release do cluster. **Resultado esperado:** `cluster status` mostra o primário e todos os nós vivos, e `/health/release/` responde a mesma geração em todos. **Se falhar:** veja `docker compose logs agent` do nó e os logs da operação em `storage_root/logs/`.

**Migrar de um host só para HA.** Pare o `compose.yaml` antigo (ou o sandbox); faça `pg_dump --format=custom` do banco com a owner e `pg_restore` no PostgreSQL novo do servidor de dados (provisionado pelo bootstrap); rode `manage.py instance export-env` na instalação antiga; copie os anexos locais para o bucket S3 (com o mesmo caminho de chave) ou para o volume compartilhado; suba o primeiro nó apontado para o banco novo, com a configuração exportada. **Resultado esperado:** o login e os anexos antigos funcionam no nó novo. **Se falhar:** a instalação antiga continua intacta; volte a ela.

**Adicionar um nó.** Gere o certificado do nó com a mesma CA, entregue o segredo, a configuração e as variáveis do nó, e suba o `compose.yaml`. O agente instala o release do cluster e só então sobe os serviços. Ponha o nó no balanceador depois de `/health/ready/` responder 200. **Remover um nó:** tire-o do balanceador, pare o projeto e rode `cluster forget <nó>` no primário.

**Perda do primário.** Sem operação em curso, nada a fazer: outro candidato assume (confira com `cluster status`). Com operação em curso, a manutenção continua: no novo primário, leia o journal (`journal`) e escolha `recover <op> abort|restore|complete` ([recuperação](deployment.md#backup-crash-e-recuperação)). Com um só candidato, restaure o `storage_root` do backup num nó e torne-o candidato.

**Failover do PostgreSQL.** Com a URL de vários hosts e `target_session_attrs=read-write`, as conexões novas vão ao novo primário; as conexões persistentes são conferidas antes do reuso (`CONN_HEALTH_CHECKS`). Pedidos em curso na troca falham uma vez. O agente segura a trava numa conexão própria: depois do failover, um candidato a retoma, com a época seguinte.

**Queda do Redis.** O site continua no ar para quem já está logado; login, MFA e o provedor OIDC falham até o Redis voltar. A prontidão fica 200 com `degraded`, para o balanceador não tirar todos os nós.

## Limites

- Atualização com migration ainda tem janela de manutenção em todos os nós; a troca nó a nó vale só para patches sem migration.
- Réplicas de leitura não são usadas: toda consulta vai ao primário do PostgreSQL.
- O canal entre agentes exige NTP nos nós (janela de 60 s do HMAC) e a mesma imagem e o mesmo `releases_root` em todos.
- O Redis não tem failover no cliente: o endereço estável (Sentinel + HAProxy ou serviço gerenciado) é do operador.
