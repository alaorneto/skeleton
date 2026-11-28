# Manutenção

A aplicação possui um único bloqueio de manutenção embutido: o **portão do deploy**, ligado e desligado exclusivamente pelo agente de deployment durante atualizações.

Manutenções planejadas — como trocar o servidor, intervir no banco de dados ou investigar incidentes — são feitas **na infraestrutura** (no proxy reverso ou no Coolify), pois ela continua disponível mesmo quando a aplicação está instável ou parada. O antigo modo de manutenção administrativo interno (a tela `/platform/maintenance/` e o comando correspondente na CLI) foi removido.

## O portão do deploy

| Aspecto | Comportamento |
|---|---|
| Quem liga e desliga | Apenas o agente de deployment, durante uma atualização ([deployment](deployment.md#inventário-manutenção-e-confirmação)) |
| Onde fica | No arquivo indicado por `SKELETON_MAINTENANCE_MARKER`, montado somente para leitura no processo web; em um cluster, existe um arquivo por nó, escrito pelo agente local |
| O que permanece aberto | Sondas de saúde (`/health/live/`, `/health/ready/`, `/health/release/`), `/metrics`, arquivos estáticos e requisições de smoke do agente (autenticadas pelo token de `SKELETON_DEPLOYMENT_SMOKE_TOKEN_FILE`) |
| Jobs | Worker e agendador entram em pausa (heartbeat `paused`) |

O portão protege duas etapas críticas: a execução de migrations (impedindo escritas enquanto o schema muda) e a verificação do novo release pelo agente usando o token de smoke **antes** de reabrir o tráfego público. Nunca remova o marcador manualmente — consulte [recuperação](deployment.md#backup-crash-e-recuperação) para saber quando e como intervir.

## A resposta

`skeleton.common.maintenance_response` constrói a resposta enquanto o portão está ativo:

- status `503`, cabeçalhos `Cache-Control: no-store` e `Retry-After: 60`;
- em `/api/` ou quando o cliente envia `Accept` JSON sem HTML: `{"code": "maintenance", "detail": "<mensagem>", "ends_at": null}` ([API](api.md));
- nos demais casos: a página autônoma `skeleton/maintenance.html`, renderizada sem `request` e sem context processors;
- requisições `HEAD` respondem com corpo vazio.

Tokens de API não têm exceção: requisições com `Authorization: Bearer` também recebem `503`. Nas métricas, todas essas respostas recebem o rótulo `route="maintenance"`, nunca o caminho da requisição.

## Manutenção planejada pela infraestrutura

O princípio é sempre o mesmo: configure o proxy para servir uma página estática de manutenção com status `503` para o domínio e, só então, pare os serviços.

- **Coolify:** no recurso, pare os serviços `web`, `worker` e `scheduler` (o `agent` pode permanecer em execução). O proxy do Coolify (Traefik) exibirá a página de erro padrão; para servir uma página personalizada, configure no Traefik um middleware `errors` apontando para um serviço de HTML estático e mantenha-o ativo durante a janela.
- **Traefik próprio:** utilize um middleware `errors` (`status: ["502-504"]`) ligado a um serviço de página estática e desative o roteador do `web` ou pare o serviço.
- **Caddy:** use `handle_errors` com `respond "Em manutenção" 503` ou `file_server` servindo a página; para fechar o acesso explicitamente, aplique `respond 503` no site.
- **nginx:** configure `error_page 502 503 504 /maintenance.html;` apontando para um `location = /maintenance.html` interno; para fechar explicitamente, adicione `return 503;` ao bloco `server`.
- **Jobs:** pare `worker` e `scheduler` para que nenhuma tarefa execute durante a janela. A fila acumula normalmente e é drenada assim que os processos retornam, entregando webhooks, e-mails e notificações com atraso.

Em um cluster de vários nós, ative a manutenção no balanceador que atende todos os nós, e não nó a nó ([alta disponibilidade](high-availability.md)).

## Limites

- Manutenção não substitui backup: o plano de backup e recuperação de desastre está em [backup e recuperação](backup-recovery.md).
- O portão do deploy não deve ser usado para manutenções planejadas: apenas o agente o manipula, como parte do protocolo de atualização.
- Se uma instância antiga for atualizada a partir de uma versão com a manutenção administrativa **ativa**, o site voltará aberto ao subir o novo release; desative-a antes de atualizar e controle a janela pela infraestrutura.
