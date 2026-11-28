# Alta disponibilidade

Este guia orienta quem opera uma instância distribuída em vários nós de aplicação, com PostgreSQL e Redis dedicados em um servidor de dados à parte. Ele reúne a arquitetura, as decisões operacionais e os roteiros de rotina e contingência, apontando para a referência detalhada de cada componente. Para avaliar o sistema em um único host com tudo integrado, utilize `compose.sandbox.yaml` ([sandbox](deployment.md#sandbox-no-coolify-369)).

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
| Dados (usuários, negócio, auditoria, sessões, jobs) | PostgreSQL da instância | Fonte de verdade; o worker usa `SKIP LOCKED` e o agendador cria apenas um job por janela, permitindo que vários nós executem ambos simultaneamente ([jobs](jobs.md)) |
| Rate limit, antirreuso de códigos TOTP e códigos de autorização OIDC | Redis da instância, com `SKELETON_CACHE_KEY_PREFIX` | Precisam ser compartilhados de forma consistente entre todos os nós |
| Anexos, pacotes de tema e exportações CSV | S3 privado ou volume compartilhado declarado com `SKELETON_FILES_SHARED=true` | Qualquer nó precisa ler arquivos gravados por outro (`skeleton.E048`) |
| Configuração da instância | Ambiente de cada nó, a partir do mesmo arquivo (modo cluster) | Um SQLite local por nó causaria divergência ([modo cluster](deployment.md#configuração-da-instância-143)) |
| Releases (código e virtualenv) | Local em cada nó, no mesmo caminho absoluto | Cada nó executa seu próprio processo e o agente instala o mesmo release em todos |
| Journal, planos, uploads e backups do agente | `storage_root`, compartilhado entre os candidatos | Permite que o candidato que assume leia o journal deixado pelo primário perdido |
| Estado do cluster de agentes | Schema `skeleton_deploy` no PostgreSQL da instância | Registra o primário atual, os nós conhecidos e as fases de cada atualização |

Os caches por processo do núcleo (aparência, página inicial e domínios próprios) releem o PostgreSQL a cada 5 a 30 segundos, propagando alterações a todos os nós dentro desse prazo, sem necessidade de reinício.

## Servidor de dados

O arquivo `deploy/data-server/compose.yaml` sobe, para cada instância, um PostgreSQL (com o bootstrap endurecido, TLS obrigatório e `pg_hba` restrito à rede dos nós), um Redis (com senha e `--maxmemory` usando `volatile-lru`) e, pelo perfil `pooler`, um PgBouncer em modo transação exclusivo para a role runtime ([servidor de dados](deployment.md#provisionamento-postgresql)).

- **Sempre um cluster PostgreSQL por instância.** As roles do núcleo (`skeleton_owner`, `skeleton_runtime`, `skeleton_audit_writer` e `skeleton_agent`) têm nomes fixos e escopo global no cluster: colocar duas instâncias no mesmo cluster faria ambas compartilharem a role runtime e sua senha. O check `skeleton.W051` avisa quando a role runtime consegue conectar em outro banco do cluster. Um mesmo servidor de dados pode hospedar vários clusters, cada um em seu projeto Compose com portas, volumes e senhas separados.
- **O servidor de dados isolado não fornece alta disponibilidade.** Para o PostgreSQL, utilize serviço gerenciado ou réplica em streaming com failover (Patroni ou repmgr) e liste todos os hosts na URL: `postgres://...@db-a:5432,db-b:5432/skeleton?target_session_attrs=read-write&sslmode=verify-full` (omitir `target_session_attrs` dispara `skeleton.W050`). Para o Redis, use serviço gerenciado ou réplica com Sentinel atrás de um endereço estável (como um HAProxy encaminhando apenas ao nó com `role:master`), pois o cliente de cache do Django não fala o protocolo Sentinel.
- **Dimensionamento de conexões.** O total `nós × (workers × threads do Gunicorn + worker + scheduler) + agente` precisa caber em `max_connections`. Acima desse limite, coloque o PgBouncer à frente da role runtime e ative `DATABASE_TRANSACTION_POOLER=true` nos nós; a role owner e os agentes conectam sempre diretamente ao PostgreSQL ([pooler](deployment.md#provisionamento-postgresql)).

## Nós de app

Cada nó executa o `compose.yaml` como projeto independente ([nó de app](deployment.md#settings-e-serviços)). O balanceador à frente deles deve:

- repassar o cabeçalho `Host` original e substituir `X-Forwarded-Proto` (com `DJANGO_TRUST_PROXY=true` e `DJANGO_TRUSTED_PROXY_COUNT` compatível; veja [proxy](deployment.md#settings-e-serviços));
- sondar `/health/ready/` em intervalo menor que o `drain_grace` do agente (padrão 20 s): a sonda responde `503` com `draining` antes de uma troca nó a nó e `503` se o banco cair; se o Redis cair, responde `200` com `{"degraded": ["cache"]}`, mantendo o nó em rotação para não derrubar todos os nós simultaneamente;
- dispensar afinidade de sessão (*sticky sessions*), pois as sessões são armazenadas no PostgreSQL.

## Configuração

Em modo cluster (`SKELETON_INSTANCE_MODE=cluster`), toda configuração vem exclusivamente do ambiente, idêntica em todos os nós: o setup não é exibido, `/platform/instance/` fica somente leitura e a CLI recusa alterações. O healthcheck do container (`python -m skeleton.api.healthcheck`) também respeita o modo cluster — nunca abre o SQLite local — e escolhe o primeiro host concreto de `DJANGO_ALLOWED_HOSTS`, recorrendo a `localhost` apenas se houver `*`.

Para gerar a configuração inicial, conclua o setup em uma cópia `standalone` apontada para o servidor de dados e exporte com `manage.py instance export-env --output instancia.env` (gera um arquivo `0600` com os segredos decifrados, que deve ser guardado no cofre de segredos). Para alterar qualquer configuração posteriormente, edite o arquivo distribuído aos nós e execute um reinício em rolagem.

## Manutenção

A aplicação mantém apenas o portão do deploy, acionado pelo agente durante atualizações. Manutenções planejadas devem ser conduzidas no balanceador ou no Coolify, servindo uma página estática de manutenção com os serviços parados ([manutenção](maintenance.md)).

## O agente em vários nós

Cada nó executa seu próprio agente, e exatamente um deles assume como **primário** — o único autorizado a aplicar migrations ([agente em vários nós](deployment.md#agente-em-vários-nós-678)).

- **Candidato e seguidor:** candidatos possuem a credencial owner e podem assumir como primário; seguidores utilizam apenas a role `skeleton_agent` e nunca se tornam primários.
- **Eleição do primário:** torna-se primário o candidato que segura a trava de sessão do PostgreSQL para o cluster e a época atual na linha `skeleton_deploy."primary"`. Como o banco libera a trava apenas quando a sessão encerra, nunca há dois primários conectados ao mesmo tempo. Antes de cada etapa que modifica o banco, o primário reconfere a trava e a época; se tiver travado e perdido a liderança, recusa com `not_primary`.
- **Reatribuição do primário:** com o cluster ocioso, execute `cluster handover --to <nó>`. Em caso de queda do primário, a trava é liberada em cerca de 30 s e outro candidato assume com a época seguinte — o tráfego de usuários não depende do primário. Se houver operação em curso durante a queda, o novo primário a marca como `interrupted` e aguarda o comando `recover` do operador.
- **Canal entre agentes:** comunicação HTTPS com TLS mútuo (validado contra uma CA privada do cluster) e assinatura HMAC em cada requisição. O seguidor baixa do primário o release pronto e encaminha a ele as solicitações do web local. Em cada nó, o processo web fala exclusivamente com o socket Unix do agente local.
- **Recuperação de serviços sob `--wait-for-agent`:** se um container de aplicação encerrar abruptamente (OOM ou SIGKILL) e reiniciar, o inicializador retoma o serviço sozinho quando o último pedido aplicado já era `restart` com `wanted="running"`. Em paralelo, o agente seguidor reconcilia serviços presos em `waiting (agent)` fora de manutenção a cada heartbeat (em até um ciclo). Cada serviço executa em seu próprio grupo de processos (`start_new_session=True`), encerrado via `os.killpg` sem deixar processos órfãos.
- **Atualização:** mudanças com migration entram em janela de manutenção coordenada em todos os nós (cada nó recebe e valida o release, todos param, o primário executa backup e migrations, todos trocam e validam saúde). Patches sem migration na mesma linha e com as mesmas apps trocam nó a nó, sem janela ([atualizações](updates.md#atualizar-um-cluster-de-vários-nós)).
- **Versão dos agentes:** o primário coordena a versão dos agentes em todo o cluster. "Atualizar agente" atualiza primeiro os demais nós e o primário por último, e nenhuma atualização de aplicação começa enquanto houver agentes em versões divergentes. Em containers com o volume `agent_code`, esse volume atua como `agent_root` e os nós seguem a nova versão automaticamente; sem o volume, o agente vem da imagem e precisa ser atualizado com uma imagem nova ([versão dos agentes](deployment.md#agente-em-vários-nós-678), [atualizar o agente nos containers](deployment.md#atualizar-o-agente-nos-containers-772)).
- **Backup:** veja o plano completo — o que copiar, como proteger segredos e roteiros de desastre — em [backup e recuperação](backup-recovery.md). Para bancos grandes, adote backup contínuo do PostgreSQL com `"backup": {"mode": "external", ...}`, fazendo o agente registrar um ponto de restauração em vez de gerar dump ([backup externo](deployment.md#backup-crash-e-recuperação)). Ative também o versionamento no bucket S3 dos anexos.

## Roteiros

**Instalação nova.**
1. Suba o servidor de dados da instância.
2. Gere a configuração inicial (setup `standalone` + `instance export-env`).
3. Gere a CA privada do canal, um certificado por nó e o segredo compartilhado (`cluster keygen`).
4. Suba o primeiro nó como candidato com `SKELETON_CLUSTER_PREFERRED=true` e crie o administrador inicial com `manage.py bootstrap_skeleton` dentro do `web`.
5. Suba os demais nós; cada um sincroniza automaticamente o release ativo do cluster.

*Resultado esperado:* `cluster status` lista o primário e todos os nós vivos, e `/health/release/` retorna a mesma geração em todos. *Se falhar:* inspecione `docker compose logs agent` no nó afetado e os logs da operação em `storage_root/logs/`.

**Migrar de um host só para HA.** Pare o `compose.yaml` antigo (ou o sandbox); gere `pg_dump --format=custom` com a role owner e restaure com `pg_restore` no novo PostgreSQL do servidor de dados (já provisionado pelo bootstrap); execute `manage.py instance export-env` na instalação antiga; copie os anexos locais para o bucket S3 (preservando os caminhos de chave) ou para o volume compartilhado; suba o primeiro nó apontado para o novo banco com a configuração exportada.

*Resultado esperado:* login e anexos antigos funcionam normalmente no novo nó. *Se falhar:* a instalação original permanece intacta e pode ser retomada.

**Adicionar um nó.** Emita um certificado para o nó assinado pela mesma CA, entregue o segredo do canal, o arquivo de configuração e as variáveis do nó, e suba o `compose.yaml`. O agente instala o release ativo do cluster e somente então inicia os serviços. Inclua o nó no balanceador apenas depois que `/health/ready/` responder `200`.

*Resultado esperado:* `cluster status` mostra o nó vivo e `/health/release/` exibe a mesma geração dos demais. *Se falhar:* o nó não sobe código antigo graças a `--wait-for-agent`; inspecione `docker compose logs agent` (verificando certificado, segredo, relógio ou `follower_release_mismatch`) e corrija antes de colocá-lo em rotação.

*Remover um nó:* retire-o do balanceador, pare o projeto e execute `cluster forget <nó>` no primário. Se o comando recusar com `cannot_forget_primary`, transfira a liderança antes com `cluster handover`.

**Perda do primário.** Sem operação em andamento, não há ação manual: outro candidato assume automaticamente (confirme em `cluster status`). Se houver operação em curso, a manutenção permanece ativa; no novo primário, leia o journal (`journal`) e execute `recover <op> abort|restore|complete` ([recuperação](deployment.md#backup-crash-e-recuperação)). Caso o cluster tenha apenas um candidato, restaure `storage_root` do backup em outro nó e promova-o a candidato.

*Resultado esperado:* `cluster status` indica o novo primário com a época seguinte e, após o `recover`, a operação termina em `ready`, `cancelled` ou `restored` sem marcador de manutenção. *Se falhar:* `cluster_storage_not_shared` indica que os candidatos não enxergam o mesmo `storage_root` (corrija a montagem compartilhada); `not_primary` no `recover` indica que o comando deve ser executado no nó apontado como primário em `cluster status`.

**Failover do PostgreSQL.** Com a URL listando todos os hosts e `target_session_attrs=read-write`, novas conexões direcionam-se ao novo primário, e conexões persistentes são testadas antes do reuso (`CONN_HEALTH_CHECKS`). Requisições em curso durante a virada falham uma vez. Como o agente mantém a trava em conexão própria, um candidato a retoma após o failover na época seguinte.

*Resultado esperado:* `/health/ready/` volta a responder `200` em segundos em todos os nós e `cluster status` apresenta um primário. *Se falhar:* mantendo-se em `503`, confirme se a URL inclui o novo primário e `target_session_attrs=read-write` (`skeleton.W050` sinaliza a omissão); se usar PgBouncer, aponte-o para o novo primário.

**Queda do Redis.** Usuários já autenticados continuam navegando normalmente, enquanto login, MFA e o provedor OIDC ficam indisponíveis até o retorno do Redis. A sonda de prontidão responde `200` com `degraded` para evitar que o balanceador remova todos os nós de rotação.

*Resultado esperado:* ao retornar o Redis, `/health/ready/` deixa de reportar `degraded` e o login volta sem reinício. *Se falhar:* se `degraded` persistir após o retorno, revise `CACHE_URL` (o endereço estável do primário atrás do Sentinel) e a senha; o evento `health.probe_degraded` nos logs mostra desde quando a degradação ocorre.

## Limites

- Atualizações com migration ainda exigem janela de manutenção em todos os nós; a troca nó a nó aplica-se apenas a patches sem migration.
- Réplicas de leitura não são utilizadas: todas as consultas vão ao primário do PostgreSQL.
- O canal entre agentes exige relógios sincronizados via NTP (janela de 60 s no HMAC), além da mesma imagem e do mesmo `releases_root` em todos os nós.
- O cliente Redis não implementa failover nativo: manter um endereço estável (Sentinel + HAProxy ou serviço gerenciado) é responsabilidade do operador.
