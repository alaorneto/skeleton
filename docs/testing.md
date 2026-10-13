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

A fixture fecha conexões, recria **somente o schema `skeleton`** com owner e aplica migrations. Depois, todas as verificações de aplicação/RLS usam runtime. Ela não cria banco/roles nem reseta dados externos; DDL/reset são serializados. Se DSN, sufixo ou marcador faltar, pare e corrija o ambiente: a ausência não autoriza reset automático nem desabilitar a barreira.

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
