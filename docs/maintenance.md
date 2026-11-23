# Manutenção

A aplicação tem um único bloqueio de manutenção: o **portão do deploy**, que o agente de deployment liga e desliga durante uma atualização. A manutenção planejada (trocar o servidor, mexer no banco, investigar um incidente) é feita **na infraestrutura**, no proxy ou no Coolify, porque ela continua funcionando quando a própria aplicação está instável ou parada. O modo de manutenção administrativo que existia na aplicação (a tela `/platform/maintenance/` e o comando de manutenção da CLI, #446–#448) foi removido no #674.

## O portão do deploy

| | |
|---|---|
| Quem liga e desliga | Só o agente de deployment, durante uma atualização ([deployment](deployment.md#inventário-manutenção-e-confirmação)) |
| Onde fica | O arquivo `SKELETON_MAINTENANCE_MARKER`, montado só para leitura no web; num cluster, um por nó, escrito pelo agente de cada nó |
| O que fica aberto | As sondas (`/health/live/`, `/health/ready/`, `/health/release/`), o `/metrics`, os arquivos estáticos e as requisições de smoke do agente (o token de `SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE`) |
| Jobs | Worker e scheduler pausam (heartbeat `paused`) |

O portão protege duas coisas: as migrations (ninguém escreve no banco enquanto o schema muda) e a verificação do release novo, que o agente faz com o token de smoke **antes** de abrir o tráfego público. Não remova o marcador à mão: a [recuperação](deployment.md#backup-crash-e-recuperação) diz quando e como.

## A resposta

`skeleton.common.maintenance_response` monta a resposta do portão:

- status 503, `Cache-Control: no-store` e `Retry-After: 60`;
- em `/api/` ou com `Accept` JSON sem HTML: `{"code": "maintenance", "detail": "<mensagem>", "ends_at": null}` ([API](api.md));
- nos demais casos: a página autônoma `skeleton/maintenance.html`, renderizada sem `request` e sem context processors;
- `HEAD` responde com corpo vazio.

O token de API não tem exceção: `Authorization: Bearer` também recebe 503. As métricas rotulam essas respostas com `route="maintenance"`, nunca com o caminho.

## Manutenção planejada pela infraestrutura

O caminho é sempre o mesmo: o proxy passa a responder uma página de manutenção estática com 503 para o domínio, e só depois os serviços param.

- **Coolify:** no recurso, pare os serviços `web`, `worker` e `scheduler` (o `agent` pode continuar). O proxy do Coolify (Traefik) passa a responder o erro do domínio; para uma página própria, configure no Traefik uma página de erro para o domínio (middleware `errors` apontando para um serviço que serve um HTML estático) e mantenha-a ligada durante a janela.
- **Traefik próprio:** um middleware `errors` (`status: ["502-504"]`) com um serviço de página estática; desligue o roteador do `web` ou pare o serviço.
- **Caddy:** `handle_errors` com `respond "Em manutenção" 503` ou um `file_server` com a página; para fechar de propósito, um `respond 503` no site.
- **nginx:** `error_page 502 503 504 /maintenance.html;` com a página num `location = /maintenance.html` interno; para fechar de propósito, `return 503;` no `server`.
- **Jobs:** para que nada rode durante a janela, pare `worker` e `scheduler`. A fila acumula e é drenada quando eles voltam: webhooks, e-mails e notificações chegam atrasados.

Num cluster de vários nós, faça isso no balanceador (que atende todos os nós), não em cada nó ([alta disponibilidade](high-availability.md)).

## Limites

- A manutenção não é backup: o plano de backup e de recuperação de desastre está em [backup e recuperação](backup-recovery.md).

- O portão do deploy não serve para manutenção planejada: só o agente o escreve, e ele faz parte do protocolo de atualização.
- Uma instância que atualizar de uma versão com a manutenção administrativa **ativa** abre o site ao subir o release novo: saia dela antes de atualizar e use a infraestrutura.
