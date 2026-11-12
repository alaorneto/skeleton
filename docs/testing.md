# Testes, fixtures e evidência

Este documento tem duas funções: registrar **evidências históricas** e explicar como repetir verificações em ambiente descartável. Um resultado antigo não certifica alterações posteriores nem uma instalação de outro projeto. Os comandos abaixo são procedimentos a executar, não resultados novos.

Uma **fixture** é o ambiente/dado controlado usado pelo teste; um **smoke** é um ensaio curto do fluxo real; uma **assertion** é a condição que o teste exige. Mocks e transportes controlados ajudam a testar respostas adversas, mas não provam que um serviço externo autorizou ou executou o fluxo. Veja [autorização e contexto](concepts.md#autorização-e-contexto) e [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) antes de interpretar os resultados.

## Como escolher e interpretar a prova

| Pergunta | Prova necessária | O que não substitui essa prova |
|---|---|---|
| Contratos de registros/serviços funcionam? | A suíte inteira em PostgreSQL com a role runtime (`pytest tests`) e seus negativos. | Um teste que só exercita o ORM não prova a RLS: as assertions de isolamento usam a role runtime. |
| Runtime realmente isola organizações no banco? | PostgreSQL real, migrations owner e assertions com role runtime restrita. | Consulta como owner ou teste só no ORM. |
| Login corporativo percorre o protocolo? | Navegador, IdP real, callback e sessão final; negativos de assinatura/estado. | `force_login`, página de login 200 ou JWT local. |
| Atualização e recuperação funcionam? | Release/pacotes reais, processos reiniciados, banco e restore no backend escolhido. | Renderizar Compose, ter Dockerfile ou testar somente planejamento. |
| gov.br está homologado? | Credenciamento e login/logout no ambiente governamental autorizado. | Keycloak ou tokens RS256 locais. |

**Ordem para repetir:** provisione dependências e serviços → confirme que o banco é descartável → carregue somente os secrets de fixture → execute o comando da suíte → registre backend, versões, comando e resultado observado → encerre serviços descartáveis. Não exponha secrets para demonstrar que o ambiente existe.

## Integração contínua

`.github/workflows/ci.yml` repete em cada push para `main` e em cada pull request os procedimentos deste documento que não dependem de serviços externos. Um job verde vale como evidência apenas para o commit, o backend e as versões em que rodou.

| Job | O que prova | O que não prova |
|---|---|---|
| `lint` | Ruff 0.17.4 com as regras de `pyproject.toml` e `ruff format --check`. | E501 e PLC0415, que seguem fora da linha de base. |
| `assets` | `npm ci --ignore-scripts` do lock de `frontend/` e `npm run check`: `site.css` e `vendor/` correspondem às fontes e às versões fixadas de Tailwind, Flowbite e Inter. | Aparência no navegador; ver [interface e design](ui-design.md). |
| `locks` | uv 0.12.7 regenera `requirements.txt` e `requirements-dev.txt` com os comandos `uv pip compile` de "Atualizar locks" do `README.md` e o job exige `git diff --exit-code`: os locks correspondem aos `.in` e foram gerados pelo comando oficial (cabeçalho, `pip`/`setuptools` fixados, hashes). | Que as versões resolvidas são as mais novas ou estão livres de vulnerabilidades (ver `build`); que `pyproject.toml` declara as mesmas faixas que `requirements.in`. |
| `typecheck` | mypy estrito, com django-stubs, nos módulos listados em `[tool.mypy] files`. | Os demais módulos, importados em silêncio até serem anotados. |
| `tests` | Seis jobs paralelos, três por Python (3.12 e 3.14): duas fatias da suíte do núcleo (`pytest tests -n 4 --shard 1/2` e `2/2`, com cobertura) e a agenda (`SKELETON_BUNDLED_APPS=calendar`). Banco `ci_skeleton_test` e um por worker (`ci_gw0_skeleton_test` a `ci_gw3_skeleton_test`), todos provisionados por `deploy/postgres/bootstrap.sql`; lock com hashes e `pip check`; `manage.py check` e `makemigrations --check` na primeira fatia e na agenda; migrations como `skeleton_owner`, assertions como `skeleton_runtime`; `tools/smoke_postgres.py` no 3.14. Juntas, as fatias são a suíte inteira (`tests/unit/test_suite_shards.py`). | Um PostgreSQL de produção, um backup/restore ou SSO real. O container de serviço roda sem `fsync`: não serve de prova de durabilidade. |
| `coverage` | Combina os dados de cobertura das duas fatias de cada Python (`coverage combine`) e publica o relatório e o artifact `coverage-postgres-py<versão>`, com o mesmo escopo de antes da divisão: a suíte do núcleo. | Cobertura mede linhas executadas, não a qualidade das assertions. A agenda não entra na medição, como antes. |
| `deploy-check` | `check --deploy` com os settings de produção e valores descartáveis. | Conectividade com banco, Redis ou SMTP. |
| `build` | Wheel do núcleo, `pip-audit` dos dois locks e `docker build`. | Que a imagem sobe em produção. |
| `ci` | Falha se qualquer job acima falhou ou foi cancelado. É o nome estável para exigir na proteção de branch, qualquer que seja a matriz. | Nada além dos outros jobs. |

Quando o job `locks` falha, o log mostra o diff que a regeneração produziu. Regenere os dois locks com os mesmos comandos e a mesma versão do uv e commite o resultado no branch do PR. Com `-o`, o uv usa o lock existente como preferência e não atualiza pacotes de carona. Um falso positivo raro ocorre quando o PyPI acrescenta um wheel a uma versão já travada (hash novo); a correção é a mesma. Nos PRs de pip do Dependabot, o workflow da seção seguinte faz isso sozinho.

### Locks dos PRs do Dependabot

O Dependabot muda só os `.in` ou regenera os locks no estilo `pip-compile`, sem `pip`/`setuptools` e com extras nos nomes, e não tem opção para usar o comando do README. `.github/workflows/dependabot-locks.yml` corrige esses PRs com um commit no branch deles:

1. **`regenerate`**, com `contents: read` e sem secrets: parte dos locks da merge-base do PR e roda `tools/workflow/dependabot_locks.py regenerate`. Ele repete os dois `uv pip compile` do README (uv 0.12.7) com `--upgrade-package` restrito ao que o PR mudou: a versão de cada pacote alterado nos locks e o nome de cada linha nova dos `.in`. `--no-build` impede o compile de executar código de build de sdists, e `--custom-compile-command` grava o cabeçalho oficial, de modo que o job `locks` não vê diff. Os locks vão para um artifact.
2. **`commit`**, só quando há diff: valida o artifact com o script da **base** (`check-artifact`: só os dois arquivos, cabeçalho oficial, LF, toda requisição `pacote==versão` com `--hash=sha256`, sem `--index-url`, `--find-links`, URL direta ou `-e`). Depois copia os dois arquivos para o branch do PR e faz o push com o token de um GitHub App. Esse job não executa código do PR, e não faz push se o branch mudou durante a execução.

O workflow só roda quando o autor do PR e o ator do evento são `dependabot[bot]`, e o branch é deste repositório. Por isso o push do app não redispara a regeneração. O push usa um GitHub App porque um commit feito com o `GITHUB_TOKEN` não dispara o CI, e o PR ficaria sem checks no commit novo.

**Configuração (uma vez, pelo responsável pelo repositório):**

1. Crie um GitHub App com a permissão de repositório **Contents: Read and write** e nenhuma outra, sem webhook, e instale-o só neste repositório.
2. Gere uma chave privada do app.
3. Em *Settings → Secrets and variables → Dependabot*, cadastre `LOCKS_APP_CLIENT_ID` (o Client ID do app) e `LOCKS_APP_PRIVATE_KEY` (o conteúdo do `.pem`). Workflows disparados pelo Dependabot só leem secrets dessa seção.

Sem os secrets, o job `commit` falha com uma mensagem que aponta para esta seção, e o conserto continua manual. O Dependabot para de rebasear um PR que recebeu commit de outro autor; se a `main` mudar os locks antes do merge, comente `@dependabot recreate` no PR: o Dependabot refaz o branch e o workflow regenera os locks de novo. O resultado foi reproduzido com o PR #133: a partir do lock original do Dependabot, `regenerate` produz byte a byte o lock corrigido à mão em #139.

O job `tests` concede `CREATE ON DATABASE` ao owner da fixture, em cada banco, para que `reset_schema()` e `restore_schema()` recriem o schema `skeleton`. As migrations rodam uma vez por worker e cada teste restaura o template (ver [Isolamento da suíte](#isolamento-da-suíte)); `tools/smoke_postgres.py` continua aplicando o `migrate` real. O container do PostgreSQL recebe `fsync=off`, `synchronous_commit=off`, `full_page_writes=off` e `max_locks_per_transaction=2048` por `POSTGRES_INITDB_ARGS`: ele é descartável, e a tabela de locks precisa comportar quatro workers recriando o schema ao mesmo tempo. As senhas são exclusivas do CI e valem só para o container de serviço. SSO real, gov.br e o agente de deployment em host não fazem parte do CI. Para mudar o número de fatias, troque o `N` de `--shard I/N` na matriz; nenhuma lista de arquivos é mantida à mão, então um pacote novo com testes entra numa fatia sozinho.

Fora de Linux/macOS, os testes marcados `posix` (artefatos assinados, que dependem de `os.O_NOFOLLOW`) e os módulos do agente (`fcntl`) são pulados com motivo explícito em vez de falharem na coleta. A fixture de renderização do Compose é pulada quando o CLI `docker` não existe.

## Suíte de testes em menos de 10 minutos (epic #199)

Rodada de 2026-11-11 na branch `test/199-fast-test-suite` ([#196](https://github.com/alaorneto/skeleton/issues/196), [#197](https://github.com/alaorneto/skeleton/issues/197), [#198](https://github.com/alaorneto/skeleton/issues/198)). Windows 11, 16 threads, Python 3.14.7, PostgreSQL 16.2 de `tools/dev_postgres.py`, assertions como `skeleton_runtime`. Somente o que rodou.

| Execução | Antes | Depois |
|---|---|---|
| `pytest tests --cov`, serial | 1282 passed, 111 skipped em 26 min 20 s (1320 s de preparação, 246 s de teste); 15353 de 21219 linhas (69,42%) | não repetida até o fim nesta rodada |
| `pytest tests -n 8` | não existia | 1304 passed, 111 skipped em 3 min 56 s |
| `pytest tests -n 16` | não existia | 1327 passed, 111 skipped em 3 min 53 s |
| `pytest tests -n 8 --cov` | não existia | 1327 passed, 111 skipped em 4 min 12 s; 15369 de 21239 linhas (69,43%) |
| `SKELETON_BUNDLED_APPS=calendar pytest tests/contrib/calendar -n 8` | 4 min no CI, serial | 59 passed em 36 s |
| Job `tests` do CI (run 43028212566) | 36 min (3.12) e 31 min (3.14) | **não medido**: exige um pull request |

Equivalência: os 1393 testes da linha de base aparecem na rodada paralela com o mesmo resultado (comparação dos dois JUnit XML); os 45 a mais são os testes novos de `test_integration_database.py` e `test_suite_shards.py`. Arquivo a arquivo, as linhas executadas só mudaram nos arquivos editados (`skeleton/conf/profiles.py`, `skeleton/system/synchronization.py`, `skeleton/testing/hashers.py`).

`sync_table_catalog` (#197): uma ressincronização sem mudanças caiu de 903 para 215 consultas e grava o mesmo catálogo (638 linhas comparadas com a implementação anterior); `tests/integration/test_table_catalog_sync.py` passa nas duas implementações no teste de reconciliação e só na nova no de número de consultas.

**Não verificado:** o CI com as fatias (primeiro PR), em especial `POSTGRES_INITDB_ARGS` com `-c` no container `postgres:16.15`; os testes `posix` e o Linux.

## Observabilidade (epic #87)

Rodada de 2026-11-11 na branch `feat/87-observability`, um commit por issue. Somente o que rodou.

### Métricas Prometheus (#175)

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, PostgreSQL local de `tools/dev_postgres.py`, assertions como `skeleton_runtime`, `prometheus-client` 0.26.0 | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -k metrics` | 23 passed, 5 skipped (agente sem `fcntl`) |
| mesmo PostgreSQL | `pytest tests/unit/test_observability_metrics.py tests/unit/test_observability_optional.py tests/integration/test_metrics_endpoint.py tests/unit/test_instance_config.py tests/unit/test_security_logging.py tests/integration/test_jobs_operations.py` | 46 passed, 1 skipped (`posix`) |

Cobertura nova:
- `tests/integration/test_metrics_endpoint.py`: sem `SKELETON_METRICS_TOKEN`, `/metrics` é 404; sem cabeçalho, sem token, token errado, esquema `Basic`/`Token` e token com sufixo respondem 401 sem corpo, com `WWW-Authenticate: Bearer` e `no-store`; POST é 405 e HEAD não tem corpo; a resposta não abre sessão nem grava cookie; `skeleton_jobs_due`, `skeleton_jobs_dead`, o atraso e os workers `live`/`stale` batem com `queue_health()` sobre jobs reais (um vencido, um futuro, um esgotado e uma entrega de webhook esgotada) e os heartbeats; o histograma usa o padrão de rota (inclusive em 404 de tenant, que responde antes da resolução de URL), `unresolved` e `health/live/`; **negativo de segurança**: depois de login por email e de requests com sysid, slug e email no caminho e na query, nenhum rótulo de nenhuma série tem UUID, `@`, o slug, o email ou os sysids, e nenhum deles aparece no texto da coleta.
- `tests/unit/test_observability_metrics.py`: o Bearer é comparado com `hmac.compare_digest` sobre digests do mesmo tamanho em todas as tentativas, inclusive as de tamanho diferente; o rótulo de rota é o padrão; o histograma só tem `method`, `route` e `status`; o middleware se remove sem token ou sem o extra; o coletor agrupa entregas por canal, junta tipos não registrados em `other` e ignora estados desconhecidos; uma falha do banco devolve só `skeleton_queue_metrics_up 0`; os checks `skeleton.W030` (token sem extra, token sem middleware) e `skeleton.E030` (token curto); `metrics_token` é segredo do catálogo e recusa tokens curtos ou com espaço.
- `tests/unit/test_observability_optional.py`: num processo novo, sem nenhum setting de observabilidade, `django.setup()`, `check`, `/health/live/` (200) e `/metrics` (404) não importam `prometheus_client`, `opentelemetry` nem `sentry_sdk`.

**Não verificado:** a coleta por um Prometheus real e o modo multiprocess do gunicorn (`PROMETHEUS_MULTIPROC_DIR`) com vários workers.

### Tracing OpenTelemetry (#176)

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, PostgreSQL local de `tools/dev_postgres.py`, assertions como `skeleton_runtime`, `opentelemetry-sdk` 1.45.2 e instrumentações 0.66b2 | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -k tracing` | 15 passed, 5 skipped (agente sem `fcntl`) |
| mesmo PostgreSQL | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -k "job or worker or logging or tracing or observability"` (jobs e logs, pelo `request_id` novo nos logs do worker) | 77 passed, 6 skipped |
| mesmo | `pytest tests/unit/test_observability_optional.py` | 1 passed |

Cobertura nova:
- `tests/integration/test_tracing.py`, com as instrumentações oficiais de Django e psycopg e um exportador em memória atrás do `RedactingSpanExporter`: um GET autenticado gera um único span `SERVER` `GET o/<uuid:organization_id>/` com `skeleton.request_id` igual ao `X-Request-ID`, e todos os spans de SQL são descendentes dele, no mesmo trace e com o mesmo `request_id`; um job executado pelo worker gera o span `CONSUMER` `job test.tracing` com `skeleton.job_id` e `skeleton.job_state`, e os spans de SQL do handler são filhos dele com o `request_id` da execução; um job que falha registra só `error.type`; **negativos de segurança**: o login (consulta por email) e o GET com email, nome e slug na query não deixam email, senha, nome, slug, sysid de organização ou de pessoa, `db.statement`, `db.query.text` nem `http.url` em nenhum span.
- `tests/unit/test_observability_tracing.py`: o tracing só liga com endpoint OTLP e respeita `OTEL_SDK_DISABLED`; sem configuração, `job_span` não faz nada; o exportador descarta texto e parâmetros SQL, usuário e host do banco, URL, alvo, query, user agent, IP, `enduser.id`, cookie e `Authorization`, mantém do evento de exceção só o tipo e tira a descrição do status; nomes de SQL viram a palavra-chave ou `SQL`; o `request_id` só entra em spans iniciados dentro do bloco; o check `skeleton.W030` avisa endpoint sem o extra.

**Não verificado:** o envio a um coletor OTLP real (o exportador OTLP/HTTP não foi exercitado contra rede) e o tracing sob gunicorn com vários workers.

### Sentry (#177)

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, `sentry-sdk` 2.74.0 | `pytest tests/unit/test_observability_sentry.py` e `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -k sentry` | 9 passed (5 skipped do agente sem `fcntl` no `-k`) |
| mesmo, PostgreSQL local de `tools/dev_postgres.py` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -k "sentry or logging or instance_config or observability"` (inclui os testes do `SecurityJSONFormatter`, que passou a usar `safe_fields`) | 46 passed, 6 skipped |

Cobertura nova, em `tests/unit/test_observability_sentry.py`:
- **Negativo de segurança:** um evento sintético com corpo, cookies, `Authorization`, query string com token e email, usuário, IP, user agent, variáveis locais, linhas de código, caminho absoluto, mensagem da exceção, mensagem e parâmetros do log, breadcrumbs de SQL com parâmetros, tags e `extra` arbitrários, threads e contextos sai sem nenhum desses valores; o teste falha se qualquer um sobreviver. Ficam o tipo e os frames (nome do arquivo, linha, função, módulo), o método, o nome do evento, os campos de `safe_fields`, a tag `skeleton.request_id`, os IDs de trace e o runtime.
- O cliente real do `sentry-sdk`, com as opções do núcleo e um transporte em memória (sem rede), captura uma exceção com email e token na mensagem, usuário, tag, `extra` e breadcrumb sensíveis: o envelope enviado não tem nenhum deles, não tem `user` e traz o `request_id` do request em curso.
- Transações com UUID, email ou query são descartadas; a pilha é cortada nos mesmos 25 frames dos logs; `send_default_pii=False`, corpo nunca lido, sem variáveis locais nem código-fonte, transações descartadas; sem DSN ou sem o extra, `sentry_sdk.init` não é chamado, e com eles é chamado uma vez; `skeleton.W030` para DSN sem extra; `sentry_dsn` é segredo do catálogo e recusa DSN sem chave ou sem projeto.

**Não verificado:** o envio a um projeto Sentry real e a integração do SDK com o Django num request que falha (o `before_send` foi exercitado com eventos sintéticos e com o cliente real fora do Django).

### Verificação conjunta do epic

Com os três commits na branch, no mesmo PostgreSQL local:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, PostgreSQL local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` | 1282 passed, 111 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) em 40 min |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (com `skeleton/observability` no modo estrito), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |
| Windows | `uv 0.12.7 pip compile` dos dois locks (comandos do `README.md`) e `pip-audit 2.9.0 --strict --require-hashes --disable-pip` de `requirements.txt` e `requirements-dev.txt` | locks regenerados; nenhuma vulnerabilidade conhecida |

## Operação do worker de jobs (#40, epic #77)

Rodada de 2026-11-11 na branch `feat/40-worker-operations`, um commit para [#40](https://github.com/alaorneto/skeleton/issues/40). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` | 1172 passed, 111 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) |
| mesmo PostgreSQL | `python -m pytest tests/integration/test_jobs_operations.py` | 9 passed |
| Windows, com `fcntl`, `os.O_DIRECTORY` e `UnixStreamServer` substituídos só no processo do teste | os três testes de backend de `tests/unit/test_agent_worker_lifecycle.py` | 3 passed |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `npm run check` em `frontend/` | sem problemas, sem migrations pendentes, assets atualizados |

Cobertura nova:
- `tests/integration/test_jobs_operations.py`: a lista de `/platform/jobs/` mostra tipo, organização e código de erro e nunca o payload; filtro inválido é ignorado; anônimo vai ao login e quem não é da plataforma recebe 403 (GET e POST); GET na ação é 405 e POST sem CSRF é 403, sem mudar o job; reenviar zera as tentativas uma vez (o segundo envio com o `updated_at` antigo não muda nada, e o serviço levanta `ConcurrentUpdate`); uma mudança concorrente vence um reenvio desatualizado; descartar deixa `cancelled`/`discarded` e um job na fila não é acionável; sem autenticação recente a ação vai à reautenticação; a retenção, pelo job registrado executado pelo worker com a role runtime, apaga só `succeeded`/`cancelled` acima de 7 dias e `dead` acima de 30, nunca `queued`/`running`, e os heartbeats `stopped` antigos; `check_jobs` falha por atraso, por esgotados e sem worker vivo.
- `tests/unit/test_agent_worker_lifecycle.py`: com um runner falso, o backend de diretório executa `drain` → `worker_stop` e `restart` → `worker_restart` (no release novo), e sem os hooks o comportamento antigo; o backend Compose gera o override do `worker`/`scheduler` com a imagem do release, sem trocar o comando, para os workers depois do web e os recria depois do web, e recusa `MIGRATION_DATABASE_URL` num worker; a configuração recusa `worker_stop` sem `worker_restart`, comando relativo e `worker_services` inválidos. Os testes de configuração só rodam em Linux/macOS (CI).
- `tests/unit/test_deployment_journal.py::test_execution_stops_workers_before_migrations_and_restarts_them_after_cutover`: num plano aprovado, `drain-web` → `stop-worker` → `migrate` → `cutover` → `restart-web` → `start-worker` → health. Só roda em Linux/macOS (CI).

**Não verificado:** os testes do agente que exigem `fcntl` nesta máquina (rodam no CI), `docker compose up` com os serviços `worker` e `scheduler`, as units systemd num host real, smoke de `/platform/jobs/` no navegador (o template é renderizado e conferido pelos testes de integração) e o PostgreSQL 16.15 do CI.

## Só PostgreSQL, setup e configuração da instância (epic #150)

Rodada de 2026-11-11 na branch do epic [#150](https://github.com/alaorneto/skeleton/issues/150), um commit por issue ([#144](https://github.com/alaorneto/skeleton/issues/144) a [#149](https://github.com/alaorneto/skeleton/issues/149)) e um de correções da suíte. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` (com as variáveis de `tools/dev_postgres.py env`) | 1093 passed, 110 skipped (marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows, PostgreSQL 16.2 local (outro servidor) | a mesma suíte numa branch que junta `main` (com #158), #159, #160 e esta | 1160 passed, 110 skipped, 3 failed (os três corrigidos depois nesta branch: o teste de reset desativava como sistema, sem o plano de plataforma que o guard PostgreSQL exige; o scaffold desligava o app de exemplo) e 25 erros da suíte SSO real sem Keycloak. Depois da correção, `test_flows.py`, `test_scaffold_end_to_end.py`, `test_setup.py` e `test_platform_instance.py` na junção: 18 passed |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING` (settings de teste e `config.settings.setup`), `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |
| navegador (painel do app) | `runserver` com SQLite da instância vazio | redirecionamento de qualquer rota para `/setup/`, token impresso uma vez (o processo filho do autoreload não emitiu outro), token aceito e páginas das etapas. O resto do fluxo não pôde ser clicado (painel oculto) e está coberto por `test_setup.py` |

Cobertura nova: `tests/unit/test_postgresql_only.py` (E025 com outro backend, `DATABASES` vazio fora e dentro do setup; migration recusada fora do PostgreSQL), `tests/unit/test_setup_mode.py` (token emitido uma vez, 0600, só o digest no SQLite; token errado, reutilizado e expirado; troca de modo e `SKELETON_SETUP=off`; URLs não PostgreSQL), `tests/integration/test_setup.py` (qualquer rota leva ao setup, CSRF, token de uso único, rate limit 429, DSN privilegiada recusada com mensagem de `BYPASSRLS`, setup completo contra o PostgreSQL de teste sem a senha do owner no SQLite, no log nem na sessão assinada, `/setup/` 404 depois de concluir e administrador criado), `tests/integration/test_platform_instance.py` (anônimo, não plataforma, segredo ausente do HTML, do log e do `change_log`, validação, chave do ambiente somente leitura com 409, autenticação recente), `tests/unit/test_container_healthcheck.py` (Host do healthcheck nos dois modos) e `tests/unit/test_agent_instance_configuration.py` (URL runtime do agente pelo SQLite da instância; só roda em Linux/macOS).

**Não verificado:** `docker compose up` de uma instalação nova até o setup e o reinício pelo Compose (sem Docker nesta máquina; os testes de renderização do Compose rodam só no CI), o agente de deployment (exige `fcntl`), o reinício por `SIGHUP` do Gunicorn, a suíte SSO real e o PostgreSQL 16.15 do CI.
## Eventos do núcleo para vínculos e ativação de apps (#43, epic #78)

Rodada de 2026-11-11 na branch `feat/43-core-lifecycle-events`, um commit para [#43](https://github.com/alaorneto/skeleton/issues/43). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` | 1170 passed, 110 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) |
| mesmo PostgreSQL | `python -m pytest tests/integration/test_core_lifecycle_events.py` | 7 passed |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

A primeira rodada completa teve 1 falha e 33 erros: o fan-out de webhooks (consumidor `"*"`) enfileirava um job de entrega inócuo para cada evento de vínculo, e os testes que esvaziam o registro de jobs ou de eventos quebravam no bootstrap. A correção foi o filtro `accepts` dos consumidores `"*"` (o fan-out aceita só tipos `webhook=True`) e manter os tipos do núcleo nos fixtures desses testes; a rodada acima é a de depois da correção.

Cobertura nova em `tests/integration/test_core_lifecycle_events.py`: os quatro tipos declarados com payload só de identificadores e fora dos webhooks; `update_membership`, `delete_membership`, `create_membership` (novo, restaurado, restaurado inativo, já ativo) publicam um evento por mudança efetiva e nada sem efeito; `provision_membership` e `leave_organization`; criação e desativação (repetida) de conta de serviço; `configure_application` publica só quando `enabled` muda (configuração sozinha não publica); um rollback do chamador não deixa evento, e a organização B não lê os eventos de A (RLS); os tipos do núcleo não geram job de entrega.

**Não verificado:** o PostgreSQL 16.15 do CI.

## PostgreSQL local de desenvolvimento (#145)

Toda a suíte roda em PostgreSQL, como no CI. `tools/dev_postgres.py` sobe um servidor descartável sem Docker:

```sh
python tools/dev_postgres.py start                 # inicializa (na primeira vez), sobe e provisiona
eval "$(python tools/dev_postgres.py env)"          # POSIX; no PowerShell: python tools/dev_postgres.py env --shell powershell | Invoke-Expression
SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -n auto
python tools/dev_postgres.py stop
```

O servidor vem do `pgserver` (binários do PostgreSQL num wheel). Como o `pgserver` só publica wheels para CPython 3.12, o script o executa pelo `uv` num ambiente isolado com Python 3.12, fora do virtualenv do projeto, e nada entra nos locks. Os dados ficam em `var/dev-postgres` (ignorado pelo Git). O banco `local_skeleton_test` é provisionado com `deploy/postgres/bootstrap.sql` e senhas só locais, no mesmo layout do CI. O `start` também cria um banco por worker paralelo (`local_gw0_skeleton_test`, `local_gw1_...`; um por CPU, ou `--workers N`), sem mexer nos que já existem, e ajusta o servidor: `fsync`, `synchronous_commit` e `full_page_writes` desligados e `max_locks_per_transaction=2048` (reinicia o servidor uma vez, na mesma porta). Por isso esse servidor nunca deve guardar dado que importe. O script também copia os dados de fuso do pacote `tzdata` quando o build do `pgserver` vem sem eles, e exporta `SKELETON_TEST_PG_DUMP` para o template de schema.

Duas execuções da suíte não podem usar os mesmos bancos ao mesmo tempo: cada teste recria o schema `skeleton` do banco do seu worker. A suíte de SSO real (`tests/sso`, Keycloak e navegador) só é coletada quando `SSO_FIXTURE_ENV_PATH` está definido; sem isso, `pytest tests` a deixa de fora.

### Rodar rápido e rodar completo

| Para | Comando | Tempo |
|---|---|---|
| O arquivo ou teste em que você está trabalhando | `python -m pytest tests/integration/test_x.py -q -x` (`-k nome` escolhe um teste; `-x` para na primeira falha) | segundos: a partida (Django e template do schema) mais 1 a 2 s por teste com banco |
| Só o que falhou na última vez | `python -m pytest tests -q -n auto --lf` | proporcional ao que falhou |
| Testes sem banco | `python -m pytest tests/unit -q -n auto` | menos de 1 minuto |
| Suíte inteira, obrigatória antes do PR | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -n auto` | cerca de 4 minutos |
| Suíte inteira com cobertura, como no CI | o mesmo, com `--cov` | cerca de 4 minutos |
| Agenda | `SKELETON_BUNDLED_APPS=calendar python -m pytest tests/contrib/calendar -q -n auto` | menos de 1 minuto |

Sem `-n`, a suíte roda num processo e num banco, como antes: é o modo para depurar com `--pdb` ou ler a saída de um teste. O comando rápido não substitui a suíte inteira, porque não prova que a mudança deixou os outros módulos intactos.

**Como a paralelização funciona.** `-n` é do `pytest-xdist` (lock de desenvolvimento): N processos, cada um com uma parte dos testes. Cada processo usa um banco só seu, com nome derivado das duas URLs de teste (`local_skeleton_test` vira `local_gw3_skeleton_test` no worker `gw3`), migra uma vez e restaura o template antes de cada teste, exatamente como no modo serial. O `tests/conftest.py` troca as duas variáveis de ambiente no próprio worker; assim os settings, `disposable_database_pair()`, os testes que leem as variáveis e os subprocessos que eles iniciam enxergam o mesmo banco. Os fixtures continuam sem criar banco nem role.

Antes de iniciar os workers, a suíte confere duas coisas: se o owner abre o banco de cada worker e se a tabela de locks do servidor comporta as restaurações simultâneas. Cada restauração segura cerca de 2.100 locks até o commit, e o padrão do PostgreSQL (6.400 no total) só aguenta dois workers. Se faltar algo, a suíte para com um erro de uso que diz o que provisionar. Num PostgreSQL que não seja o de `tools/dev_postgres.py`, crie os bancos `<prefixo>_gwN_skeleton_test` como o banco base (bootstrap, senhas, `GRANT CREATE ON DATABASE`) e aumente `max_locks_per_transaction`.

`--shard I/N` (`tools/suite_shards.py`) roda só uma fatia dos arquivos de teste. Serve ao CI; localmente, `-n` basta.

Os tempos medidos estão em [Suíte de testes em menos de 10 minutos](#suíte-de-testes-em-menos-de-10-minutos-epic-199).

**Rodadas abaixo de [#144](https://github.com/alaorneto/skeleton/issues/144):** as seções seguintes registram a história anterior ao modelo só PostgreSQL e citam a suíte SQLite que existia então (`tests/integration_sqlite`, hoje `tests/integration`). Elas continuam como evidência do que rodou naquela época, não como procedimento.

## Tokens de API e contas de serviço (epic #79)

Rodada de 2026-11-11 na branch `feat/79-api-tokens`, um commit por issue: [#45](https://github.com/alaorneto/skeleton/issues/45) (token, `sk_integrations.0004`), [#46](https://github.com/alaorneto/skeleton/issues/46) (Bearer em `/api/v1/`), [#47](https://github.com/alaorneto/skeleton/issues/47) (telas e REST) e [#48](https://github.com/alaorneto/skeleton/issues/48) (limites e uso, `sk_integrations.0005`). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` | 1194 passed, 110 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) |
| mesmo PostgreSQL | os sete arquivos de tokens (abaixo) | 31 passed |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `npm run check` em `frontend/` | sem problemas, sem migrations pendentes, assets atualizados |

Cobertura nova:
- `tests/integration/test_api_tokens.py` e `tests/unit/test_api_token_format.py` (#45): só prefixo e digest persistidos, segredo e digest ausentes da auditoria global (lida como administrador da plataforma, por causa da RLS); escopos vazios, desconhecidos, de tabela não exposta, de ação não aceita, nomeado não registrado ou que não seja lista recusados; validade ausente, passada, sem fuso ou acima do limite recusada; revogação (com CAS), `auth_version`, expiração (relógio simulado), segredo errado e texto malformado invalidam; outra pessoa e outra organização recebem "inexistente"; sem `integrations.manage` não há token de serviço; contas de serviço não emitem token pessoal; política de MFA.
- `tests/integration/test_api_token_auth.py` e `tests/unit/test_api_token_header.py` (#46): token sem CSRF cria registro e a sessão continua exigindo CSRF; sessão + Bearer é 400; escopo de leitura não cria e escopo de criação não lê, mesmo com o papel; em B o papel de Alice não tem permissão e o escopo não a cria; não membro recebe 404; `PATCH me`, vínculos, auditoria e anexos sem `files:read` são 403; token de conta de serviço em outra organização é 404; revogado, `auth_version` mudado, desconhecido e lixo são 401 com `WWW-Authenticate` e evento sem o token; rotas HTML ignoram o cabeçalho; a auditoria grava o usuário do token e `http:token:<prefixo>`.
- `tests/integration/test_api_token_management.py` (#47): a página mostra o token uma vez com `no-store` e depois só o prefixo; validade e escopo inválidos são 400; Bob não revoga nem vê os tokens de Alice (404); a REST emite, lista sem segredo e revoga com `If-Match` (428 sem ele); um token recebe 403 nos endpoints de token; contas de serviço de A são 404 para quem não tem `integrations.manage` e pela URL de B; desativar a conta invalida o token.
- `tests/integration/test_api_token_limits.py` e `tests/unit/test_ratelimit.py` (#48): acima do limite do token, 429 com `Retry-After` e nada gravado, e outro token segue livre; falhas de um IP bloqueiam qualquer Bearer desse IP e não de outro; `last_used_at` grava uma vez por janela, mesmo sem o cache, e não gera `AuditEvent`; janela fixa e cache fora do ar deixam passar.

Depois da rodada completa, o trabalho da agenda (#76) achou que um token de feed autenticava `GET /api/v1/me/` como Bearer; o middleware passou a recusá-lo (`6794c1d`, com `test_feed_tokens_are_not_api_credentials`). Rodado depois da correção: `test_api_token_auth.py` e `test_api_tokens.py`, 20 passed; a suíte completa não foi repetida.

**Não verificado:** clientes reais (curl com um token emitido pela página), Redis compartilhado entre vários workers (os testes usam o cache local), smoke das páginas no navegador e o PostgreSQL 16.15 do CI.

## Arquivos: cota por organização, antivírus e órfãos (epic #81)

Rodada de 2026-11-10 na branch do epic [#81](https://github.com/alaorneto/skeleton/issues/81), com um commit por issue: [#61](https://github.com/alaorneto/skeleton/issues/61) (cota, `sk_files.0002`) e [#62](https://github.com/alaorneto/skeleton/issues/62) (antivírus e órfãos). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 1019 passed, 112 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI, com `test_storage_quota.py`, `test_file_scanning_jobs.py` e `tests/integration_postgres/test_storage_quota_concurrency.py` | 392 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (31 arquivos), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova:
- `tests/integration_postgres/test_storage_quota_concurrency.py`: duas reservas simultâneas que juntas passam da cota, na primeira reserva e com contador existente: uma vence, a outra recebe `QuotaExceeded`, o contador fica certo e o de A é invisível para B (RLS).
- `tests/integration_sqlite/test_storage_quota.py`: reserva e liberação pelo upload e pela remoção, upload recusado não conta, restaurar acima da cota responde 409 `quota_exceeded`, contador próprio por organização; upload HTML acima da cota é uma página 409; a recontagem corrige uma divergência plantada; a página da plataforma mostra uso e cota (403/404 para quem não é plataforma), limite sem valor é 400, a cota própria vence o padrão, formulário sem a revisão da linha existente é 409 e voltar ao padrão remove a linha.
- `tests/unit/test_storage_quota_form.py`: MiB para bytes e os três modos.
- `tests/unit/test_file_scanning.py`: respostas do `clamd` (OK, FOUND, erro), o protocolo INSTREAM contra um `clamd` falso em TCP (EICAR em quarentena, arquivo limpo em vários blocos) e `clamd` inacessível.
- `tests/integration_sqlite/test_file_scanning_jobs.py`: com scanner, o anexo nasce `pending` e o download fica bloqueado; limpo vira `available`, EICAR vira `quarantined` com a assinatura; scanner fora deixa `pending`, o job volta à fila com `ScannerUnavailable` e o download continua 404; sem scanner, `available` na hora; o objeto de um upload revertido é apagado depois da carência, e os de linhas vivas e excluídas logicamente ficam.

**Não verificado:** um `clamd` real (a suíte usa um `clamd` falso e um scanner falso), armazenamento S3, o `run_worker` real, smoke manual no navegador da página da plataforma e o PostgreSQL 16.15 do CI.

## Apps first-party no wheel do núcleo (epic #83)

Rodada de 2026-11-11 na branch `feat/83-bundled-apps`: [#66](https://github.com/alaorneto/skeleton/issues/66) (contrato, mais duas correções) e a parte de estado de [#67](https://github.com/alaorneto/skeleton/issues/67). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q`, no commit `2735c0c` | 1179 passed, 110 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker). O commit seguinte só troca o namespace das rotas `/me/apps/`, que não existem sem app first-party |
| mesmo PostgreSQL | `tests/unit/test_bundled_apps.py`, `tests/integration/test_bundled_lifecycle.py`, `tests/unit/test_presentation.py`, `tests/unit/test_packages.py` | 12, 4 e 137 passed (97 skipped `posix`) |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

A primeira rodada completa teve 1 falha (`test_presentation.py::test_v2_rejects_unsafe_specs_before_import[old-core]`): a validação de first-party tinha capturado a checagem do piso do core do manifesto v2. Corrigido em `2735c0c`; a rodada acima é a de depois.

Cobertura nova:
- `tests/unit/test_bundled_apps.py` (#66): manifesto `bundled_app` válido; fora de `skeleton.contrib`, sem o prefixo `skeleton_`, v1 e com arquivos próprios recusados; app de terceiro com `skeleton_*` ou `runtime.user_urls` recusada; manifest fora do pacote instalado recusado; versão divergente para o startup; o namespace `skeleton.contrib` é só de first-party; opt-in só por nome do mapa fixo; a suíte roda sem app first-party; o núcleo nunca importa `skeleton.contrib` (análise `ast`).
- `tests/integration/test_bundled_lifecycle.py` (#67, estado): app nova registrada `kind=bundled` com disponibilidade `selected` e nenhuma ativação; retirada sem declaração é divergência; com `SKELETON_BUNDLED_RETAINED` vira `schema_retained` e desativa as organizações; reinstalada volta a `bundled` com a mesma linha e configuração; instalada sem registro é divergência; nomes retidos vêm do mapa fixo.

A retirada com tabelas de verdade (schema retido validado pelo snapshot do wheel) exige uma app first-party com tabelas, a agenda; ela é verificada no PR do epic #84, que fecha o #67.

**Não verificado:** o PostgreSQL 16.15 do CI e um deployment real com `SKELETON_BUNDLED_APPS`.

## Notificações: preferências, email, resumo, push e retenção (epic #80)

Rodada de 2026-11-10 na branch do epic [#80](https://github.com/alaorneto/skeleton/issues/80), com um commit por issue: [#53](https://github.com/alaorneto/skeleton/issues/53) (preferências, `sk_notifications.0003`), [#54](https://github.com/alaorneto/skeleton/issues/54) (email imediato, `NotificationDelivery`, `0004`), [#55](https://github.com/alaorneto/skeleton/issues/55) (resumo, `0005`), [#56](https://github.com/alaorneto/skeleton/issues/56) (web push, `WebPushSubscription`, `0006`) e [#57](https://github.com/alaorneto/skeleton/issues/57) (retenção). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 1052 passed, 112 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI, com os seis arquivos novos de notificação | 407 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (31 arquivos), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova:
- `tests/unit/test_notification_preference_resolution.py`: linha do tipo > linha `*` > padrão do tipo; tipo obrigatório sempre in-app.
- `tests/integration_sqlite/test_notification_preferences.py`: sem linha valem os padrões; a API cria, altera com `If-Match` (428 sem ele, 412 com revisão antiga) e exclui logicamente; desligar in-app de tipo obrigatório, modo inválido, hora fora de 0–23 e campo `user` no corpo respondem 400; tipo ou canal desconhecido 404; anônimo 403; a página salva sem JavaScript, mostra o tipo obrigatório travado, responde 409 a um formulário com revisão antiga sem gravar, 400 a campo estranho e 403 sem CSRF; outra pessoa não vê a linha alheia.
- `tests/unit/test_notification_email_render.py` e `tests/integration_sqlite/test_notification_email.py`: email em inglês para perfil `en` com hora no fuso (12:00 UTC → 09:00 em São Paulo) e em português por padrão; `List-Unsubscribe`; nada sai na transação do `notify()`; entrega `sent` não sai de novo; opt-out antes do fan-out não cria entrega; opt-out e desativação depois do fan-out viram `skipped` (`opted_out`, `recipient_inactive`); SMTP recusando vira cinco tentativas e `dead`; rollback não envia; entregas de A não aparecem em B.
- `tests/unit/test_notification_digest_schedule.py` e `tests/integration_sqlite/test_notification_digest.py`: hora local do resumo (diário e semanal às segundas, inclusive com virada de dia em Tóquio); a mesma janela não duplica; lidas ficam fora; resumo vazio não é criado; itens lidos entre a criação e o envio viram `skipped` (`empty_digest`); um resumo por organização; tipos imediatos fora do resumo.
- `tests/unit/test_web_push_encoding.py`: o vetor de teste do apêndice A da RFC 8291 byte a byte; ida e volta com chaves aleatórias; allowlist (HTTP, porta, credenciais na URL, IP, host parecido e subdomínio); JWT VAPID ES256 verificado com a chave pública.
- `tests/integration_sqlite/test_web_push.py`: assinatura exige sessão e CSRF e recusa metadados de nuvem, host fora da lista e chaves inválidas; endpoint cifrado no banco; o mesmo navegador com outra pessoa move a assinatura; payload decifrado pelo "navegador" só com rótulo e link (o snapshot só com `push_snapshot`); 410 exclui logicamente e não reenvia; 503 retentado até `dead`; seção de push oculta sem JavaScript e ausente sem chave VAPID, `/sw.js` 404 sem configuração.
- `tests/integration_sqlite/test_notification_retention.py`: prazos por setting; só linhas vencidas saem e o contador continua igual às não lidas restantes; outra organização intocada; entregas `retrying` ficam; notificações órfãs saem só depois das linhas e entregas.

**Não verificado:** envio real por SMTP, push real a um serviço do navegador (a suíte usa um serviço falso no lugar do `outbound_http`), o `run_worker` real, smoke manual no navegador (opt-in de push e página de preferências) e o PostgreSQL 16.15 do CI.

## Agenda, app first-party (epic #84)

Rodada de 2026-11-11 na branch `feat/84-calendar`, que mescla as branches dos epics #79 e #83; um commit por issue ([#74](https://github.com/alaorneto/skeleton/issues/74), [#68](https://github.com/alaorneto/skeleton/issues/68) a [#73](https://github.com/alaorneto/skeleton/issues/73), [#111](https://github.com/alaorneto/skeleton/issues/111), [#71](https://github.com/alaorneto/skeleton/issues/71), [#75](https://github.com/alaorneto/skeleton/issues/75), [#76](https://github.com/alaorneto/skeleton/issues/76) e o fechamento de [#67](https://github.com/alaorneto/skeleton/issues/67)). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, PostgreSQL 16.2 local de `tools/dev_postgres.py`, assertions como `skeleton_runtime` | `SKELETON_BUNDLED_APPS="" SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q` (núcleo, sem a agenda) | 1218 passed, 110 skipped (os marcados `posix`, os do agente que exigem `fcntl` e os que exigem Docker) |
| mesmo PostgreSQL | `SKELETON_BUNDLED_APPS=calendar python -m pytest tests/contrib/calendar -q` | 59 passed |
| Windows | `manage.py check --fail-level WARNING` e `makemigrations --check --dry-run`, com e sem `SKELETON_BUNDLED_APPS=calendar`; Ruff 0.17.4 `check .` e `format --check .`; `python -m mypy`; `npm run check`; `uv 0.12.7 pip compile` dos dois locks | sem problemas, sem migrations pendentes, assets e locks sem diff |

Cobertura nova:
- `tests/integration/test_aggregate.py` (#74): cada organização com a própria autorização e RLS (contagem pelo manager sem filtro), escrita no callback falha (`READ ONLY`), resultado preguiçoso recusado, vínculo inativo, app desativada e negação pulam a organização, limite e aninhamento, rota marcada fora da transação do middleware.
- `tests/contrib/calendar/test_calendars.py` (#68): um calendário pessoal padrão (restrição no banco e corrida que lê o vencedor), visibilidade travada (400 pela gestão de acesso), padrão não excluível, gateway cria compartilhado, fuso inválido 400, sem TablePermission 404, B isolada, ação de admin.
- `test_events.py` (#69): leitura sem update lê e não cria, privado é "Ocupado", admin vê só ocupado e não escreve no pessoal alheio, horários e dia inteiro, CAS e `sequence`.
- `test_recurrence.py` (#70): 09:00 locais através do horário de verão em Europe/Berlin (2026) e America/Sao_Paulo (2018), EXDATE e substituição, regras fora do subconjunto (inclusive `FREQ=SECONDLY`) recusadas, truncamento sinalizado, divisão "esta e as seguintes" preservando IDs.
- `test_attendees.py` (#72), `test_reminders.py` (#73), `test_service_api.py` (#111, kit de contrato), `test_views.py` (#71), `test_my_agenda.py` (#75), `test_ics_feed.py` (#76: parser `icalendar` com RRULE, EXDATE e RECURRENCE-ID, feed com a autorização atual, 404 para token revogado, expirado ou desconhecido, token de feed recusado na API) e `test_retention.py` (#67: snapshot do wheel igual aos modelos; retirada sem declaração reprovada pelo `check_skeleton_schema`; com `SKELETON_BUNDLED_RETAINED` as tabelas passam no check, as organizações ficam desativadas e os dados ficam; reinstalação com os mesmos UUIDs).

**Não verificado:** clientes de calendário reais assinando o feed, smoke das telas no navegador, um deployment real com `SKELETON_BUNDLED_APPS=calendar` e o PostgreSQL 16.15 do CI.

## Superfícies do núcleo sobre a API de serviços (#110)

Rodada de 2026-11-10 para [#110](https://github.com/alaorneto/skeleton/issues/110), que fecha o epic [#113](https://github.com/alaorneto/skeleton/issues/113): a auditoria (`/o/<org>/audit/`, a REST `/api/v1/o/<org>/audit/` com o filtro por registro e `/platform/audit/`) passou a usar o `AuditLogService`, com a paginação numerada e os campos publicados. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 1015 passed, 112 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI, com `test_core_audit_log_service.py` | 391 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (31 arquivos), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova em `tests/integration_sqlite/test_core_audit_log_service.py` (SQLite e PostgreSQL): contrato do serviço; a lista é filtrada antes de contar (membro com `audit` vê só os eventos dos registros que lê, sem o registro privado do administrador; colunas sensíveis redigidas); `denied` (sem `TablePermission`) conta zero e recebe `NotFound` no detalhe; páginas numeradas sem repetição, página além do fim vazia e entradas inválidas (`page`, `page_size`, `record_id`, `model_label`) como `InvalidInput`; ator divergente recusado; eventos de B nunca aparecem para o administrador de A, nem pelo identificador; sem organização, quem não administra a plataforma recebe `Forbidden`. Em `test_core_surfaces_on_services.py`, `AuditView`, a página de auditoria e a auditoria da plataforma entraram na lista das superfícies que só chamam serviços. A suíte HTTP existente (`test_audit_findings.py`, `test_http_boundaries.py`, `test_platform_boundary.py` e demais) passou sem mudar expectativas.

**Não verificado:** smoke manual no navegador das páginas de auditoria; o PostgreSQL 16.15 do CI. A primeira rodada PostgreSQL desta branch teve 5 falhas em `test_core_surfaces_on_services.py` porque os arquivos foram editados com a suíte em execução (`inspect.getsource` leu linhas deslocadas); a rodada da tabela, depois das edições, passou inteira.
## Rodada combinada: instância, webhooks e sequências

Rodada de 2026-11-10 na branch que junta #143, #49, #44, #63, #64, #65, #155 e #156 (um commit por issue e o ajuste final do catálogo da instância e do mapa de pacotes). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 1006 passed, 112 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI, com `test_app_activation_upgrade.py`, `test_sequences.py`, `test_sequence_fields.py`, `test_service_accounts.py`, `test_webhook_endpoints.py`, `test_webhook_delivery.py` e `tests/integration_postgres/test_sequence_concurrency.py` | 382 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido com as quatro tabelas novas; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (31 arquivos), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `npm run check` | sem problemas, sem migrations pendentes |

As falhas de teste da #44 e da #63 (seções abaixo) não se repetem aqui: a lista inteira passou com as correções.

**Não verificado:** smokes manuais no navegador, entrega de webhook a um destino HTTPS público real, o `run_worker` real, os testes `posix` e o PostgreSQL 16.15 do CI.

## Log, reenvio e teste de webhooks (#65)

Rodada de 2026-11-10 para [#65](https://github.com/alaorneto/skeleton/issues/65): log de entregas (HTML e API), reenvio com `resend_of` (`sk_integrations.0003`), `webhook.ping` e o job periódico `webhooks.retention`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 977 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI | 369 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `npm run build` e `npm run check` | sem problemas, sem migrations pendentes; o Tailwind passou a ler `skeleton/integrations/templates` |

Cobertura nova em `tests/integration_sqlite/test_webhook_delivery.py` (SQLite e PostgreSQL):
- o log guarda e mostra no máximo 1 KB da resposta, escapado na página e igual na API;
- o reenvio exige `If-Match` do endpoint (428/412; no HTML, revisão antiga não cria nada), cria uma entrega nova com `resend_of`, o mesmo evento e o mesmo corpo, e a original continua `failed` com a mesma tentativa e o mesmo status;
- o `webhook.ping` chega assinado, com `data: {}` e sem registro;
- membro sem a capacidade e administrador de B recebem 404 na página, na lista, no detalhe e no reenvio, inclusive pela URL de B;
- a retenção apaga só a entrega final expirada, mantém a que tem retentativa agendada e a original de um reenvio vivo.

**Não verificado:** smoke manual no navegador da página de entregas; o PostgreSQL 16.15 do CI.

## Entrega assinada de webhooks (#64)

Rodada de 2026-11-10 para [#64](https://github.com/alaorneto/skeleton/issues/64): `WebhookDelivery` e `WebhookEndpointHealth` (`sk_integrations.0002`, operacionais), o consumidor `webhooks.fanout`, o job `webhooks.deliver` e a notificação `webhooks.endpoint_disabled`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 972 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `test_webhook_delivery.py`) | 364 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova:
- `tests/unit/test_webhook_signature.py`: o vetor de teste publicado em `docs/webhooks.md`; duas assinaturas na rotação, a atual primeiro; agenda de retentativas (1 min a 24 h, nada depois da sétima); 5xx/408/429/timeout retentáveis e os demais 4xx não; trecho de resposta limitado a 1 KB e sem caracteres de controle.
- `tests/integration_sqlite/test_webhook_delivery.py` (SQLite e PostgreSQL, com um receptor HTTP local pelo override de desenvolvimento):
  - entrega assinada e verificável com o segredo, corpo só com identificadores (nenhum campo da nota), `Skeleton-Webhook-Id` igual à entrega e consumidor idempotente;
  - 503 é retentado em cerca de 1 minuto e entregue na segunda tentativa; 400 falha sem retentativa;
  - principal que não lê a nota privada gera entrega `unauthorized` e nada chega ao receptor;
  - evento não inscrito ou não exposto não cria entrega;
  - a rotação envia duas assinaturas, ambas válidas;
  - 15 falhas seguidas desativam o endpoint, notificam os administradores, e o próximo evento não é entregue.

**Não verificado:** entrega a um destino HTTPS público real (a suíte usa o override de desenvolvimento com HTTP local), o worker `run_worker` de fato (os testes usam `run_pending_jobs`) e o PostgreSQL 16.15 do CI.

## Endpoints de webhook por organização (#63)

Rodada de 2026-11-10 para [#63](https://github.com/alaorneto/skeleton/issues/63): app `skeleton.integrations` (`sk_integrations.0001`), `WebhookEndpoint` com segredo cifrado (`INTEGRATION_ENCRYPTION_KEYS`), serviços, páginas e API. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 962 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `test_webhook_endpoints.py`) | 357 passed, 1 skipped, 1 failed: a consulta crua do teste rodava fora do tenant, e a RLS escondia as linhas. Com a leitura dentro do contexto da organização, `test_webhook_endpoints.py` repetido no PostgreSQL e no SQLite: 13 passed em cada. A lista inteira não foi repetida |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido (inclui a infraestrutura nova); smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `npm run check` | sem problemas, sem migrations pendentes |

Cobertura nova em `tests/integration_sqlite/test_webhook_endpoints.py` (SQLite e PostgreSQL):
- a criação devolve o segredo uma vez, com `no-store`; a leitura, a lista e a página não o mostram; a coluna guarda só o token Fernet; a auditoria não traz nem o segredo nem o texto cifrado;
- recusas com 400: `http://`, IP privado, nome que resolve para endereço interno, metadados de nuvem, tipo de evento não exposto, desconhecido ou vazio;
- o principal precisa ser conta de serviço ativa da mesma organização (conta de B e pessoa: 400); campo desconhecido é recusado;
- sem `integrations.manage` (gestor de pessoas, membro), lista, detalhe e página respondem 404; B não vê o endpoint de A;
- a rotação exige `If-Match` (428/412), devolve segredo novo e mantém o anterior por 24 horas;
- `PATCH` e `DELETE` usam CAS, e a URL interna também é recusada na alteração;
- as páginas HTML exigem CSRF e autenticação recente (redireciona; na API, 403 `reauthentication_required`).

O módulo `outbound_http` passou a resolver o DNS no momento da chamada, para o resolver poder ser substituído; os 45 testes da #49 continuam passando.

**Não verificado:** smoke manual no navegador; o PostgreSQL 16.15 do CI. `INTEGRATION_ENCRYPTION_KEYS` ainda não está no catálogo da configuração da instância (#143, outra branch).

## Contas de serviço (#44)

Rodada de 2026-11-10 para [#44](https://github.com/alaorneto/skeleton/issues/44): `User.account_type`/`service_organization` (`sk_accounts.0002`, com a infraestrutura do `User` reinstalada), `integrations.manage`, `create_service_account`/`deactivate_service_account`, recusa de login e o diretório. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 949 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `test_service_accounts.py`) | 343 passed, 1 skipped, 1 failed: o teste lia o evento global de auditoria sem a GUC de plataforma, que a RLS exige. Corrigido o teste; depois disso, e do ajuste do diretório, rodaram de novo no PostgreSQL `test_service_accounts.py`, `test_notifications_fanout.py`, `test_core_organization_service.py` e `test_core_identity_service.py`: 23 passed. A lista inteira não foi repetida |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run`, `record_infrastructure sk_accounts` | sem problemas, sem migrations nem infraestrutura pendentes |

Cobertura nova em `tests/integration_sqlite/test_service_accounts.py` (SQLite e PostgreSQL):
- o administrador cria a conta: email `@service.invalid`, senha inutilizável, um vínculo sem papel, auditoria com o administrador como ator, marca nas listas HTML e API;
- login com a senha inutilizável responde igual a um email desconhecido; mesmo com uma senha forjada no banco, o backend recusa, a sessão forçada não vale, o reset não envia email e `pre_login`/`pre_social_login` negam;
- `organization_admin`, `app_manager` e `people_manager` são recusados com 400, direto e por grupo, nos dois sentidos; `member` é aceito;
- o vínculo numa segunda organização é recusado pela API e pelo provisionamento de sistema;
- a desativação revoga os papéis, desativa vínculo e identidade e incrementa `auth_version`; pessoa e conta de outra organização respondem como indisponíveis;
- o gestor de pessoas não cria, desativa nem altera o vínculo da conta, mas continua gerindo pessoas;
- a conta fica fora das listas da plataforma (404 no detalhe), e o CHECK do banco recusa flags de plataforma;
- fica fora do diretório de pessoas e do fan-out de notificações.

**Não verificado:** a lista inteira do PostgreSQL depois das duas últimas correções; SSO real; o PostgreSQL 16.15 do CI.

## Cliente HTTP de saída com proteção contra SSRF (#49)

Rodada de 2026-11-10 para [#49](https://github.com/alaorneto/skeleton/issues/49): `skeleton/common/outbound_http.py`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 941 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (sem teste novo: o cliente não usa banco) | 337 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (com o módulo em modo estrito), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations novas |

Cobertura nova em `tests/unit/test_outbound_http.py` (45 casos):
- tabela de faixas bloqueadas: loopback, `169.254.169.254`, 10/8, 172.16/12, 192.168/16, CGNAT 100.64/10, `0.0.0.0`, multicast, reservados, documentação, `::1`, `::`, link-local e ULA IPv6, `::ffff:127.0.0.1`, NAT64 e 6to4. Endereços públicos IPv4 e IPv6 passam;
- URLs internas são recusadas sem nenhuma tentativa de conexão;
- um nome com qualquer endereço interno é recusado;
- só HTTPS, portas permitidas (configuráveis) e nenhuma credencial na URL;
- um resolver que responde público e depois privado é consultado uma vez, e a conexão vai ao endereço público validado;
- o TLS usa o nome no SNI e no certificado;
- o override de desenvolvimento é recusado com `DEBUG=False`;
- com um receptor HTTP local e o override ligado: redirect 302 devolvido sem ser seguido, corpo truncado em 64 KB, `HTTP_PROXY`/`HTTPS_PROXY`/`ALL_PROXY` ignorados, e exatamente três requisições recebidas.

**Não verificado:** uma requisição HTTPS real a um destino público (a suíte não sai para a internet), deployments atrás de proxy de saída corporativo (sem suporte, documentado) e o PostgreSQL 16.15 do CI.

## Campos de sequência no formulário genérico (#156)

Rodada de 2026-11-10 para [#156](https://github.com/alaorneto/skeleton/issues/156) (epic #89): `TableSpec.sequence_fields`, preenchimento na criação genérica e o check no `skeleton.E028`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 916 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `test_sequence_fields.py`) | 350 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova em `tests/integration_sqlite/test_sequence_fields.py` (SQLite e PostgreSQL), com a spec do Notes trocada no teste para numerar `code`:
- a API e o formulário preenchem o código com números consecutivos (`NT-<ano>-001`, `-002`); o formulário não mostra o campo; B numera de forma independente;
- enviar o código na criação ou no `PATCH` responde 400, sem consumir número nem mudar o código;
- uma criação recusada pela validação, ou por quem não é membro, não consome número;
- o check reprova campo inexistente, campo gravável e sequência não declarada.

**Não verificado:** smoke manual no navegador (o exemplo Notes não liga a sequência por padrão); o PostgreSQL 16.15 do CI.

## Sequências sem lacunas (#155)

Rodada de 2026-11-10 para [#155](https://github.com/alaorneto/skeleton/issues/155) (epic #89): app `skeleton.sequences` (`sk_sequences.0001`), registro em código com o check `skeleton.E028`, `next_number` e `SequenceService`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 912 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `tests/integration_postgres/test_sequence_concurrency.py` e `test_sequences.py`) | 346 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido (inclui a tabela nova); smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations pendentes |

Cobertura nova:
- `tests/unit/test_sequence_registry.py`: declarações válidas; recusas de chave, período, modelo sem `{number}`, campo desconhecido, formato inválido e duplicata; períodos de calendário em UTC.
- `tests/integration_sqlite/test_sequences.py` (SQLite e PostgreSQL):
  - números consecutivos no modelo `AT-{year}-{number:04d}`;
  - um rollback devolve o número, sem lacuna;
  - organizações, anos, meses e sequências sem período são independentes;
  - sem tenant, sequência não declarada e data sem fuso são recusadas sem gravar;
  - os contadores são auditados (criação e incremento) e isolados por organização;
  - `SequenceService` passa no `assert_service_contract`, só a app dona numera, e ator divergente, ator ausente e contexto global são recusados.
- `tests/integration_postgres/test_sequence_concurrency.py`: seis alocações simultâneas recebem números distintos e consecutivos, tanto na criação do contador quanto com o contador existente, sem deadlock; B não vê o contador de A.

**Não verificado:** o PostgreSQL 16.15 do CI.

## Configuração da instância num SQLite próprio (#143)

Rodada de 2026-11-10 para [#143](https://github.com/alaorneto/skeleton/issues/143): `skeleton/instance/` (armazém, catálogo, cifra, resolução), `manage.py instance` e os settings lendo ambiente > SQLite > default. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 905 passed, 112 skipped (marcados `posix`, inclusive o teste de permissão 0600, e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (sem teste novo: o armazém não usa o PostgreSQL) | 337 passed, 1 skipped |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy` (com `skeleton/instance` em modo estrito), `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations novas |
| Windows, SQLite e chave temporários | `instance status`, `set` (com e sem `--stdin`), `set` de chave desconhecida e de valor inválido, `show` com uma variável de ambiente definida, busca da senha nos bytes do SQLite, settings `development` carregados num processo novo | recusas com mensagem clara; `show` com a origem `env` e o segredo mascarado; a senha não aparece no arquivo; os settings usaram os valores gravados |

Cobertura nova em `tests/unit/test_instance_config.py`:
- o catálogo é fechado e valida bool, inteiro, lista, URL e escolha; variáveis de bootstrap e a credencial owner ficam fora dele;
- `set` cifra segredos (a senha e a senha do banco não aparecem nos bytes do SQLite) e `show` os mascara;
- o `change_log` registra chave, ação e origem, nunca o valor;
- `set` recusa chave desconhecida e valor inválido sem gravar;
- o ambiente vence o SQLite, e a origem `env`/`instance`/`default` aparece em `show`;
- sem a chave mestra, ou com outra, a leitura do segredo falha com erro explícito, sem cair para o default;
- `import-env`, `unset`, `export-template` (sem valores) e `status`;
- SQLite e chave em arquivos separados, e criados com 0600 (POSIX);
- **settings de produção carregados num processo novo só a partir do SQLite**, sem `.env` e sem as variáveis do catálogo.

**Não verificado:** o teste de permissão 0600 (pulado no Windows), o PostgreSQL 16.15 do CI e um smoke num checkout limpo em Linux.

## Apps fechadas por padrão e upgrade da ativação (#22)

Rodada de 2026-11-10 para [#22](https://github.com/alaorneto/skeleton/issues/22): disponibilidade e ativação fechadas por padrão, `upgrade_app_activation`, `set_app_availability`, aviso de `check_skeleton_schema` e de `package doctor`, e agente/núcleo 0.5.0. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 881 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI (com `test_app_activation_upgrade.py`) | 316 passed, 1 skipped, 2 failed em `test_schema_template.py`: o fingerprint compara as linhas e a pré-condição de testes já tinha gravado a disponibilidade. Com o módulo marcado `closed_app_defaults`, `tests/integration_postgres/test_schema_template.py` repetido sozinho: 12 passed. A lista inteira não foi repetida depois dessa correção |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations novas |
| SQLite de desenvolvimento novo com o Notes e duas organizações | `check_skeleton_schema`, `upgrade_app_activation --dry-run`, o comando duas vezes, `set_app_availability example_notes --mode selected --enable smoke-a` | aviso de app sem disponibilidade; o dry-run listou e não gravou; a primeira execução criou `all` para o Notes (sem ativações, porque o manifesto do exemplo agora diz `false`), a segunda não fez nada; o CLI deixou o Notes `selected`, concedido e ativo só em `smoke-a`, e o aviso sumiu |

Cobertura nova em `tests/integration_sqlite/test_app_activation_upgrade.py` (SQLite e PostgreSQL, marcado `closed_app_defaults`): sem o upgrade nenhuma app responde (404/403); o upgrade preserva exatamente as apps efetivamente ativas (Notes com default antigo verdadeiro ativo em A, desativado em B por escolha anterior, app com default falso continua desligada), com eventos de `AppInstallation` com `source` `system:upgrade:app-activation`; organização criada depois do upgrade não tem app ativa e repetir o comando não muda nada; app instalada depois nasce `selected` sem concessão e o upgrade não a abre; com inventário do agente, só as apps conhecidas antes viram `all`; interrupção depois de cada fase e falha numa organização retomam sem duplicar linhas (a organização que já tinha confirmado mantém a sua transação); `--dry-run` lista e não grava; linhas do #21 (`upgrade_state` vazio) são processadas uma vez; `set_app_availability` e o aviso do `check_skeleton_schema`. Em `tests/unit/test_package_doctor.py`: `default_enabled: true` gera só o aviso `default_enabled_ignored`. Os testes que dependiam do default aberto passaram a declarar a pré-condição: `skeleton_db` abre e ativa as apps por `skeleton.testing.applications.apps_open_by_default`, e as revisões de ativação vêm de `activation_revision` em vez da revisão virtual 0.

**Não verificado:** os testes do agente (`fcntl`) e de pacotes (`posix`), inclusive o passo novo do `finish()` e a remoção de `activation_default_upgrade_requires_review`; uma atualização real pelo agente; Docker e SSO real; o PostgreSQL 16.15 do CI.

## Saída voluntária da organização e caminhos de ingresso (#25)

Rodada de 2026-11-10 para [#25](https://github.com/alaorneto/skeleton/issues/25): `leave_organization`, `/o/<org>/leave/`, `GET /api/v1/o/<org>/membership/`, `POST /api/v1/o/<org>/membership/leave/`, botão no painel e página inicial em blocos. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 878 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI | 318 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations novas |

Cobertura nova em `tests/integration_sqlite/test_access_lifecycle_regressions.py` (SQLite e PostgreSQL): membro comum com papel direto, grupo e concessão de registro sai, os três vínculos ficam excluídos logicamente e `/o/<org>/` responde 404 na requisição seguinte, sem afetar a organização B onde ele administra; GET da confirmação não altera nada; POST sem CSRF 403, revisão antiga ou ausente 409; o último administrador recebe 409 no HTML e `last_administrator` na API, sem mudança de revisão nem de papéis; a API exige `If-Match` (428), recusa revisão antiga (412), campos no corpo (400), GET (405) e POST sem CSRF (403), e responde 204; administrador direto sai quando outra pessoa administra via grupo, e essa pessoa, agora sozinha, não sai; o serviço recusa ator diferente do contexto; convite aceito depois readmite com o mesmo vínculo; a página inicial sem vínculo mostra os caminhos de ingresso e, com convite aberto na sessão, o bloco "Convite pendente" sem revelar a organização. Em `tests/accounts/test_social_hardening.py`: a confirmação de email do JIT não readmite quem saiu. Em `tests/integration_postgres/test_admin_concurrency.py`: dois administradores saindo ao mesmo tempo serializam no lock da organização (um sai, o outro recebe 409) e a saída concorrente com a remoção do mesmo vínculo pelo administrador termina sem deadlock.

**Não verificado:** smoke manual no navegador; os fluxos de domínio e de link (#26, #28) ainda não existem, então a regra "não readmitir quem saiu" só foi provada para o JIT de SSO; testes `posix`, Docker e SSO real; o PostgreSQL 16.15 do CI.

## Designação de administrador pela plataforma (#20)

Rodada de 2026-11-10 para [#20](https://github.com/alaorneto/skeleton/issues/20): `designate_organization_admin`, `organization_overview`, as razões fechadas de `HTTP_SYSTEM_REASONS` e o card "Administradores" em `/platform/organizations/<id>/`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.13.15, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 875 passed, 111 skipped (marcados `posix` e os que exigem Docker ou PostgreSQL) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI | 315 passed, 1 skipped (o teste do serviço que vaza, que a RLS impede de reproduzir) |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .` e `format --check .`, `python -m mypy`, `manage.py check --fail-level WARNING`, `makemigrations --check --dry-run` | sem problemas, sem migrations novas |

Cobertura nova em `tests/integration_sqlite/test_platform_boundary.py` (SQLite e PostgreSQL): lista fechada das razões aceitas por HTTP e recusa para quem não é administrador de plataforma; designar e substituir pelo formulário, com eventos de `MemberRole` com `source` `system:organization:designate-admin` e o administrador de plataforma como ator; a página mostra só contagem e emails dos administradores, não o membro comum; formulário com versão antiga (409), substituir alguém por ele mesmo (400), designar a si mesmo (403), email sem identidade (400); último administrador protegido (409, sem escrita); GET 405, POST sem CSRF 403, autenticação vencida redireciona para reautenticar, não administrador 403; depois de designar, o administrador de plataforma continua com 404 em `/o/<org>/`. Em `tests/integration_postgres/test_admin_concurrency.py`: a designação sob o contexto de A não lê nem altera linhas de B (RLS com a role runtime), e designação concorrente com a remoção de um administrador serializa no lock da organização, sem deadlock.

**Não verificado:** smoke manual no navegador; testes `posix`, Docker e SSO real; o PostgreSQL 16.15 do CI.

## Django 6.1.3, django-allauth 65.21.1 e psycopg 3.3.8 (#122)

Rodada de 2026-11-09 em Windows 11 com Python 3.14 e os locks regenerados por `uv pip compile`. O venv local foi instalado com as versões do novo `requirements-dev.txt` usadas como constraints (sem `--require-hashes`, porque os hashes são de wheels Linux). Para comparação, os testes que falharam também rodaram num venv com os locks anteriores (Django 5.2.18, allauth 65.19.7): passaram nos dois casos, o que confirma que as falhas eram regressões da atualização e não do ambiente.

| Conjunto executado | Backend | Resultado observado |
|---|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado | 531 passed, 107 skipped (marcados `posix`, agente e os que exigem Docker) |
| `ruff check`, `ruff format --check` (0.17.4) e `mypy` | — | sem problemas |
| `manage.py check --fail-level WARNING` e `makemigrations --check --dry-run` | SQLite em memória | sem problemas, sem migrations novas |

Antes das correções, a mesma suíte teve 13 falhas: API interna de reautenticação removida no allauth 65.21 (`did_recently_authenticate`), `get_authentication_records` passando a exigir o usuário, o decorator `reauthentication_required` deixando de aceitar login recente, e faixas `django >=5.2,<5.3` no manifesto do Notes e no template de scaffold.

**Não verificado nesta rodada:** PostgreSQL com role runtime, os testes POSIX (pacotes, `test_core_distribution`, agente), `pip install --require-hashes` dos locks e build da imagem; ficam a cargo do CI do PR. A suíte emite `RemovedInDjango2028Warning` para as settings `EMAIL_*` (substituídas por `MAILERS` no Django 6.1); a migração fica para uma issue própria.

## Segredos de provedores de identidade cifrados (#19)

Rodada de 2026-11-09 para [#19](https://github.com/alaorneto/skeleton/issues/19): `skeleton/common/crypto.py` (keyrings Fernet por setting, `EncryptedTextField`, `SealedSecret`), MFA sobre o módulo comum, `IDENTITY_PROVIDER_ENCRYPTION_KEYS`, checks E026/E027 e `rotate_identity_provider_keys`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 869 passed, 111 skipped (marcados `posix` e os que exigem Docker) |
| Windows 11, PostgreSQL 16.2 local descartável (`pgserver`), assertions como `skeleton_runtime` | lista do job `postgres` do CI mais `tests/unit/test_crypto.py` e `tests/accounts/test_identity_provider_secrets.py` | 312 passed, 5 skipped: os 4 testes com tabela descartável de `test_identity_provider_secrets.py` (criada pelo owner do SQLite; os modelos de provedor chegam com #29) e o teste do serviço que vaza |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança válido; smoke do runtime aprovado |
| Windows | Ruff 0.17.4 `check .`, `python -m mypy`, `manage.py check --fail-level WARNING` | sem problemas |

Cobertura nova: `tests/unit/test_crypto.py` (keyring vazio ou inválido, cifra e decifra, `repr` sem segredo, rotação com `nova,antiga` e leitura só com `nova`, keyrings separados, E026) e `tests/accounts/test_identity_provider_secrets.py` (só texto cifrado no banco, `SealedSecret` na leitura, comando de rotação com saída só de contagens e IDs, rotação sem a chave antiga sem nenhuma escrita, E027). As regressões do MFA (`tests/accounts/test_identity.py`, `test_flows.py`, `test_providers.py`) continuam passando com os dados no formato anterior.

**Não verificado:** testes `posix`, Docker e SSO real; o PostgreSQL 16.15 do CI.

## API de serviços do núcleo e subsistemas (epic #113)

Rodada de 2026-11-09 para o epic [#113](https://github.com/alaorneto/skeleton/issues/113) (#102 a #110) e as dependências implementadas no mesmo branch: #17, #21, #18, #36, #37, #38, #39, #41, #42, #50, #51, #52, #58, #59 e #60. Cada commit de issue foi verificado em seguida num worktree separado; a tabela registra a rodada final no commit `27f9e8e`. Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite (venv com as versões do lock, sem `--require-hashes`) | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 820 passed, 111 skipped (marcados `posix` e os que exigem Docker) |
| Windows 11, PostgreSQL 16.2 local (binários portáteis do pacote `pgserver`, banco descartável `local_skeleton_test` provisionado por `deploy/postgres/bootstrap.sql`, migrations como `skeleton_owner`, assertions como `skeleton_runtime`) | a lista de testes do job `postgres` do CI (`tests/integration_postgres` e os módulos SQLite listados, inclusive os novos deste epic) | 307 passed, 1 skipped (o teste do serviço que vaza, pulado de propósito no PostgreSQL) |
| mesmo PostgreSQL | `tools/smoke_postgres.py` | inventário de segurança do schema válido; smoke de segurança do runtime aprovado |
| Windows | `manage.py check --fail-level WARNING`, `manage.py makemigrations --check --dry-run` | sem problemas; nenhuma migration pendente |
| Windows, settings de produção com valores descartáveis (como o job `deploy-check`) | `manage.py check --deploy --fail-level WARNING` | sem problemas (inclui o check E024 de armazenamento privado) |
| Windows | Ruff 0.17.4 `check .`/`format --check .` e `python -m mypy` (estrito em `skeleton/service_api` e `skeleton/testing`) | sem erros |

Cobertura nova, por área:

- **Capacidades e disponibilidade:** `test_capabilities.py` (matriz papel × recurso em HTML e API, escalada negada, reconciliação, MFA, menu) e `test_app_availability.py` (A/B, revogação, dependências, CAS, E017).
- **RLS e contrato operacional:** `test_self_discovery.py`, `tests/integration_postgres/test_self_discovery_policies.py` (política faltante, extra ou alargada reprova o check de schema) e `test_operational_records.py`.
- **Jobs e eventos:** `test_jobs_queue.py`, `test_jobs_worker.py` (dois workers PostgreSQL nunca executam o mesmo job), `test_jobs_periodic.py` (dois agendadores concorrentes), `test_domain_events.py` e `test_event_consumers.py`.
- **Notificações:** `test_notifications_notify.py`, `test_notifications_fanout.py` e `test_notifications_inbox.py` (contador com número constante de consultas; a caixa global revelou no PostgreSQL um JOIN bloqueado pela RLS, corrigido em `sk_notifications.0002`).
- **Arquivos:** `tests/unit/test_private_storage.py`, `test_attachments.py` e `test_attachment_downloads.py` (streaming com zero consultas, revogação, estados, X-Accel-Redirect).
- **Serviços do núcleo:** `test_core_identity_service.py`, `test_core_organization_service.py`, `test_core_access_service.py` (matriz papel × ação igual a `authorized_queryset`), `test_core_application_service.py`, `test_core_audit_service.py`, `test_core_notification_service.py`, `test_core_job_event_services.py`, `test_core_file_service.py` e `test_core_surfaces_on_services.py` (superfícies migradas sem caminhos internos e mapeamento único de erros).

**Não verificado nesta rodada:** os testes `posix` (pacotes, agente) e os que exigem Docker, pulados no Windows; SSO real; o PostgreSQL 16.15 do CI (a rodada local usou 16.2). Esses ficam a cargo do CI do PR.

## API de serviços entre apps — core/agente 0.4.0 (epic #112)

Rodada de 2026-11-09 para [#98](https://github.com/alaorneto/skeleton/issues/98), [#99](https://github.com/alaorneto/skeleton/issues/99), [#100](https://github.com/alaorneto/skeleton/issues/100) e [#101](https://github.com/alaorneto/skeleton/issues/101): `runtime.service_api`, checks E012/E018/W018/E019/W019, `package doctor`/`build`, kit `skeleton.testing.service_api`, `NoteService` e gerador. Rodada depois do merge da `main` com Django 6.1.3, django-allauth 65.21.1 e psycopg 3.3.8 (#122). Somente o que rodou:

| Ambiente | Comando | Resultado |
|---|---|---|
| Windows 11, Python 3.14.7, SQLite (venv com as versões do lock, sem `--require-hashes`: os hashes são de wheels Linux) | `SKELETON_ENABLE_EXAMPLE_APP=true TEST_DATABASE_URL=sqlite:///:memory: .venv/Scripts/python.exe -m pytest tests/unit tests/integration_sqlite tests/accounts -q` | 680 passed, 108 skipped (marcados `posix` e Docker) |
| Linux (WSL Debian), Python 3.14.7 standalone, venv com `requirements-dev.txt` por hashes, SQLite | mesmo comando com `.venv/bin/python` | 807 passed, 3 skipped (Compose exige o CLI do Docker): inclui os testes `posix` de artefatos assinados; `pip check` sem problemas |
| Linux (WSL), mesmo venv | `manage.py check --fail-level WARNING` e `manage.py makemigrations --check --dry-run` | sem problemas; nenhuma migration pendente |
| Windows | `python -m mypy` (inclui `skeleton/testing` e `skeleton/system/import_boundary.py` no modo estrito) e Ruff 0.17.4 `check`/`format --check` | sem erros |

Cobertura nova: `tests/unit/test_service_api_manifest.py` (validação do campo, piso 0.4.0, manifesto histórico, doctor/build e estrutura E018), `tests/unit/test_import_boundary.py` (imports absolutos, relativos, `from pacote import módulo`, superfícies do núcleo), `tests/unit/test_service_contract_kit.py` (kit contra serviços corretos e defeituosos), `tests/unit/test_package_doctor.py` (fronteira no doctor), `tests/integration_sqlite/test_service_api_checks.py` (checks com apps de fixture registradas), `tests/integration_sqlite/test_notes_service_api.py` (NoteService pelo kit: default deny, A/B, app desativada, ator divergente, CAS, conflito de código, rollback sem evento de auditoria, página e sumário via serviço, e um serviço que vaza B para A reprovado pelo kit) e o fluxo `skeleton_startapp` → `check --fail-level WARNING` → teste de contrato gerado em `tests/integration_sqlite/test_scaffold_end_to_end.py`.

**PostgreSQL com a role runtime não rodou localmente** (sem servidor PostgreSQL nesta máquina). `tests/integration_sqlite/test_notes_service_api.py` foi incluído no job `postgres` do CI; a evidência PostgreSQL é o resultado desse job no PR do epic, não esta rodada. O teste do serviço que vaza é pulado no PostgreSQL de propósito: lá a RLS da role runtime já impede o vazamento.

## Interface Tailwind/Flowbite e administração em `/platform/`

Rodada de 2026-11-08 em Windows 11 com Python 3.14.7 e Django 5.2.18 (venv local; lock Linux instalado sem `--require-hashes`), Node 25.9 para o build do CSS. Cobre a renovação visual, a navegação em três áreas e a retirada do Django admin.

| Conjunto executado | Backend | Resultado observado |
|---|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado | 452 passed, 107 skipped (marcados `posix` e os que exigem Docker) |
| `ruff check`, `ruff format --check` (0.17.4) e `mypy` | — | sem problemas |
| `manage.py check --fail-level WARNING` e `makemigrations --check --dry-run` | SQLite de desenvolvimento | sem problemas, sem migrations novas |
| `collectstatic` com `config.settings.build` (ManifestStaticFilesStorage) | — | `url()` das fontes e o JS do Flowbite resolvidos com hash |
| `npm ci --ignore-scripts` + `npm run check` em `frontend/` | — | CSS e `vendor/` atualizados |

No navegador (servidor de desenvolvimento, SQLite descartável, login real), foram conferidos em 1280px e 390px, claro e escuro: login (inclusive erro), Minha conta (perfil, senha, 2FA, sair), organizações, painel, catálogo, lista com paginação, detalhe, formulário com abas (teclado ArrowLeft/ArrowRight/Home/End), administração de acesso com abas, apps da organização, e toda a área `/platform/` (visão geral, usuários, organizações, catálogo, auditoria, apps, documentação e as cinco etapas de Versões e atualizações, com uma operação de exemplo). Nenhuma página tinha rolagem horizontal em 390px.

**Não verificado nesta rodada:** PostgreSQL/RLS (inclusive a listagem do seletor de organização dentro do contexto tenant), os testes POSIX (`test_core_distribution`, pacotes, agente), o fluxo real com o agente de deployment e o comportamento com JavaScript desligado (`@media (scripting: none)` não emulado).

## Integração do renderer CRUD — core/agente 0.3.0

Rodada de 2026-10-14 com Python 3.14.7, Django 5.2.18 e django-crispy-forms 2.7. Locks runtime/dev regenerados por `pip-compile --generate-hashes` e instalados com `--require-hashes`; `pip check` e `manage.py check` concluíram sem problemas.

| Conjunto executado | Backend | Resultado observado |
|---|---|---|
| Nove arquivos do conjunto específico abaixo | SQLite isolado, wheel instalado externamente | 280 passed |
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado | 499 passed |
| `tests/integration_postgres` e cinco arquivos HTTP/UI compartilhados abaixo | PostgreSQL 16.15; assertions com `skeleton_runtime` NOSUPERUSER/NOBYPASSRLS | 88 passed |

Os dois conjuntos SQLite conservaram um warning do ZIP malicioso com membro duplicado; a execução PostgreSQL não reportou warnings. O par privado de DSNs passou pelas barreiras de `disposable_database_pair()`: mesmo banco terminado `_skeleton_test`, diferente dos bancos normais, reset explicitamente autorizado, migrations com `skeleton_owner` e verificações com runtime. Não se usou owner para provar isolamento/UI.

```sh
DJANGO_SETTINGS_MODULE=config.settings.test TEST_DATABASE_URL=sqlite:///:memory: \
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/unit/test_presentation.py tests/unit/test_packages.py \
  tests/unit/test_presentation_release_changes.py tests/unit/test_core_distribution.py \
  tests/integration_sqlite/test_record_presentation.py \
  tests/integration_sqlite/test_reference_picker.py \
  tests/integration_sqlite/test_http_boundaries.py \
  tests/integration_sqlite/test_application_administration.py \
  tests/integration_sqlite/test_platform_boundary.py -q
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/unit tests/integration_sqlite tests/accounts -q
# Ambiente privado já provisionado; não imprimir DSNs:
set -a
. .private/runtime/services.env
set +a
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration_sqlite/test_record_presentation.py \
  tests/integration_sqlite/test_reference_picker.py \
  tests/integration_sqlite/test_http_boundaries.py \
  tests/integration_sqlite/test_application_administration.py \
  tests/integration_sqlite/test_platform_boundary.py -q
```

### Smoke do manifesto ao Chromium

Projeto/SQLite/venv Python 3.14 descartáveis **fora do checkout**: wheel `django_skeleton-0.3.0-py3-none-any.whl` construído com `pip wheel --no-deps --no-build-isolation`, dependências runtime instaladas por hashes e wheel instalado com `--no-deps`. Copiados somente overlays `config`/`manage.py`; nenhum diretório `skeleton` foi copiado ao projeto.

A app temporária registrou `Folder` e `Item` como `SecuredRecord`, specs explícitas e manifesto canônico v2. `Folder` não declarou `model_ui`; `Item` declarou code 4/title 8/target 12, body 12/rows 8 e UUID readonly na aba Conteúdo. Body era sensível e write-only desde o startup. Migrations reais instalaram infraestrutura congelada; `migrate`, `sync_table_catalog`, `check_skeleton_schema` e `bootstrap_skeleton` concluíram antes de iniciar runserver em loopback.

Observações com login allauth real, sessão/CSRF e Chromium:

- Catálogo fora do admin abriu ambos os modelos. Desktop 1280px: code/title na mesma linha, razão de larguras corrigida pelo gap ≈2, tanto no form como no detalhe. Mobile 390px: campos em uma coluna e `scrollWidth=390`. Textarea declarou rows 8; detalhe não tinha inputs editáveis.
- Criação com FK escolhida pelo popup e edição concordaram com API e consulta em contexto autorizado. Body não apareceu em GET edit/detail/API; edição sem opt-in preservou o segredo. Opt-in vazio limpou o valor, inclusive com JavaScript desabilitado; auditoria conservou `[REDACTED]`.
- POST de body com 10001 caracteres, opt-in marcado e aba inicialmente oculta respondeu 400; título retido, aba Conteúdo ativa, erro/ARIA associado e link do resumo focou `id_body`. ArrowRight/Home/End e seções sem JavaScript foram exercitados.
- FK de B e FK cuja leitura foi revogada entre GET/POST responderam 400, sem revisão/eventos de escrita residuais. Duas revisões iguais produziram um vencedor: formulário stale 409, API stale 412 e apenas um evento da edição vencedora.
- GET/Cancelar de delete conservou revisão/audit. Confirmação tinha apenas CSRF/revisão; soft delete/restore conservaram UUID e acrescentaram eventos próprios. Ator delete-sem-read não recebeu título/campos/segredo e seu Cancelar apontou à lista.
- Desativação bloqueou com 403 o POST de formulário já aberto. A configuração dedicada continuou disponível e reativou a app. Perfil, gestão de acesso e configuração salvaram pelo renderer comum; modelo sem UI usou spans 12 automáticos.
- `/admin/` mostrou somente configuração/parametrização, sem listas/forms de Item/Folder. CSS/JS do renderer vieram da própria origem, sem CDN. A regressão do wheel também renderizou form/detail nos cinco profiles, com valores/erros escapados e media uma vez.

Tabs, servidor, PostgreSQL iniciado para a fixture e arquivos temporários do smoke foram encerrados/removidos. Os arquivos privados PostgreSQL preexistentes foram preservados. Esta prova é de renderer/CRUD/autorização nos backends indicados; não homologa SSO externo, Compose ou backup/restore pelo agente.


## Verificação da revisão de código e documentação

Rodada de 2026-10-13, posterior ao checkpoint inicial `2207ba4`, com Python 3.14.7 e Django 5.2.18. Os resultados históricos das seções seguintes foram mantidos, não substituídos por contagens inferidas.

| Conjunto | Evidência observada |
|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | 306 passed, um warning do ZIP malicioso |
| Matriz PostgreSQL: segurança/infraestrutura, deployment, schema retido, gov.br e rollback da criação de organização | 104 casos concluídos com `skeleton_runtime`, em dois lotes |
| `tests/sso` | 25 passed, Chromium e Keycloak 26.8.0 reais, 94 warnings SAML preservados |

O primeiro comando PostgreSQL coletou 104 casos e atingiu seu limite operacional de 600 segundos depois de 86 casos concluídos. A retomada executou exatamente os 17 casos gov.br restantes e o caso de rollback da organização: **18 passed**. Não houve falha de assertion nessa matriz, mas o timeout não foi apresentado como uma execução única bem-sucedida. Separadamente, os quatro cenários de concorrência administrativa e o rollback da organização passaram juntos (**5 passed**); as dez regressões de journal passaram após remover uma assertion incidental sobre flags de `flock`.

### Reproduções e smokes desta revisão

- **Antes da correção:** criação com objeto de proprietário ainda ativo em memória, mas desativado no banco, deixou uma organização órfã. Depois, a validação continuou recusando o vínculo e a organização não permaneceu.
- **HTTP real + PostgreSQL runtime + Redis:** duas sessões com cookies/CSRF reais, POST e PATCH concorrentes do mesmo vínculo. Antes, `201/500` com deadlock; depois, `201/412`, vínculo desativado, revisão incrementada uma vez e sem sobrescrita silenciosa. Hooks apenas coordenaram o instante dos locks; não substituíram SQL, views ou respostas.
- **HTML no Chromium + SQLite isolado:** formulário aberto na revisão 1; mudança concorrente pela API confirmou revisão 2. O envio do formulário antigo retornou 409 e exibiu a orientação de atualizar a página. GET posterior conservou estado e ETag da revisão 2.
- **Agente real + socket Unix:** a resposta de `/submit` foi descartada depois da persistência do journal. O worker real encontrou drift de arquivo e falhou antes de manutenção. Reenvio com vínculo idêntico recuperou o mesmo resultado; outro request ID recebeu `idempotency_conflict`. Nenhum novo worker, gate ou cutover foi iniciado. Esse negativo prova transporte/idempotência, não atualização ou restauração completas.
- **Documentação e CLI:** 16 documentos Markdown verificados quanto a links relativos, anchors, fences e headings obrigatórios do exemplo. `manage.py package versions --manifest examples/notes/manifest.json` informou core `0.1.0` instalado e Notes `0.1.0` project-local. `check` passou, `makemigrations --check --dry-run` não encontrou mudanças e `pip check` não encontrou dependências quebradas.

Scripts e bancos desses smokes são descartáveis. Esta revisão não homologa gov.br, não ensaia Compose e não acrescenta prova de backup/restore PostgreSQL pelo agente. A evidência histórica diretório/SQLite abaixo mantém seu escopo original.


O smoke `tools/smoke_postgres.py` também passou nesta rodada: runtime sem ownership/superuser/BYPASSRLS, inventário exato de RLS/FORCE, ausência de contexto, isolamento A/B e entre conexões, FK tenant, imutabilidade, recusa de DML/DDL privilegiado, limpeza após COMMIT/ROLLBACK e rollback conjunto de negócio/auditoria quando INSERT do writer é revogado. O schema check runtime confirmou o inventário de segurança.

## Verificação da retomada: versões, pacotes e gov.br

Registro histórico da retomada, com Python 3.14.7, locks então usados e bancos isolados. Os números abaixo foram efetivamente observados naquela rodada; não são a contagem atual garantida nem resultado desta revisão documental:

| Suíte | Backend/protocolo exercitado | Resultado |
|---|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado, artefatos assinados e wheels reais | 279 passed |
| `tests/integration_postgres` + `test_deployment_records.py`, `test_retained_schema.py`, `test_govbr.py` | PostgreSQL 16.15, assertions com `skeleton_runtime` | 83 passed |
| `tests/sso` | Chromium e Keycloak 26.8.0 reais, OIDC/SAML | 25 passed |

Também executados: `manage.py check` sem problemas, `makemigrations --check --dry-run` sem alterações e `pip check` sem dependências quebradas. `tools/smoke_postgres.py` passou com runtime sem ownership/superuser/BYPASSRLS, incluindo isolamento A/B, guards/FKs, COMMIT/ROLLBACK e rollback de negócio/auditoria quando INSERT do writer é revogado.

### Smoke de instalação e atualização, não apenas testes

Deployment descartável em diretório com agente Unix-socket independente, virtualenv/wheelhouse offline bloqueado por hashes, Gunicorn real, banco SQLite externo aos releases e login/sessão/CSRF reais no Chromium:

- Publicados pacotes assinados do exemplo Notes e wheels core somente com `skeleton`. Exercitados os comandos `manage.py package init`, `versions`, `build` e `validate`, além das APIs de assinatura/publicação do core.
- Pelo admin: upload verificado → preview de arquivos/recursos/migrations → aprovação → manutenção/backup/migrator/checks runtime → reinício → confirmação. Apps instalaram em `applications/notes`, sem editar overlays do projeto.
- Notes `0.1.0` → `0.2.0`: dois registros em organizações distintas, incluindo tombstone, conservaram UUIDs, conteúdo e revisão; configurações e audit permaneceram. O arquivo antigo declarado deixou o release ativo.
- Desinstalação removeu código/rotas ativas, conservou schema, três eventos audit de negócio e migrations históricas reais; reinstalação conservou dados e deixou ambas organizações desabilitadas. Reativação explícita pela UI tenant restaurou leitura de seu registro/configuração, sem ativar a outra organização.
- Upgrades core de fixtures até `0.1.3` pela interface conservaram `config/settings/project.py`, rota customizada e dados empresariais. Esses números são versões dos artefatos descartáveis, não alteração da versão publicada deste checkout.
- Índice local assinado mostrou app atual e core disponível. POST sem CSRF retornou 403; usuário autenticado somente como admin de organização recebeu 403 no admin de releases. Reautenticação foi exigida ao expirar a janela.
- Ticket antecipado exibiu `awaiting_submission`, acompanhou a operação durante manutenção e não foi persistido no resultado. Histórico voltou após reinício. Painel e plano foram vistos em viewport de 390px, sem overflow global.
- Uma migration real deliberadamente falha conservou manutenção e dados. Com o daemon parado, `recover <id> restore` restaurou backup/release anterior, removeu o gate somente após checks/health e apareceu como `restored` na UI. A migration falha não ficou aplicada.
- Probe separado de startup aceitou manifesto local coerente e recusou versão local divergente do inventário. Os dez servidores LSP iniciaram; navegação Python resolveu Django na `.venv`, símbolos de documentos/estilos/configurações responderam e SQL foi analisado sem conexão ao banco.

As verificações encontraram e corrigiram falhas reais de integração: markers Python com duas partes tratados como SemVer, limite de um artefato aplicado ao wheelhouse completo, `config.py` do agente sombreando settings no probe, permissões congeladas copiadas ao staging, enum de lifecycle incompleto, fallback de template sobre chave inexistente, admissão gov.br sem email e initial de formulário de convite. O negativo de nonce assinado pelo IdP foi corrigido para interceptar o primeiro request do redirect, realmente alterar o nonce antes da assinatura e verificar recusa sem criar identidade.

### Limites desta verificação

O fluxo diretório/SQLite acima não prova execução de imagem/Compose nem backup/restore de PostgreSQL pelo agente. Esses caminhos têm implementação/runbook, mas continuam sem smoke completo nesta rodada; não foi concedido acesso adicional ao daemon Docker nem há runtime rootless alternativo instalado. Não extrapole as assertions PostgreSQL de segurança para cutover/restore de container.

Gov.br: os 36 casos de perfil no conjunto de contas usam JWTs RS256 locais e transporte HTTP controlado; Keycloak real prova os contratos corporativos OIDC/SAML, **não** homologação governamental. Credenciais/callback/logout autorizados de gov.br continuam ausentes. Login/logout governamentais reais e aprovação produtiva dependem do credenciamento descrito em [authentication-sso](authentication-sso.md#credenciamento-e-evidência-obrigatória). Nenhuma dessas aprovações foi simulada.

Um warning ZIP de membro duplicado pertence ao teste de pacote malicioso. Os 94 DeprecationWarnings de `python3-saml` continuam visíveis; não foram ocultados.

### Comandos das suítes desta retomada

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/unit tests/integration_sqlite tests/accounts
# Ambiente privado dos dois DSNs do mesmo DB descartável e marcador de reset:
set -a
. .private/runtime/services.env
set +a
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration_sqlite/test_deployment_records.py \
  tests/integration_sqlite/test_retained_schema.py tests/accounts/test_govbr.py
.venv/bin/python tools/smoke_postgres.py
# Keycloak real iniciado; exports preparados antes de carregar Django:
. .private/sso-fixtures/environment.sh
env -u TEST_DATABASE_URL -u TEST_MIGRATION_DATABASE_URL \
  SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests/sso
```

Serviços e scripts descartáveis do smoke são encerrados/removidos após a prova. Fixtures privadas preexistentes permanecem ignoradas; não são configuração produtiva. Para repetir o fluxo administrativo, siga o provisionamento de [deployment](deployment.md#agente-independente-de-deployment) com raiz/DB descartáveis e pacotes reais; não reaplique reset sobre dados de instalação.

## Verificação anterior: correções de segurança

Verificação final das correções executada em 2026-10-12, com Python 3.14.7 e os locks deste template:

| Suíte | Backend real | Resultado observado |
|---|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado | 137 passed |
| `tests/integration_postgres` + regressões compartilhadas de apps, acesso, audit e contas (comando abaixo) | PostgreSQL 16.15, `skeleton_runtime` sem BYPASSRLS | 78 passed |
| `tests/sso` | Chromium + Keycloak 26.8.0, OIDC e SAML em dois realms | 23 passed |

Nesta rodada também executados: setup em SQLite vazio (`migrate`, catálogo, schema check e bootstrap), `check` sem problemas, `makemigrations --check --dry-run` sem alterações e `pip check` sem dependências quebradas. Na integração inicial, anterior às correções, também foram executados bootstrap repetido, `collectstatic` (160 arquivos existentes e 421 pós-processados) e sincronizações idempotentes de catálogo (29 tabelas e 291 campos naquele schema).

Smoke desta rodada no Chromium com login real, cookies e CSRF: configuração da app alterou o título da página própria; desativação removeu o menu e bloqueou sua API com 403. PATCH JSON acima de 1048576 bytes respondeu 413 `request_too_large`, mantendo ETag e perfil inalterados. A superfície também retornou `X-Frame-Options: DENY`. Banco/servidor temporários foram descartados. O smoke PostgreSQL passou novamente com runtime restrito e schema check positivo; negativos de trigger, lifecycle e FK passaram na suíte.

Na integração inicial, Gunicorn WSGI foi iniciado com PostgreSQL/Redis reais: readiness respondeu HTTP 200/`status=ok` e WhiteNoise entregou `site.css`. O navegador exibiu conflito ao remover o último administrador, preservando a sessão. Esses smokes históricos não são build de imagem nem implantação HTTPS/SMTP produtiva.

Ao terminar a integração, todas as abas e os serviços descartáveis de aplicação, Gunicorn, PostgreSQL, Redis e Keycloak foram encerrados. Os arquivos privados ignorados permanecem apenas como fixtures locais; os comandos abaixo exigem reiniciar/provisionar os serviços antes de repetir.

`tools/smoke_postgres.py` passou usando conexões runtime: inventário exato de RLS/FORCE/policies, isolamento A/B, FK composta, imutabilidade, recusa de DML/DDL privilegiado, limpeza após COMMIT/ROLLBACK, conexões intercaladas e rollback conjunto de negócio/auditoria quando INSERT do writer foi revogado. O guard por owner foi exercitado separadamente, sem apresentá-lo como prova de RLS. A suíte PostgreSQL também demonstrou dois consumidores concorrentes de convite com exatamente um vencedor e lifecycle global sem perder o último administrador de outra organização.

Na integração inicial, o navegador real também exercitou login, perfil, emissão/aceitação de convite com email fixado, cadastro, reset por link de mailbox, grupos/vínculos, visibilidade, auditoria redigida, exclusão lógica/restauração com o mesmo UUID e layout de 390px sem overflow global. Cookie/CSRF reais deram PATCH 200, falta de CSRF 403 e revisão antiga 412; outro usuário recebeu lista privada vazia/detalhe 404 e, após compartilhamento, GET 200 mas PATCH 403. Login MFA correto/incorreto após rotação e recovery code de uso único foram exercitados via endpoints reais na suíte de contas.

Os negativos OIDC/SAML manipulam intercâmbios reais do IdP, sem substituir seus validadores. A rodada final passou com 94 DeprecationWarnings de `python3-saml` sobre `datetime.utcnow/utcfromtimestamp` no Python atual; avisos não foram ocultados.

Serviços usados: PostgreSQL em loopback:5433, Redis 8.8.3 em loopback:6380 na integração inicial e em socket temporário no probe de regressão, e Keycloak em loopback:8080, isolados e sem alterar configurações/grupos/socket do sistema. Arquivos privados de fixture em `.private/runtime/services.env` e `.private/sso-fixtures` são ignorados e não são configuração produtiva. O Docker default recusou o socket: build/execução da imagem e da stack Compose **não foram verificados**; renderização da configuração e execução isolada dos probes foram verificadas. GitHub OAuth real, SMTP externo, HTTPS/domínio produtivo e transporte de dados SQLite→PostgreSQL **não foram exercitados** por falta de credenciais/ambiente ou por serem procedimentos offline de outro projeto.

Para repetir em outra máquina, provisione os pré-requisitos abaixo; falta de serviço/secret é falha de pré-requisito, não aprovação por SQLite ou mocks. Não execute reset/DDL concorrente sobre o mesmo banco de fixture.

### Regressões dos achados recuperados

As correções cobrem EA01–EA09, SP01–SP05, RD01–RD10 e SBR01–SBR16. Os históricos da implementação registram falhas antes das alterações; as regressões permanentes abaixo exercitam os contratos corrigidos. Requisitos de providers ainda não habilitados e hipóteses sem reprodução não foram convertidos em bugs confirmados.

| Achados | Regressões permanentes |
|---|---|
| EA01, EA03–EA09 | `tests/integration_sqlite/test_app_extensions.py` |
| EA02, SBR08, SBR14 | `tests/integration_sqlite/test_recovered_surfaces.py` |
| SP01–SP05, SBR03, SBR10, SBR13 | `tests/accounts/test_social_hardening.py` |
| RD01, RD06, SBR02, SBR11, SBR12 | `tests/accounts/test_lifecycle_findings.py` |
| RD03, RD04 | `tests/integration_postgres/test_schema_assurance.py` |
| RD05, SBR01, SBR04 | `tests/integration_sqlite/test_access_lifecycle_regressions.py` |
| SBR05, SBR06 | `tests/integration_sqlite/test_audit_findings.py` |
| RD02, RD10, SBR07, SBR15, SBR16 | `tests/integration_sqlite/test_http_findings.py` |
| RD07–RD09, SBR09 | `tests/unit/test_deployment_findings.py` |

RD04 inclui substituir a função de lifecycle por uma procedure de mesmo nome, assinatura e corpo: antes da correção, o check retornou `[]` e a regressão falhou. SBR03 inclui novo callback antes da confirmação local, preservando a admissão inicial sem permitir readmissão de vínculo suspenso; o fluxo SAML real também executa essa repetição.

As regressões de deployment usam `docker compose config --format json`, executam o probe renderizado contra Redis real e o probe web contra servidor WSGI real com Host produtivo permitido. Isso comprova esses comandos/configurações, não build de imagem nem execução da stack pelo daemon Docker.


## Isolamento da suíte

O isolamento evita que um teste apague dados de outro teste ou de uma instalação real. **Runtime** é a role limitada usada nas verificações de aplicação; **owner** é a role privilegiada usada somente para reconstruir o schema da fixture. Um banco “de teste” não se torna seguro só pelo nome: as URLs, o sufixo e o marcador de reset precisam satisfazer todas as barreiras abaixo.

`tests/conftest.py` oferece fixture `skeleton_db`. pytest-django fornece settings e desbloqueio explícito do ORM; não use criação/flush automático, `django_db`, TransactionTestCase de flush ou `--reuse-db` como atalho em schema protegido. O `pytest-xdist` é usado, mas só com um banco provisionado por worker (abaixo): dois processos nunca compartilham um schema.

No SQLite, a sessão prepara um banco-modelo vazio e migrado. Cada teste recebe uma cópia própria via API de backup SQLite em `tmp_path`; a conexão é reaberta para registrar as funções SQL (UDFs) necessárias aos triggers. Ao terminar, descarta-se somente o arquivo temporário. Testes unitários sem banco não ativam essa fixture.

No PostgreSQL, `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) devem apontar ao **mesmo banco descartável**, com nome terminado `_skeleton_test`, diferente de qualquer `DATABASE_URL`/`MIGRATION_DATABASE_URL` normal. Também é obrigatório `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`.

Com `pytest -n`, cada worker usa o seu banco, `<prefixo>_gwN_skeleton_test`, derivado dessas duas URLs por `tools/integration_database.py`. O nome mantém o sufixo, então todas as barreiras valem para ele. Um worker que não trocou de banco é recusado por `disposable_database_pair()`, e um banco de worker que não existe para a suíte antes de qualquer teste (ver [Rodar rápido e rodar completo](#rodar-rápido-e-rodar-completo)).

A fixture fecha conexões e recria **somente o schema `skeleton`** com owner a cada teste. Depois, todas as verificações de aplicação/RLS usam runtime. Ela não cria banco/roles nem reseta dados externos; DDL/reset são serializados. Se DSN, sufixo ou marcador faltar, pare e corrija o ambiente: a ausência não autoriza reset automático nem desabilitar a barreira.

O schema de cada teste vem de um **template da sessão**, como no SQLite. O fixture `postgres_template` aplica as migrations uma única vez e captura estrutura e dados do schema com `pg_dump --schema=skeleton`, rodando como `skeleton_owner`. Em cada teste, `restore_schema()` apaga o schema, recria-o e executa esse SQL numa só transação, também como owner. O `GRANT CREATE` ao `skeleton_audit_writer` vale só durante a restauração, para transferir a ownership das funções de auditoria, como faz `InstallRecordInfrastructure`. Antes, cada teste rodava o `migrate` completo, cerca de 4 s por teste. A restauração roda com `synchronous_commit=off` na transação do owner: um schema de fixture perdido numa queda é recriado pelo teste seguinte. O template não é `--reuse-db`: nenhum teste herda dados de outro, e nada é truncado ou apagado linha a linha. `tests/integration_postgres/test_schema_template.py` compara o schema restaurado com o migrado (tabelas, colunas, constraints, índices, triggers, funções, policies, flags de RLS, owners, ACLs, sequências e dados) e mostra que a comparação falha quando falta uma policy, trigger, grant, FORCE RLS ou linha. A comparação de ACLs usa os privilégios efetivos: uma ACL `NULL` conta como `acldefault()` do owner. Isso é necessário porque um `REVOKE` pode deixar gravada uma ACL explícita igual ao padrão, que o `pg_dump` omite e o restore recria como `NULL`. O texto dos CHECK e dos índices com predicado ou expressão também passa por um ida-e-volta: o restore pode mover casts sem mudar a expressão (por exemplo, um `IN` sobre varchar). Por isso, o fingerprint recria cada CHECK (como `NOT VALID`) e cada um desses índices numa transação revertida e compara o texto que o PostgreSQL devolve.

O template exige `pg_dump` com versão igual ou maior que a do servidor (no CI vem de `postgresql-client`). Para usar outro binário, aponte `SKELETON_TEST_PG_DUMP` para ele. Com `SKELETON_TEST_POSTGRES_TEMPLATE=0`, a fixture volta a migrar a cada teste, o que serve para depurar migrations. Sem `pg_dump` e sem essa variável, a suíte falha com erro de uso, em vez de ficar lenta em silêncio.

O perfil de teste (`skeleton.conf.profiles.test_settings`) usa `skeleton.testing.hashers.FastPBKDF2PasswordHasher`: PBKDF2-SHA256 com 1.000 iterações, com o mesmo algoritmo e o mesmo formato de hash da produção. Com as iterações de produção, cada senha de fixture custava cerca de 0,3 s, e quase todo teste cria várias identidades. Esse hasher nunca entra nos settings de uma instalação.

Browser SSO tem fixture de sessão com DB isolado e servidor real, preservando commits até fim do cenário. Não use TestCase com atomic externa como prova de COMMIT/SET LOCAL; os testes transacionais precisam de commits verdadeiros.

## SQLite

**Pré-requisitos:** virtualenv e locks instalados, exemplo habilitado e ambiente local sem URL de produção. O primeiro comando usa bancos isolados da suíte. Os comandos `manage.py` seguintes, ao contrário, operam no banco escolhido pelos settings/ambiente; só os execute num banco local descartável.

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests/unit tests/integration_sqlite tests/accounts
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
```

**Resultado esperado:** suíte concluída e checks sem problemas no banco local. **Limite:** sucesso em SQLite demonstra seus contratos, mas não privilégios PostgreSQL ou funcionamento de infraestrutura externa. Uma falha de setup deve ser corrigida, não escondida por fixtures que pulam a proteção.

Bootstrap usa getpass ou secret descartável forte; repita bootstrap/sync e compare UUIDs/ausência de reset de senha. Inicie runserver e faça login real, perfil, dashboard, convite, roles/grupos, Note opt-in e audit. API smoke usa cookie/CSRF dessa sessão, nunca force_login como prova de autenticação. Navegador deve mostrar estado final e fechar tabs.

Testes permanentes: `tests/unit/test_http_revision.py`, `test_security_logging.py`; `tests/integration_sqlite/test_record_contract.py`, `test_security_invariants.py`, `test_http_boundaries.py`. Cobrem comportamentos públicos, não source-string assertions como substituição de prova.

## PostgreSQL runtime real

**Pré-requisitos:** PostgreSQL iniciado, banco descartável criado e provisionado pelo operador, duas roles/URLs do mesmo banco e marcador de reset. Os comandos seguintes usam arquivos privados da instalação de integração; em outra máquina, provisione equivalentes antes de carregá-los. Não há secrets de produção nesses exemplos.

```sh
set -a
. .private/runtime/services.env
set +a
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration_sqlite/test_app_extensions.py \
  tests/integration_sqlite/test_access_lifecycle_regressions.py \
  tests/integration_sqlite/test_audit_findings.py \
  tests/accounts/test_lifecycle_findings.py \
  tests/accounts/test_social_hardening.py
.venv/bin/python tools/smoke_postgres.py
```

O smoke faz reset **somente** do schema skeleton do DB de fixture validado; é ferramenta permanente de segurança, não healthcheck produtivo. Não copie secrets para stdout/argumentos. Se DATABASE_URL/MIGRATION_DATABASE_URL forem iguais às URLs de teste, a barreira recusa: separe ambiente normal e teste, não desative a barreira.

Alternativa Compose, com secrets privados completos e DB descartável previamente criado/provisionado:

```sh
docker compose --profile integrations up -d postgres redis keycloak
# Operador aplica bootstrap.sql ao DB fixture novo; senhas externas ao SQL.
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py migrate
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py sync_table_catalog
DJANGO_SETTINGS_MODULE=config.settings.production .venv/bin/python manage.py check_skeleton_schema
```

Os dois primeiros usam MIGRATION_DATABASE_URL owner; o terceiro DATABASE_URL runtime e requisitos production. Imagens fixas não são prova de que serviços iniciaram.

Assertions esperadas: runtime sem super/BYPASS/ownership/SET ROLE privilegiado; policies/RLS/FORCE/grants exatos; sem contexto SELECT=0/INSERT42501; A lê A, UPDATE de B (incluindo soft delete) afeta zero, INSERT B42501; DELETE físico é recusado com 42501 por ausência de grant, proteção mais restrita que uma operação filtrada por RLS. FK A→B23503; tenant reassignment guard; UUID GUC malformed22P02; COMMIT/ROLLBACK restaura contexto vazio; duas conexões intercaladas não cruzam tenant; discovery Membership própria só SELECT; runtime não TRUNCATE/ALTER/INSERT audit/UPDATE audit/DELETE audit (42501). Guard de identidade é exercitado separadamente por owner, não usado como prova de RLS. Negativos de deploy removem trigger/acrescentam policy permissiva e exigem falha específica.

## OIDC e SAML reais

**OIDC** e **SAML** são protocolos de login entre a aplicação e um provedor de identidade (IdP). O navegador precisa percorrer os redirecionamentos reais e terminar com sessão Django; receber claims isoladas não prova esse percurso. Consulte [login e protocolos](concepts.md#login-e-protocolos).

**Pré-requisitos:** Keycloak iniciado com ambos os realms importados, endpoints exatos acessíveis, dependências SAML/XML instaladas e banco de fixture isolado. Prepare arquivos privados antes de iniciar Django; só então execute os testes.

Realms `tests/sso/realm-skeleton-a.json` e `realm-skeleton-b.json`: usuários fixture, confidential OIDC PKCE S256 e SAML com response/assertion assinadas. Django no host `http://127.0.0.1:8000`; IdP `http://127.0.0.1:8080/realms/skeleton-a`/b. HTTP só DEBUG local. Não troque por aliases que mudem issuer/callback no browser.

```sh
.venv/bin/python tools/prepare_sso_fixtures.py --output .private/sso-fixtures
. .private/sso-fixtures/environment.sh
.venv/bin/python -m playwright install chromium
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests/sso
```

Preparador busca discovery/metadata/cert **públicos** nos realms iniciados; gera SP keys/secrets em diretório privado, providers.json/environment.json/environment.sh (0600), imprime só paths. Source do environment.sh deve ocorrer **antes de inicializar Django**, porque parser resolve env secrets. `SSO_PROVIDER_CONFIG_PATH` e `SSO_FIXTURE_ENV_PATH` identificam arquivos. Não copie private keys IdP nem altere trust store. Roles/vínculos/convites são criados por serviços em fixture; não use claims arbitrárias para promovê-los.

Browser completa redirect/login IdP/callback real e consulta API pela sessão final: org correta, ausência staff/superuser, rerun sem duplicar User/Profile/Membership. Testes negativos manipulam intercâmbios reais da fixture, não substituem o validador: state/replay/issuer/audience/signature/expiry/PKCE OIDC; assinatura/destination/audience/expiry/InResponseTo/unsolicited/replay SAML. Coincidência email não conecta; sem binding/JIT ou email não confiável não concede afiliação. GitHub exige app OAuth/credenciais reais/callback documentado, senão registre não exercitado.

**Resultado esperado:** login retorna à aplicação, cria/reusa a identidade correta e os casos adversos são recusados sem conceder vínculo indevido. **Falha/limite:** serviço ou secret ausente é pré-requisito pendente, não teste aprovado; esta fixture corporativa não habilita GitHub real nem homologação gov.br.

## Matrix mínima para nova tabela

Esta **matriz** cruza atores, organizações e operações para encontrar vazamentos que um teste de criação feliz não detecta. A/B são duas organizações; Alice/Bob são usuários com acessos distintos; o administrador e o usuário sem `Membership` completam os casos. CAS compara a revisão lida antes de gravar: duas gravações com a mesma revisão devem ter só um vencedor. [Auditoria e concorrência](concepts.md#auditoria-e-concorrência) explica esse contrato.

- Todos próprios UUID/base/spec e físicos catalogados, inclusive technical manifest nominal.
- A/B/Alice/Bob/admin/sem Membership: list/detail/count/lookup/FK picker/audit/restore não vazam tenant ou privado.
- Tenant ausente/nested tenant diferente/streaming/async proibido falham fechado; renderização dentro do contexto.
- TablePermission obrigatória, default deny; grant não pula tabela/tenant; visibility org/grupo dá leitura, não escrita.
- Revogar papel/grupo/grant perde efeito no próximo request; org/is_staff/owner escondidos não aceitos em mass assignment.
- Último administrador protegido, inclusive GroupRole e lifecycle global de User.
- Tombstones invisíveis; restore conflito preserva linhas/sysids; referências cross-tenant falham INSERT/UPDATE.
- Duas revisões iguais: um vencedor, outro stale; `me` compara ambas revisões; unknown fields/CSRF negados.
- Audit create/save(update_fields)/queryset.update/bulk_create/links/SQL/softdelete/restore: um evento correto por mudança, ator/revision/tenant/source corretos e secret/body nunca no payload.
- update_fields vazio sem escrita; bulk_update/conflict upsert rejeitados; SQL com revision correto auditado.
- Rollback/savepoint e handler write-then403/500 sem linha/evento; falha writer INSERT aborta negócio; Audit UPDATE/DELETE negados.
- SQLite externo sem UDF não grava silenciosamente. Catálogo sync idempotente preserva sysid/textos e não faz DDL.
- Conta: convite expired/replay/wrong email/corrida, público sem Membership, reset normal e tokens inválidos/SSO-only/inativo/deletado, auth_version, MFA/recovery single-use/cipher/rotação preservando máscara, admin sem bypass/enrollment sem deadlock.

A aprovação exige evidência dos backends/protocolos relevantes; uma suíte limitada não autoriza alegar matrix completa.

## Administração/configuração e viewer — core 0.2.0

Rodada desta implementação, com Notes opt-in, Python 3.14 e Django 5.2.18:

| Execução observada | Resultado |
|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | 402 passed; 1 warning esperado de ZIP com membro duplicado no negativo de arquivo malicioso |
| PostgreSQL 16.15, role runtime restrita: `tests/integration_postgres` + administração, apresentação, picker, fronteira de admin e extensões | 76 passed |
| Login real e HTTP do projeto gerado com dois modelos expostos e um não exposto | CAS 200/412/409; FK revogada recusada; A/B, mass assignment e filtro desconhecido negados; boolean null; delete/restore com mesmo UUID; write-only preservado/limpo |
| Chromium real, desktop/390px/sem JavaScript | Admin só parâmetros; viewer e registros globais externos; abas por teclado; erro focável na aba correta; sem disclosure write-only; popup seleciona UUID autorizado; largura do documento 390px |
| Agente independente, backend directory, SQLite e Gunicorn com dois workers | Core update e install → update → uninstall → reinstall confirmados; negativo de staging e restore de migration falhada confirmados |

Comandos das duas matrizes:

```sh
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/unit tests/integration_sqlite tests/accounts -q

# Ambiente privado de DB descartável, runtime/owner separados como descrito acima:
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration_sqlite/test_application_administration.py \
  tests/integration_sqlite/test_record_presentation.py \
  tests/integration_sqlite/test_reference_picker.py \
  tests/integration_sqlite/test_platform_boundary.py \
  tests/integration_sqlite/test_app_extensions.py -q
```

Os testes de fronteira percorrem login allauth/CSRF, flags/MFA, namespace único, rotas antigas 404, shell/media e CAS dos registros globais. Os testes de apresentação/picker cobrem projeção, orçamento de queries independente de linhas, relações negadas, cobertura/widget/allowlists e serviços próprios comuns a HTML/API. V1 assinado, rejeições do schema v2 e diffs/compatibilidade de configuração fazem parte dos unitários. A rodada completa antecedeu apenas a adição do clearfix no shell; essa correção foi confirmada no navegador com rodapé abaixo do formulário e mesmas mídias.

### Smoke de lifecycle observado

Fixture de publicador/app própria assinada, trust store privado, ambientes offline por hashes, DB externo ao release, supervisor real e canal independente:

- Core 0.2.0 novo ativado na geração 1; instalação do app 0.1.0 na geração 2 mostrou exatamente um grupo, sem links/modelos de negócio no admin. Configuração tenant começou na revisão virtual 0 e foi persistida pela tela real.
- Update 0.2.0 na geração 3 exibiu `presentation_changes` para campo/label, tela removida/adicionada e colunas. Grupo não duplicou; tela velha saiu, tela nova apareceu; UUIDs de configuração e registro, e valor configurado, permaneceram.
- Coluna write-only numa lista de um pacote assinado foi recusada no staging com `static_checks_failed`, sem formulário de aprovação e sem cutover.
- Uninstall na geração 4 mostrou retenção de dois registros em uma organização e removeu o grupo. Reinstall na geração 5 conservou registro/configuração/valor e voltou desativado; negócio retornou 403 até reativação explícita. Formulário aberto antes de desativar também recebeu 403 no POST, enquanto parâmetros permitiram reativar.
- Update com `RunPython` histórica deliberadamente falhada terminou `failed/migration_failed`, geração 5, backup registrado e sem workers servindo o destino. Com daemon parado, `recover <operation> restore` confirmou `restored`. Depois de checks/saúde e reinício, browser/API observaram app 0.2.0, tela anterior válida, mesmos UUIDs e configuração anterior; nenhuma publicação parcial.

Os scripts/chaves/DBs eram descartáveis e não são fixtures distribuídas. Essa rodada **não ensaiou lifecycle/backup/restore PostgreSQL ou backend Compose**, nem repetiu SSO/Keycloak/gov.br. Os 76 testes PostgreSQL demonstram contratos de runtime/RLS e das superfícies, não essas operações de deployment. Resultado SQLite não substitui esses ensaios produtivos.

Fechamento: o negativo/positivo de fronteira administrativa passou novamente (**5 passed**) após o clearfix; `manage.py check` não encontrou problemas e `makemigrations --check --dry-run` não detectou mudanças. Verificador descartável confirmou links locais de 16 documentos e validou o exemplo JSON do contrato pelo schema fechado de apresentação.
