# Manutenção

A instância tem dois modos de manutenção, independentes entre si. Os dois respondem 503 com a mesma resposta, HTML no navegador e JSON na API, antes da sessão, da autenticação e do banco.

| | Manutenção do deploy | Manutenção administrativa |
|---|---|---|
| Quem liga e desliga | Só o agente de deployment ([deployment](deployment.md#inventário-manutenção-e-confirmação)) | Um administrador da plataforma em `/platform/maintenance/`, a CLI `manage.py maintenance` ou o término agendado |
| Onde fica | O arquivo `SKELETON_MAINTENANCE_MARKER`, montado só para leitura no web | A tabela `maintenance` do SQLite da instância (`SKELETON_INSTANCE_DB`) |
| O que fica aberto | Nada além das sondas e das requisições de smoke do agente | Login, logout, MFA, reautenticação, SSO e `/platform/` só para administradores completos |
| Jobs | Worker e scheduler pausam sempre | Pausam se a opção "pausar jobs" estiver marcada (o padrão) |
| Mensagem | Texto padrão de atualização | Mensagem do administrador, com a previsão de término |

Um modo não desliga o outro. O agente apaga o marcador ao terminar um deploy, mas a manutenção administrativa continua. Sair da manutenção administrativa também não remove o marcador.

## A resposta de manutenção

`skeleton.common.maintenance_response` monta a resposta dos dois modos:

- status 503, `Cache-Control: no-store` e `Retry-After` (segundos até o término agendado, no mínimo 30; 300 sem término; 60 no deploy);
- em `/api/` ou com `Accept` JSON sem HTML: `{"code": "maintenance", "detail": "<mensagem>", "ends_at": "<ISO 8601 UTC ou null>"}` ([API](api.md));
- nos demais casos: a página autônoma `skeleton/maintenance.html`, renderizada sem `request` e sem context processors, com a mensagem escapada, a previsão de término e, na manutenção administrativa, o link "Entrar como administrador da plataforma";
- `HEAD` responde com corpo vazio.

O token de API não tem exceção: `Authorization: Bearer` também recebe 503. As sondas `/health/live/`, `/health/ready/` e `/health/release/`, o `/metrics` e os arquivos estáticos respondem antes do bloqueio e seguem como antes. As métricas rotulam essas respostas com `route="maintenance"`, nunca com o caminho.

## O que fica aberto na manutenção administrativa

A lista é fechada, por nome de URL, em `skeleton.deployment.MAINTENANCE_OPEN_URL_NAMES`: login e logout, reautenticação, as telas de MFA (autenticar, reautenticar, ativar TOTP, códigos de recuperação, WebAuthn) e os logins e callbacks de SSO (OIDC, SAML, LDAP). Mais as rotas do namespace `platform`, sob `/platform/`. Cadastro, troca e recuperação de senha e gestão de e-mail recebem 503, porque enviam e-mail dentro da requisição. Uma rota nova só fica aberta se o nome dela entrar na lista.

Em `/platform/`, só passa quem é administrador completo da plataforma (staff e superuser, com MFA quando `SKELETON_REQUIRE_ADMIN_MFA` está ligado). Os papéis delegados (auditor, operador) e qualquer outra pessoa recebem a mesma resposta de manutenção, não 403, para não revelar a superfície administrativa. O guard é `skeleton.web.maintenance.MaintenancePlatformGuardMiddleware`, depois da autenticação.

## Pela CLI

O comando usa só o SQLite da instância: não abre o PostgreSQL e não roda os system checks. Por isso ele também tira a instância da manutenção com o banco fora do ar.

```sh
python manage.py maintenance status
python manage.py maintenance on --message "Atualização do banco" --for 2h
python manage.py maintenance on --until 2026-11-14T03:00-03:00 --keep-jobs
python manage.py maintenance off
```

- `--message`: texto simples, até 500 caracteres, com quebras de linha; caracteres de controle são recusados. Sem mensagem, vale o texto padrão.
- `--for` (`90m`, `2h`, `1d`) ou `--until` (ISO 8601 com fuso): o término agendado. Sem um deles, o modo dura até alguém sair.
- `--keep-jobs`: worker e scheduler continuam processando.
- `on` com o modo já ativo troca a mensagem, o término ou a opção de jobs e mantém o início.

No Compose, rode o comando no container web, que monta o volume da instância: `docker compose exec web python manage.py maintenance off`. No systemd, rode com o usuário, o diretório e o `EnvironmentFile` do serviço web: `systemd-run --wait --pipe --uid=skeleton-web -p EnvironmentFile=/srv/skeleton/private/runtime.env -p WorkingDirectory=/srv/skeleton/current /srv/skeleton/current/.venv/bin/python manage.py maintenance status`.

Cada entrada e cada saída gravam uma linha no `change_log` da instância (`key` `maintenance`, `action` `enter` ou `exit`, a origem `cli:maintenance-on`, `cli:maintenance-off` ou `platform:<pk>`) e um evento de segurança. Nenhum dos dois guarda a mensagem.

## Pela plataforma

Um administrador completo da plataforma abre `/platform/maintenance/`. A página mostra o estado, a origem, o início e a previsão de término, e tem o formulário de entrada: mensagem, término opcional, "pausar jobs" (marcado por padrão) e uma confirmação explícita. Entrar e sair exigem reautenticação recente e CSRF.

Durante o modo, toda página de `/platform/` mostra o banner "Manutenção ativa", com o link para encerrar, e a visão geral mostra o estado. Para voltar a `/platform/` durante a manutenção, use o link "Entrar como administrador da plataforma" da página de manutenção: o login e o MFA continuam abertos.

## Término agendado

O término é calculado na leitura: quando `ends_at` passa, o modo deixa de valer em todos os processos sem nenhuma escrita. A linha continua gravada como ativa até alguém sair (a CLI mostra "encerrada pelo término agendado"). O término precisa estar no futuro na entrada.

## Propagação e falha fechada

Cada processo (cada worker do Gunicorn, o worker de jobs e o scheduler) lê o estado com um leitor em cache (`skeleton.instance.maintenance`). Ele faz um `os.stat` por requisição e relê a linha quando o arquivo muda ou, no máximo, a cada 5 segundos. Uma mudança chega a todos os processos em até 5 segundos, sem reinício.

Se o SQLite existe mas não pode ser lido, o processo mantém o último estado lido. Se ainda não leu nenhum, trata o modo como ativo (falha fechada): a instância responde 503 até a leitura voltar a funcionar. Um SQLite que não existe significa que o modo nunca foi ligado.

## Jobs durante a manutenção

Com a opção "pausar jobs" (o padrão), o worker deixa de reivindicar jobs e o scheduler deixa de agendar, como no deploy, com o heartbeat `paused`. Um job em execução termina normalmente. Com `--keep-jobs`, ou a opção desmarcada, os dois seguem processando. O marcador do deploy sempre pausa, independentemente da opção.

Enquanto a pausa durar, `check_jobs` não acusa `queue_lag` e imprime `paused=1`, para que o healthcheck do worker não o marque como não saudável. A fila acumula e é drenada quando o modo termina: webhooks, e-mails e notificações chegam atrasados. Detalhes em [jobs](jobs.md).

## Limites

- O estado vive no SQLite de cada instância. Vários hosts web, cada um com o seu volume de instância, não compartilham o modo: ligue e desligue em cada host.
- A manutenção administrativa não substitui a do deploy: ela não drena o web, não faz backup e não bloqueia o agente.
- A mensagem é texto simples; não aceita HTML nem links.
