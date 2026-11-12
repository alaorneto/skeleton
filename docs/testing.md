# Testes, fixtures e evidência

Este documento explica como verificar o projeto hoje, em ambiente descartável: todo comando e todo caminho citado existe no repositório atual. Os resultados de cada rodada, com os comandos da época, estão na [evidência histórica](testing-evidence.md). Um resultado antigo não certifica alterações posteriores nem uma instalação de outro projeto.

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

A suíte unitária também protege a documentação: `tests/unit/test_documentation_references.py` confere links, âncoras, caminhos citados, comandos `manage.py`, o catálogo de serviços do núcleo e as fronteiras de código; `test_docs_app_snippets.py` confere os exemplos de código de app; `test_check_reference.py`, a referência dos checks. Ao criar pacote, serviço, comando, check ou documento, atualize o catálogo correspondente no mesmo PR.

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

O job `tests` concede `CREATE ON DATABASE` ao owner da fixture, em cada banco, para que `reset_schema()` e `restore_schema()` recriem o schema `skeleton`. As migrations rodam uma vez por worker, sempre (`SKELETON_TEST_TEMPLATE_CACHE=0`, sem o cache local do template), e cada teste restaura o template (ver [Isolamento da suíte](#isolamento-da-suíte)); `tools/smoke_postgres.py` continua aplicando o `migrate` real. O container do PostgreSQL recebe `fsync=off`, `synchronous_commit=off`, `full_page_writes=off` e `max_locks_per_transaction=2048` por `POSTGRES_INITDB_ARGS`: ele é descartável, e a tabela de locks precisa comportar quatro workers recriando o schema ao mesmo tempo. As senhas são exclusivas do CI e valem só para o container de serviço. SSO real, gov.br e o agente de deployment em host não fazem parte do CI. Para mudar o número de fatias, troque o `N` de `--shard I/N` na matriz; nenhuma lista de arquivos é mantida à mão, então um pacote novo com testes entra numa fatia sozinho.

Fora de Linux/macOS, os testes marcados `posix` (artefatos assinados, que dependem de `os.O_NOFOLLOW`) e os módulos do agente (`fcntl`) são pulados com motivo explícito em vez de falharem na coleta. A fixture de renderização do Compose é pulada quando o CLI `docker` não existe.

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

Os testes marcados `needs_example_app` são pulados sem `SKELETON_ENABLE_EXAMPLE_APP=true`; com `SKELETON_TEST_REQUIRE_EXAMPLE_APP=1` (ligado no CI) a ausência da app é erro de uso, e a suíte para antes de rodar.

### Rodar rápido e rodar completo

| Para | Comando | Tempo |
|---|---|---|
| O arquivo ou teste em que você está trabalhando | `python -m pytest tests/integration/test_x.py -q -x` (`-k nome` escolhe um teste; `-x` para na primeira falha) | um teste com banco: 1,6 s com o template em cache, 6,2 s quando o código mudou e a suíte migra de novo |
| Só o que falhou na última vez | `python -m pytest tests -q -n auto --lf` | proporcional ao que falhou |
| Testes sem banco | `python -m pytest tests/unit -q -n auto` | menos de 1 minuto |
| Suíte inteira, obrigatória antes do PR | `SKELETON_ENABLE_EXAMPLE_APP=true python -m pytest tests -q -n auto` | cerca de 4 minutos |
| Suíte inteira com cobertura, como no CI | o mesmo, com `--cov` | cerca de 4 minutos |
| Agenda | `SKELETON_BUNDLED_APPS=calendar python -m pytest tests/contrib/calendar -q -n auto` | menos de 1 minuto |

Sem `-n`, a suíte roda num processo e num banco, como antes: é o modo para depurar com `--pdb` ou ler a saída de um teste. O comando rápido não substitui a suíte inteira, porque não prova que a mudança deixou os outros módulos intactos.

**Como a paralelização funciona.** `-n` é do `pytest-xdist` (lock de desenvolvimento): N processos, cada um com uma parte dos testes. Cada processo usa um banco só seu, com nome derivado das duas URLs de teste (`local_skeleton_test` vira `local_gw3_skeleton_test` no worker `gw3`), migra uma vez e restaura o template antes de cada teste, exatamente como no modo serial. O `tests/conftest.py` troca as duas variáveis de ambiente no próprio worker; assim os settings, `disposable_database_pair()`, os testes que leem as variáveis e os subprocessos que eles iniciam enxergam o mesmo banco. Os fixtures continuam sem criar banco nem role.

Antes de iniciar os workers, a suíte confere duas coisas: se o owner abre o banco de cada worker e se a tabela de locks do servidor comporta as restaurações simultâneas. Cada restauração segura cerca de 2.100 locks até o commit, e o padrão do PostgreSQL (6.400 no total) só aguenta dois workers. Se faltar algo, a suíte para com um erro de uso que diz o que provisionar. Num PostgreSQL que não seja o de `tools/dev_postgres.py`, crie os bancos `<prefixo>_gwN_skeleton_test` como o banco base (bootstrap, senhas, `GRANT CREATE ON DATABASE`) e aumente `max_locks_per_transaction`.

**Template em cache entre execuções.** Migrar custa de 4 a 8 s por processo. Quando nada que molda o schema mudou desde a execução anterior, o template guardado em `var/test-template-cache` é restaurado sem migrar (ver [Isolamento da suíte](#isolamento-da-suíte)). Editar só testes mantém o cache; editar código do núcleo, de uma app, os settings ou trocar de servidor faz a próxima execução migrar de novo. `SKELETON_TEST_TEMPLATE_CACHE=0` desliga o cache (para depurar migrations); outro valor muda o diretório.

Para escrever um teste novo sem aumentar o tempo da suíte nem quebrar a execução paralela, veja [Escrever testes que mantêm a suíte rápida](#escrever-testes-que-mantêm-a-suíte-rápida).

`--shard I/N` (`tools/suite_shards.py`) roda só uma fatia dos arquivos de teste. Serve ao CI; localmente, `-n` basta.

Os tempos medidos estão em [Suíte de testes em menos de 10 minutos](testing-evidence.md#suíte-de-testes-em-menos-de-10-minutos-epic-199).

### Migrar e conferir um banco local

Os comandos `manage.py` abaixo operam no banco escolhido pelos settings e pelo ambiente, não nos bancos isolados da suíte: só os execute num banco local descartável.

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate python manage.py migrate
python manage.py sync_table_catalog
python manage.py check_skeleton_schema
```

Bootstrap usa getpass ou secret descartável forte; repita bootstrap/sync e compare UUIDs/ausência de reset de senha. Inicie runserver e faça login real, perfil, dashboard, convite, roles/grupos, Note opt-in e audit. API smoke usa cookie/CSRF dessa sessão, nunca force_login como prova de autenticação. Navegador deve mostrar estado final e fechar tabs.

## Isolamento da suíte

O isolamento evita que um teste apague dados de outro teste ou de uma instalação real. **Runtime** é a role limitada usada nas verificações de aplicação; **owner** é a role privilegiada usada somente para reconstruir o schema da fixture. Um banco “de teste” não se torna seguro só pelo nome: as URLs, o sufixo e o marcador de reset precisam satisfazer todas as barreiras abaixo.

`tests/conftest.py` oferece fixture `skeleton_db`. pytest-django fornece settings e desbloqueio explícito do ORM; não use criação/flush automático, `django_db`, TransactionTestCase de flush ou `--reuse-db` como atalho em schema protegido. O `pytest-xdist` é usado, mas só com um banco provisionado por worker (abaixo): dois processos nunca compartilham um schema.

No PostgreSQL, `TEST_DATABASE_URL` (runtime) e `TEST_MIGRATION_DATABASE_URL` (owner) devem apontar ao **mesmo banco descartável**, com nome terminado `_skeleton_test`, diferente de qualquer `DATABASE_URL`/`MIGRATION_DATABASE_URL` normal. Também é obrigatório `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`.

Com `pytest -n`, cada worker usa o seu banco, `<prefixo>_gwN_skeleton_test`, derivado dessas duas URLs por `tools/integration_database.py`. O nome mantém o sufixo, então todas as barreiras valem para ele. Um worker que não trocou de banco é recusado por `disposable_database_pair()`, e um banco de worker que não existe para a suíte antes de qualquer teste (ver [Rodar rápido e rodar completo](#rodar-rápido-e-rodar-completo)).

A fixture fecha conexões e recria **somente o schema `skeleton`** com owner a cada teste. Depois, todas as verificações de aplicação/RLS usam runtime. Ela não cria banco/roles nem reseta dados externos; DDL/reset são serializados. Se DSN, sufixo ou marcador faltar, pare e corrija o ambiente: a ausência não autoriza reset automático nem desabilitar a barreira.

O schema de cada teste vem de um **template da sessão**. O fixture `postgres_template` aplica as migrations uma única vez e captura estrutura e dados do schema com `pg_dump --schema=skeleton`, rodando como `skeleton_owner`. Em cada teste, `restore_schema()` apaga o schema, recria-o e executa esse SQL numa só transação, também como owner. O `GRANT CREATE` ao `skeleton_audit_writer` vale só durante a restauração, para transferir a ownership das funções de auditoria, como faz `InstallRecordInfrastructure`. Antes, cada teste rodava o `migrate` completo, cerca de 4 s por teste. A restauração roda com `synchronous_commit=off` na transação do owner: um schema de fixture perdido numa queda é recriado pelo teste seguinte. O template não é `--reuse-db`: nenhum teste herda dados de outro, e nada é truncado ou apagado linha a linha. `tests/integration_postgres/test_schema_template.py` compara o schema restaurado com o migrado (tabelas, colunas, constraints, índices, triggers, funções, policies, flags de RLS, owners, ACLs, sequências e dados) e mostra que a comparação falha quando falta uma policy, trigger, grant, FORCE RLS ou linha. A comparação de ACLs usa os privilégios efetivos: uma ACL `NULL` conta como `acldefault()` do owner. Isso é necessário porque um `REVOKE` pode deixar gravada uma ACL explícita igual ao padrão, que o `pg_dump` omite e o restore recria como `NULL`. O texto dos CHECK e dos índices com predicado ou expressão também passa por um ida-e-volta: o restore pode mover casts sem mudar a expressão (por exemplo, um `IN` sobre varchar). Por isso, o fingerprint recria cada CHECK (como `NOT VALID`) e cada um desses índices numa transação revertida e compara o texto que o PostgreSQL devolve.

Entre execuções locais, o template pode vir de um cache em disco (#350, `var/test-template-cache`, ignorado pelo Git). A chave é um SHA-256 de tudo que molda o schema migrado: todos os arquivos de código e dados de `skeleton/`, `config/`, `applications/` e `examples/`, `tools/integration_database.py` e `tests/conftest.py`, `INSTALLED_APPS` e `MIGRATION_MODULES`, `DJANGO_SETTINGS_MODULE` e as variáveis `SKELETON_*` do projeto (não as `SKELETON_TEST_*`, nem as URLs ou o worker), as versões de todas as bibliotecas instaladas e as versões do servidor e do `pg_dump`. Os valores só entram no hash; o arquivo guarda o SQL do dump e o fingerprint. Uma entrada de outra chave, de outro formato, malformada ou com meta-comando do psql é ignorada, e a suíte migra. A chave é ampla de propósito: uma edição fora de `tests/` custa uma migração; uma entrada esquecida custaria um schema errado. Por isso o CI desliga o cache e prova as migrations em toda execução. O cache não reaproveita dados entre testes: cada teste continua restaurando o schema inteiro.

O template exige `pg_dump` com versão igual ou maior que a do servidor (no CI vem de `postgresql-client`). Para usar outro binário, aponte `SKELETON_TEST_PG_DUMP` para ele. Com `SKELETON_TEST_POSTGRES_TEMPLATE=0`, a fixture volta a migrar a cada teste, o que serve para depurar migrations. Sem `pg_dump` e sem essa variável, a suíte falha com erro de uso, em vez de ficar lenta em silêncio.

O perfil de teste (`skeleton.conf.profiles.test_settings`) usa `skeleton.testing.hashers.FastPBKDF2PasswordHasher`: PBKDF2-SHA256 com 1.000 iterações, com o mesmo algoritmo e o mesmo formato de hash da produção. Com as iterações de produção, cada senha de fixture custava cerca de 0,3 s, e quase todo teste cria várias identidades. Esse hasher nunca entra nos settings de uma instalação.

Browser SSO tem fixture de sessão com DB isolado e servidor real, preservando commits até fim do cenário. Não use TestCase com atomic externa como prova de COMMIT/SET LOCAL; os testes transacionais precisam de commits verdadeiros.

## PostgreSQL runtime real

**Pré-requisitos:** PostgreSQL iniciado, banco descartável criado e provisionado pelo operador, duas roles/URLs do mesmo banco e marcador de reset. Os comandos seguintes usam arquivos privados da instalação de integração (`. .private/runtime/services.env`) ou, no desenvolvimento local, `eval "$(python tools/dev_postgres.py env)"`; em outra máquina, provisione equivalentes antes de carregá-los. Não há secrets de produção nesses exemplos.

```sh
set -a
. .private/runtime/services.env
set +a
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest \
  tests/integration_postgres \
  tests/integration/test_app_extensions.py \
  tests/integration/test_access_lifecycle_regressions.py \
  tests/integration/test_audit_findings.py \
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

## Escrever testes que mantêm a suíte rápida

O tempo da suíte é quase todo preparo, não teste. Na linha de base do epic [#199](https://github.com/alaorneto/skeleton/issues/199), 1320 s eram `setup` e 246 s eram as funções de teste: cada teste que pede `skeleton_db` recria o schema inteiro antes de rodar. Um teste novo custa principalmente o que ele pede para ser preparado. Custos medidos em 2026-11-12 (Windows 11, em série, PostgreSQL local de `tools/dev_postgres.py`):

| Peça | Custo por teste |
|---|---|
| Teste sem banco (`tests/unit`) | milissegundos |
| `skeleton_db` (restaurar o schema e abrir as apps) | 0,8 a 1,1 s |
| `build_security_fixture()` (quatro identidades, duas organizações, papéis, catálogo e notas) | 0,47 s |
| Uma identidade por `create_identity()` | 0,014 s |
| Subprocesso `python` com `django.setup()` | 2 s |
| `call_command("migrate")` dentro de um teste | 4 a 8 s |

### Custo

1. **Banco só quando a prova depende dele.** Lógica pura (validação, parsing, formatação, cálculo, montagem de mensagem ou de payload) vai para `tests/unit`, sem `skeleton_db`. O que depende de RLS, triggers, auditoria, CAS, isolamento A/B ou da role runtime continua em PostgreSQL com a role runtime. Não troque essa prova por mock do ORM, SQLite ou owner para ganhar tempo: o [checklist do `AGENTS.md`](../AGENTS.md#checklist-mínimo-de-nova-tabela) continua valendo.
2. **Um preparo, vários casos de leitura.** Com `skeleton_db`, cada caso de `@pytest.mark.parametrize` paga a restauração do schema. Quando os casos só leem o mesmo cenário, escreva um teste com o fixture `subtests` do pytest 9: o preparo roda uma vez e cada caso aparece no relatório com o próprio nome, também com `-n`. Exemplos de casos só de leitura: a matriz de atores e ações de um default deny, as rotas que devem responder 404 para outra organização, os campos que não podem aparecer numa resposta. Continue com `parametrize`, ou com testes separados, quando um caso escreve e o seguinte precisa do banco intacto.

   ```python
   def test_outra_organizacao_recebe_404_em_todas_as_rotas(skeleton_db, client, subtests):
       cenario = build_security_fixture()  # pago uma vez
       client.force_login(cenario.outsider)
       for rota in (f"/o/{cenario.a.pk}/", f"/o/{cenario.a.pk}/audit/", f"/api/v1/o/{cenario.a.pk}/catalog/"):
           with subtests.test(rota=rota):
               assert client.get(rota).status_code == 404
   ```

3. **Cenário mínimo.** Monte só o que as assertions usam. Para um teste de um ator numa organização, `create_identity()` e `create_organization()` bastam. `build_security_fixture()` é para a matriz A/B completa, e o kit da API de serviços tem `build_service_scenario()`. Não crie fixture de escopo `session` ou `module` com linhas no banco: o schema é recriado a cada teste, e compartilhar dados entre testes é exatamente o que o [isolamento da suíte](#isolamento-da-suíte) proíbe.
4. **Não migre dentro de um teste.** O template já entrega o schema migrado. Para DDL de prova use o fixture `owner_schema_editor`. A migração real é testada onde ela é o assunto (`tests/integration/test_scaffold_end_to_end.py`, `tools/smoke_postgres.py`), e `SKELETON_TEST_POSTGRES_TEMPLATE=0` serve para depurar migrations.
5. **Subprocesso só quando o processo novo é a prova.** Exemplos: importações num processo limpo (`test_observability_optional.py`) ou um comando de ponta a ponta. Para chamar um comando do Django no mesmo processo, use `call_command`.
6. **Nada de esperar o relógio.**
   - Tempo que passa: passe `now=` aos serviços que aceitam, ou substitua o `timezone` do módulo com `monkeypatch` (`test_api_tokens.py`, `test_ics_feed.py`).
   - Retentativa e backoff: injete a agenda ou o relógio em vez de dormir.
   - Outro thread ou outra transação: sincronize com `threading.Barrier` ou `Event` com timeout (`test_jobs_worker.py`, `test_admin_concurrency.py`). Espere o evento que prova o estado (o lock tomado), não um intervalo fixo.
7. **Senha de fixture.** O perfil de teste já usa PBKDF2 com 1.000 iterações (`skeleton.testing.hashers`). Não restaure o hasher de produção, a não ser num teste do próprio hashing.
8. **Sem rede externa.** Use servidores locais em `127.0.0.1` e transportes falsos (`test_outbound_http.py`, `test_webhook_delivery.py`, `test_file_scanning.py`).

### Execução paralela

Com `-n`, cada worker é um processo com um banco só seu, e os testes de um mesmo arquivo podem cair em workers diferentes, em qualquer ordem. Um teste novo precisa passar nessas condições:

- **Porta 0, nunca fixa:** `ThreadingHTTPServer(("127.0.0.1", 0), Handler)`, com a porta lida de `server.server_address[1]`. A única porta fixa hoje é a 8000 de `test_rd09_rendered_web_health_uses_permitted_production_host`, porque o healthcheck renderizado do Compose aponta para ela. Não crie outra.
- **Arquivos em `tmp_path` ou `tmp_path_factory`,** nunca num caminho fixo do repositório, do usuário ou do sistema.
- **Estado global restaurado no fim:** `monkeypatch`, `override_settings`, `try/finally` ao mexer em registros (eventos, jobs, apps) e signals conectados com `dispatch_uid` e desconectados no fim.
- **Nenhuma dependência de ordem:** um teste não supõe que outro rodou antes nem depois dele.
- **Banco pelas variáveis, nunca pelo nome:** use `disposable_database_pair()` ou `TEST_DATABASE_URL` e `TEST_MIGRATION_DATABASE_URL`. Dentro de um worker, elas já apontam para o banco dele, inclusive nos subprocessos.
- **Rode `-n auto` antes do PR:** um teste que só passa em série compartilha estado e precisa ser corrigido. Não o marque nem o rode à parte.

### Medir

- **Arquivo novo:** `python -m pytest tests/<caminho>/test_novo.py -q --durations=10`. Um `call` acima de 1 s, ou um `setup` acima de 2 s, pede uma linha no PR dizendo o que está sendo provado e por que não ficou mais barato. Hoje passam disso o scaffold de ponta a ponta (cerca de 30 s) e os testes de concorrência do PostgreSQL (2 a 3 s cada).
- **Suíte inteira:** `python -m pytest tests -q -n auto --durations=25` lista os mais lentos.
- **CI:** quando o maior job de teste passar de 8 minutos, aumente o número de fatias do núcleo em vez de cortar testes. São os itens `--shard I/N` da matriz em `.github/workflows/ci.yml`, um por fatia, todos com o mesmo `N`. Arquivos e pacotes novos entram numa fatia sozinhos.

### O que não fazer para ganhar tempo

Nenhum destes é otimização aceita:
- apagar, pular, ou marcar um teste como lento ou `xfail` para tirá-lo do caminho obrigatório;
- trocar a prova em PostgreSQL com a role runtime por SQLite, mock do ORM ou conexão do owner;
- usar `--reuse-db`, flush, `TRUNCATE` ou uma transação revertida no lugar do schema recriado;
- reexecutar um teste instável até ele passar, em vez de corrigir a causa.

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
- Catálogo sync idempotente preserva sysid/textos e não faz DDL.
- Conta: convite expired/replay/wrong email/corrida, público sem Membership, reset normal e tokens inválidos/SSO-only/inativo/deletado, auth_version, MFA/recovery single-use/cipher/rotação preservando máscara, admin sem bypass/enrollment sem deadlock.

- Tabelas operacionais com `organization` entram no inventário de RLS de `tools/postgres_assertions.py` (ENABLE/FORCE e policies); o schema check tem negativos em banco para os ramos operacional (trigger de auditoria a mais, RLS desligado) e append-only (policy de leitura alargada, grant de escrita ao runtime) em `tests/integration_postgres/test_schema_assurance.py`.

A aprovação exige evidência dos backends/protocolos relevantes; uma suíte limitada não autoriza alegar matrix completa.

## Evidência histórica

Os resultados de cada rodada, com comando, ambiente e números, estão em [testing-evidence.md](testing-evidence.md). Ao registrar uma rodada nova, acrescente a seção no topo daquele arquivo, logo depois do aviso.
