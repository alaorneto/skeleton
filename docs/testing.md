# Testes, fixtures e evidência

Este documento tem duas funções: registrar **evidências históricas** e explicar como repetir verificações em ambiente descartável. Um resultado antigo não certifica alterações posteriores nem uma instalação de outro projeto. Os comandos abaixo são procedimentos a executar, não resultados novos.

Uma **fixture** é o ambiente/dado controlado usado pelo teste; um **smoke** é um ensaio curto do fluxo real; uma **assertion** é a condição que o teste exige. Mocks e transportes controlados ajudam a testar respostas adversas, mas não provam que um serviço externo autorizou ou executou o fluxo. Veja [autorização e contexto](concepts.md#autorização-e-contexto) e [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) antes de interpretar os resultados.

## Como escolher e interpretar a prova

| Pergunta | Prova necessária | O que não substitui essa prova |
|---|---|---|
| Contratos de registros/serviços funcionam localmente? | Suíte SQLite isolada e seus negativos. | Isso não demonstra RLS PostgreSQL. |
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
| `sqlite` | Lock com hashes e `pip check`, `manage.py check`, `makemigrations --check` e `tests/unit tests/integration_sqlite tests/accounts` com cobertura (`coverage.xml` publicado como artefato), em Python 3.12 e 3.14. | RLS PostgreSQL. Cobertura mede linhas executadas, não a qualidade das assertions. |
| `deploy-check` | `check --deploy` com os settings de produção e valores descartáveis. | Conectividade com banco, Redis ou SMTP. |
| `postgres` | Banco `ci_skeleton_test` provisionado por `deploy/postgres/bootstrap.sql`; migrations como `skeleton_owner`, assertions como `skeleton_runtime` e `tools/smoke_postgres.py`. | Um PostgreSQL de produção ou um backup/restore. |
| `build` | Wheel do núcleo, `pip-audit` dos dois locks e `docker build`. | Que a imagem sobe em produção. |

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

O job PostgreSQL concede `CREATE ON DATABASE` ao owner da fixture para que `reset_schema()` e `restore_schema()` recriem o schema `skeleton`. As migrations rodam uma vez por sessão e cada teste restaura o template (ver [Isolamento da suíte](#isolamento-da-suíte)); `tools/smoke_postgres.py` continua aplicando o `migrate` real. As senhas são exclusivas do CI e valem só para o container de serviço. SSO real, gov.br e o agente de deployment em host não fazem parte do CI.

Fora de Linux/macOS, os testes marcados `posix` (artefatos assinados, que dependem de `os.O_NOFOLLOW`) e os módulos do agente (`fcntl`) são pulados com motivo explícito em vez de falharem na coleta. A fixture de renderização do Compose é pulada quando o CLI `docker` não existe.

## Django 6.1.3, django-allauth 65.21.1 e psycopg 3.3.8 (#122)

Rodada de 2026-11-09 em Windows 11 com Python 3.14 e os locks regenerados por `uv pip compile`. O venv local foi instalado com as versões do novo `requirements-dev.txt` usadas como constraints (sem `--require-hashes`, porque os hashes são de wheels Linux). Para comparação, os testes que falharam também rodaram num venv com os locks anteriores (Django 5.2.18, allauth 65.19.7): passaram nos dois casos, o que confirma que as falhas eram regressões da atualização e não do ambiente.

| Conjunto executado | Backend | Resultado observado |
|---|---|---|
| `tests/unit tests/integration_sqlite tests/accounts` | SQLite isolado | 531 passed, 107 skipped (marcados `posix`, agente e os que exigem Docker) |
| `ruff check`, `ruff format --check` (0.17.4) e `mypy` | — | sem problemas |
| `manage.py check --fail-level WARNING` e `makemigrations --check --dry-run` | SQLite em memória | sem problemas, sem migrations novas |

Antes das correções, a mesma suíte teve 13 falhas: API interna de reautenticação removida no allauth 65.21 (`did_recently_authenticate`), `get_authentication_records` passando a exigir o usuário, o decorator `reauthentication_required` deixando de aceitar login recente, e faixas `django >=5.2,<5.3` no manifesto do Notes e no template de scaffold.

**Não verificado nesta rodada:** PostgreSQL com role runtime, os testes POSIX (pacotes, `test_core_distribution`, agente), `pip install --require-hashes` dos locks e build da imagem; ficam a cargo do CI do PR. A suíte emite `RemovedInDjango2028Warning` para as settings `EMAIL_*` (substituídas por `MAILERS` no Django 6.1); a migração fica para uma issue própria.

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

`tests/conftest.py` oferece fixture `skeleton_db`. pytest-django fornece settings e desbloqueio explícito do ORM; não use criação/flush automático, `django_db`, TransactionTestCase de flush, `--reuse-db` ou pytest-xdist como atalho em schema protegido.

No SQLite, a sessão prepara um banco-modelo vazio e migrado. Cada teste recebe uma cópia própria via API de backup SQLite em `tmp_path`; a conexão é reaberta para registrar as funções SQL (UDFs) necessárias aos triggers. Ao terminar, descarta-se somente o arquivo temporário. Testes unitários sem banco não ativam essa fixture.

No PostgreSQL, `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) devem apontar ao **mesmo banco descartável**, com nome terminado `_skeleton_test`, diferente de qualquer `DATABASE_URL`/`MIGRATION_DATABASE_URL` normal. Também é obrigatório `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`.

A fixture fecha conexões e recria **somente o schema `skeleton`** com owner a cada teste. Depois, todas as verificações de aplicação/RLS usam runtime. Ela não cria banco/roles nem reseta dados externos; DDL/reset são serializados. Se DSN, sufixo ou marcador faltar, pare e corrija o ambiente: a ausência não autoriza reset automático nem desabilitar a barreira.

O schema de cada teste vem de um **template da sessão**, como no SQLite. O fixture `postgres_template` aplica as migrations uma única vez e captura estrutura e dados do schema com `pg_dump --schema=skeleton`, rodando como `skeleton_owner`. Em cada teste, `restore_schema()` apaga o schema, recria-o e executa esse SQL numa só transação, também como owner. O `GRANT CREATE` ao `skeleton_audit_writer` vale só durante a restauração, para transferir a ownership das funções de auditoria, como faz `InstallRecordInfrastructure`. Antes, cada teste rodava o `migrate` completo, cerca de 4 s por teste. O template não é `--reuse-db`: nenhum teste herda dados de outro, e nada é truncado ou apagado linha a linha. `tests/integration_postgres/test_schema_template.py` compara o schema restaurado com o migrado (tabelas, colunas, constraints, índices, triggers, funções, policies, flags de RLS, owners, ACLs, sequências e dados) e mostra que a comparação falha quando falta uma policy, trigger, grant, FORCE RLS ou linha. A comparação de ACLs usa os privilégios efetivos: uma ACL `NULL` conta como `acldefault()` do owner. Isso é necessário porque um `REVOKE` pode deixar gravada uma ACL explícita igual ao padrão, que o `pg_dump` omite e o restore recria como `NULL`. O texto dos CHECK e dos índices com predicado ou expressão também passa por um ida-e-volta: o restore pode mover casts sem mudar a expressão (por exemplo, um `IN` sobre varchar). Por isso, o fingerprint recria cada CHECK (como `NOT VALID`) e cada um desses índices numa transação revertida e compara o texto que o PostgreSQL devolve.

O template exige `pg_dump` com versão igual ou maior que a do servidor (no CI vem de `postgresql-client`). Para usar outro binário, aponte `SKELETON_TEST_PG_DUMP` para ele. Com `SKELETON_TEST_POSTGRES_TEMPLATE=0`, a fixture volta a migrar a cada teste, o que serve para depurar migrations. Sem `pg_dump` e sem essa variável, a suíte falha com erro de uso, em vez de ficar lenta em silêncio.

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
