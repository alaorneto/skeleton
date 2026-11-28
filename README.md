# Skeleton Django

Base reutilizável em Django 6.1 para sistemas com login, múltiplas organizações e controle de acesso por registro. O núcleo resolve a infraestrutura comum — identificar a pessoa, isolar os dados de cada empresa ou equipe, autorizar leituras e escritas e manter o histórico das alterações — enquanto as regras de negócio ficam no seu projeto derivado, nunca dentro de `skeleton`.

**Apenas PostgreSQL.** Todos os dados da aplicação vivem no PostgreSQL, inclusive no desenvolvimento e nos testes. O SQLite interno da instância guarda somente configuração (com segredos cifrados) e o estado do setup inicial. Em produção, a aplicação usa credenciais separadas para atender usuários (role *runtime*, sem `BYPASSRLS`) e para alterar o schema (role *owner*, restrita às migrations), além de Redis compartilhado, SMTP e HTTPS.

## Por onde começar

| O que você quer fazer | Onde ler |
|---|---|
| Rodar o sistema localmente | Siga [Iniciar localmente](#iniciar-localmente) abaixo e habilite o exemplo de anotações |
| Entender os conceitos e o vocabulário | [Guia dos conceitos](docs/concepts.md) e depois [Arquitetura](docs/architecture.md) |
| Criar uma app de negócio | [Extensão](docs/extending.md), [Modelo de dados](docs/data-model.md), [Controle de acesso](docs/access-control.md) e [API de serviços](docs/service-api.md) |
| Integrar apps entre si ou usar serviços do núcleo | [API de serviços](docs/service-api.md) e o skill `service-api` |
| Personalizar marca e tema da instância | [Criar e publicar um tema](docs/themes.md), [Aparência](docs/appearance.md) e o skill `instance-theme` |
| Integrar uma interface ou cliente HTTP | [API](docs/api.md) e [Autenticação e SSO](docs/authentication-sso.md) |
| Montar uma instância de produto (SaaS em níveis) | [Instância de produto](docs/product-instances.md) |
| Instalar, atualizar ou operar em produção | [Deployment](docs/deployment.md), [Manutenção](docs/maintenance.md), [Guia de atualização](docs/updates.md) e [Testes](docs/testing.md) |
| Relatar uma falha de segurança | [Política de segurança](SECURITY.md) — sempre pelo canal privado do GitHub, nunca em issue pública |

## Quem usa cada interface

A conta da pessoa é global, mas o vínculo e os papéis pertencem a cada organização. Administrar uma organização não dá acesso a outras organizações nem à administração do servidor.

| Interface | Quem usa | Para que serve |
|---|---|---|
| `/accounts/login/` | Pessoa com admissão autorizada | Entrar com senha, SSO ou MFA, conforme a política vigente |
| `/notifications/`, `/o/<org>/notifications/` | Pessoa autenticada | Ler e arquivar notificações de todas as organizações ou de uma só; ao abrir um item, o servidor revalida o acesso ([notificações](docs/notifications.md)) |
| `/profile/notifications/` | Pessoa autenticada | Definir preferências por tipo e canal: caixa in-app, email imediato, resumo diário ou semanal no fuso da pessoa e web push ([notificações](docs/notifications.md)) |
| `/profile/tokens/` | Pessoa autenticada | Emitir e revogar tokens pessoais de API, com escopos e validade; o segredo aparece uma única vez ([API](docs/api.md#gestão-de-tokens)) |
| `/join/` | Pessoa autenticada com email verificado | Ver organizações abertas a pedidos, solicitar ingresso e acompanhar ou cancelar pedidos próprios |
| `/join/<org>/<token>/` | Quem recebeu um link de ingresso | Entrar direto ou enviar pedido de ingresso; permite criar conta quando a organização libera |
| `/o/<org>/` | Membro ativo da organização | Acessar os dados e recursos da organização |
| `/o/<org>/catalog/`, `/o/<org>/tables/<table_key>/` | Membro com permissão na tabela | Consultar, visualizar e editar registros de negócio no viewer padronizado |
| `/o/<org>/flows/tasks/`, `/o/<org>/flows/instances/` | Membro ativo da organização | Assumir e concluir tarefas, aprovar etapas e acompanhar o histórico dos fluxos ([fluxos](docs/flows.md)) |
| `/o/<org>/apps/skeleton_calendar/`, `/me/apps/skeleton_calendar/` | Membro com permissão de calendário (quando a biblioteca da agenda está ativa por alguma app) | Usar calendários pessoais e compartilhados, recorrências, convites, lembretes, *Minha agenda* entre organizações e feed ICS ([agenda](skeleton/contrib/calendar/docs/spec.md)) |
| `/o/<org>/apps/` | Gestor de apps ou administrador da organização | Ativar ou desativar apps disponibilizadas pela plataforma e ajustar seus parâmetros, sem instalar código |
| `/o/<org>/requests/`, `/o/<org>/admission/` | Gestor de pessoas ou administrador (a política é exclusiva do administrador) | Aprovar ou recusar pedidos de ingresso e definir se a organização aceita pedidos, aparece no diretório e qual papel padrão atribui |
| `/o/<org>/admission/links/` | Quem gerencia convites | Criar e revogar links de ingresso com validade, limite de usos e entrada direta ou sob aprovação |
| `/o/<org>/admission/domains/` | Administrador da organização | Cadastrar e comprovar por DNS os domínios de email aceitos para admissão direta ou sob aprovação |
| `/o/<org>/identity/` | Administrador da organização | Consultar os provedores corporativos da organização e configurar JIT, aprovação, papel padrão e domínios permitidos |
| `/o/<org>/integrations/service-accounts/` | Quem possui `integrations.manage` | Gerenciar contas de serviço da organização e seus tokens |
| `/platform/` | Administrador da plataforma | Gerenciar usuários, organizações, aplicativos instalados, catálogo de tabelas e auditoria global em views próprias |
| `/platform/apps/<app>/availability/` | Administrador da plataforma | Controlar para quais organizações uma app instalada fica disponível (todas ou apenas selecionadas); revogar fecha o acesso preservando a escolha da organização |
| `/platform/identity-providers/` | Administrador da plataforma | Cadastrar, editar, desativar e testar provedores OIDC, SAML e LDAP das organizações, com segredos cifrados e somente de escrita |
| `/platform/domains/` | Administrador da plataforma | Consultar e revogar a posse de domínios verificados em caso de disputa |
| `/platform/audit/`, `/platform/catalog/` | Administrador ou auditor de plataforma | Consultar a auditoria global e o catálogo de tabelas (editar rótulos continua restrito ao administrador) |
| `/platform/updates/` | Administrador da plataforma ou operador de releases | Ver versões instaladas, escolher novas versões, enviar pacotes, revisar o plano de impacto, aprovar e acompanhar as operações executadas pelo agente |
| `/api/v1/` | Cliente com sessão Django válida ou token de API | Operar recursos autorizados; o token apenas restringe o papel da pessoa e não acessa áreas administrativas ([autenticação por token](docs/api.md#autenticação-por-token)) |

Nos endereços `/o/<org>/`, `<org>` é o UUID da organização. O servidor sempre valida o vínculo do usuário: conhecer o UUID não concede acesso.

## Iniciar localmente

Use Python >=3.12,<3.15 (os locks são gerados com Python 3.14). Crie um ambiente virtual `.venv` para isolar as dependências e instale sempre a partir dos locks com hashes, garantindo uma instalação reprodutível em vez de baixar versões arbitrárias.

O suporte a SAML depende das bibliotecas nativas de XML e `xmlsec`. Se faltarem na máquina, instale os pacotes de sistema necessários; não remova o provedor para mascarar uma instalação incompleta.

Este roteiro vale para um checkout novo. Se você já tem um `.env` configurado, preserve-o. O checkout executa o núcleo diretamente do código-fonte; projetos derivados distribuídos consomem o wheel do core, como descrito em [deployment](docs/deployment.md#distribuição-do-core-e-adoção-de-projetos-derivados).

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python tools/dev_postgres.py start   # PostgreSQL local descartável, sem Docker (usa uv)
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Sem configuração prévia, o `runserver` abre o **setup inicial** em <http://127.0.0.1:8000/setup/>. Copie o token exibido no terminal e siga as etapas na tela. Use a porta informada pelo `start` nas duas URLs do banco:

- **Runtime:** `postgres://skeleton_runtime:local-runtime@127.0.0.1:<porta>/local_skeleton_test`
- **Owner:** `postgres://skeleton_owner:local-owner@127.0.0.1:<porta>/local_skeleton_test`

O setup aplica as migrations, sincroniza o catálogo, verifica o schema, cria o administrador e a organização inicial e reinicia o servidor em modo normal ([deployment](docs/deployment.md#primeira-instalação-pelo-setup-146)).

Se preferir configurar pela linha de comando (variáveis de ambiente têm precedência sobre o SQLite da instância), defina `MIGRATION_DATABASE_URL` apenas nesse terminal e rode:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
.venv/bin/python manage.py bootstrap_skeleton --email admin@example.test --organization demo --name Demo
```

Cada comando tem uma função distinta: `migrate` aplica mudanças versionadas no schema; `sync_table_catalog` registra a descrição das tabelas existentes; `check_skeleton_schema` confere se os triggers, guards e políticas de segurança estão ativos. Sincronizar o catálogo não substitui migrations nem checks.

O `bootstrap_skeleton` cria o administrador inicial da plataforma (com perfil e email verificado), a primeira organização e o vínculo dele como administrador organizacional. A senha é pedida no terminal via `getpass`, sem ser exibida; em automações, passe `SKELETON_BOOTSTRAP_PASSWORD` como segredo temporário, nunca em argumentos de CLI ou arquivos versionados.

Acesse <http://127.0.0.1:8000/accounts/login/> e entre com essa conta. Repetir o bootstrap não sobrescreve senhas nem dados existentes. A promoção para administrador da plataforma ocorre somente nesse bootstrap: atribuir `organization_admin` a outra pessoa não lhe dá `is_staff` nem `is_superuser`.

Em desenvolvimento, os emails são impressos no console — útil para testar o fluxo, mas não substitui a verificação de entrega SMTP. O cadastro público vem desligado por padrão; novos usuários entram por convite ou SSO autorizado. Mesmo ativando `SKELETON_PUBLIC_SIGNUP=true`, a pessoa cria apenas a conta e o perfil após confirmar o email, sem receber organização, vínculo ou permissão de negócio.

### Exemplo de negócio

A app de exemplo **Notes** demonstra como uma app de negócio vive fora do núcleo. Para ativá-la localmente, defina `SKELETON_ENABLE_EXAMPLE_APP=true` e execute novamente `migrate`, `sync_table_catalog` e `check_skeleton_schema`. A tabela `example_notes.note` passa a existir no gateway, acessível apenas a quem tiver permissão. Veja [Extensão](docs/extending.md) para detalhes do modelo, manifesto, especificação, migration, páginas e API.

Todo núcleo publicado lê `runtime.service_api`: cada app nova declara sua [API de serviços](docs/service-api.md), validada pelos checks `E018`/`E019` e pelo `package doctor`, com testes de contrato via `skeleton.testing.service_api` (manifestos antigos sem o campo ainda carregam com aviso).

Para formulários e telas de detalhe, o núcleo inclui `django-crispy-forms==2.7` com o pack SSR `skeleton_crud` (apenas apresentação, sem alterar autorização, CAS ou persistência). O layout — rótulos, abas, ordem e grade responsiva de 12 colunas — é declarado em `runtime.model_ui` no manifesto, sem editor paralelo. No exemplo Notes, os campos usam `col_span` 4 (Código), 8 (Título) e 12 (Texto, textarea com 8 linhas); apps sem layout declarado recebem defaults automáticos. As regras de `col_span` (`1..12`), `rows` (`1..40`) e `columns` (`1` ou `2`) estão em [Contrato de dimensões e exemplos](docs/application-admin-ui-plan.md#5-formulário-e-detalhe-padronizados) e são entendidas por todo núcleo publicado (`FIELD_DIMENSIONS_CORE_MINIMUM` é `0.0.0`).

### Interface e CSS

A interface renderizada no servidor (SSR) usa Tailwind CSS 4 e Flowbite 4 com a paleta "Cobalto e Ardósia" e a fonte Inter servidas localmente, sem CDN. O CSS compilado (`skeleton/web/static/skeleton/web/site.css`) e os arquivos vendorizados são versionados para que o wheel não dependa de Node. Sempre que alterar `frontend/src/` ou templates, recompile e versione o resultado:

```sh
cd frontend && npm ci --ignore-scripts && npm run build
```

O job `assets` do CI executa `npm run check` e reprova CSS ou assets desatualizados. Tokens, componentes e diretrizes estão em [Interface e design](docs/ui-design.md).

### Ferramentas LSP de desenvolvimento

A configuração local em `.omp/lsp.json` integra BasedPyright e Ruff (Python), servidores VS Code (HTML, CSS e JSON), YAML Language Server, Taplo (TOML), Dockerfile Language Server, Marksman (Markdown) e Postgres Language Server (SQL).

Os executáveis ficam em ambientes isolados do usuário (`uv tool install` para BasedPyright e Ruff, `~/.local/share/skeleton-lsp` para servidores Node e `bin` para Taplo e Marksman) e não fazem parte das dependências de runtime. Como `.omp/` é ignorado pelo Git, ajuste os caminhos absolutos ao configurar outra máquina e recarregue os servidores após qualquer mudança.

O BasedPyright usa `.venv/bin/python` para resolver Django e bibliotecas. A análise de HTML é apenas estrutural e não valida semanticamente tags de template Django. Já `.omp/postgres-language-server.json` desliga conexões ao banco: a análise SQL não recebe DSNs nem segredos e não substitui as verificações de schema com a role runtime.

### Lint, pre-commit e CI

O Ruff (lint, ordenação de imports e formatação) é configurado em `pyproject.toml`, usando a mesma versão (`0.17.4`) no pre-commit e no CI. Ele fica fora dos locks por ser ferramenta isolada via `uv`/`pre-commit`; já `mypy`, `django-stubs`, `pytest-cov` e `pytest-xdist` ficam no lock de desenvolvimento porque precisam do Django instalado no mesmo ambiente.

Além da linha de base, o lint ativa `DTZ` (datas com fuso obrigatório), `BLE` (evitar `except Exception` genérico) e `S` (regras de segurança). Testes, `tools/`, o agente e o skill dispensam via `per-file-ignores` apenas o que seria ruído nesses contextos. No restante do código, qualquer dispensa precisa ser pontual e justificada (`# noqa: BLE001 - motivo em uma frase`), nunca ocultando SQL com entrada externa.

As versões fixadas precisam caminhar juntas — `tests/unit/test_workflow_pins.py` confere Ruff `0.17.4` em `.github/workflows/ci.yml`, `.pre-commit-config.yaml` e nos comandos abaixo, além de `uv 0.12.7` em `ci.yml`, `dependabot-locks.yml` e na seção "Atualizar locks". Actions do GitHub são fixadas por SHA de commit (`uses: dono/repo@<sha> # v7`) e a imagem base Python por digest; atualize-as pelo PR semanal do Dependabot. O job `lint` também roda os hooks do pre-commit (YAML, TOML, JSON, conflitos de merge, arquivos grandes e chaves privadas) em todo o repositório. A cobertura mínima (`fail_under` em `pyproject.toml`) é verificada no relatório combinado do job `coverage`.

```sh
uvx ruff@0.17.4 check
uvx ruff@0.17.4 format
uv tool install pre-commit && pre-commit install  # hooks pre-commit e commit-msg
.venv/bin/python -m mypy
eval "$(.venv/bin/python tools/dev_postgres.py env)"
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests -n auto --cov  # cerca de 4 min; ver docs/testing.md
```

A tipagem estática é gradual. Os módulos listados em `[tool.mypy] files` rodam em modo estrito — entre eles `tools/workflow`, `tools/dev_postgres.py`, o agente de deployment (com tipos de journal em `deployment_agent/types.py`), os serviços de acesso e identidade, os adapters do allauth, tokens de API, webhooks e a agenda. O mypy sempre analisa como Linux, pois o agente roda apenas em POSIX. Como a agenda é uma app first-party opcional, execute `SKELETON_BUNDLED_APPS=calendar python -m mypy` (como faz o CI) para carregar seus modelos no plugin do Django. Pacotes sem stubs publicados (`django-allauth`, `python-dateutil`) são tratados como `Any`. Para ocultar commits de reformatação no `git blame`, configure uma vez por checkout: `git config blame.ignoreRevsFile .git-blame-ignore-revs`.

Issues, branches, commits, PRs e releases seguem o skill `dev-workflow` (`.claude/skills/dev-workflow/`), obrigatório antes de planejar ou alterar arquivos versionados ([AGENTS.md](AGENTS.md#rastreabilidade-plano-issue-commit-versão-e-fechamento)). O hook `commit-msg` exige `Refs #N` em cada commit, o check "Verificação do PR" exige issue classificada e somente o label `release` dispara publicação de versão no merge. Detalhes próprios deste repositório estão em [Fluxo de trabalho](docs/workflow.md).

O workflow `.github/workflows/ci.yml` executa lint, verificação dos assets compilados, a suíte completa em PostgreSQL com a role runtime (Python 3.12 e 3.14), smoke de segurança do runtime, `check --deploy`, auditoria dos locks, construção dos wheels e da imagem Docker. Testes marcados `posix` e do agente de deployment são pulados fora de Linux/macOS porque dependem de `os.O_NOFOLLOW` e `fcntl`. Veja [Testes](docs/testing.md#integração-contínua).

### Atualizar locks (manutenção, não todo startup)

```sh
uv pip compile --python-version 3.14 --python-platform x86_64-manylinux_2_28 --generate-hashes requirements.in -o requirements.txt
uv pip compile --python-version 3.14 --python-platform x86_64-manylinux_2_28 --generate-hashes requirements-dev.in -o requirements-dev.txt
uv pip compile --python-version 3.14 --python-platform x86_64-manylinux_2_28 --generate-hashes requirements/ci-tools.in -o requirements/ci-tools.txt
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

Ao alterar dependências em `requirements.in`, `requirements-dev.in`, `requirements/ci-tools.in` ou `pyproject.toml`, regenere os locks no mesmo commit. Para atualizar apenas um pacote sem mexer nos demais, adicione `--upgrade-package <pacote>`. Em PRs abertos pelo Dependabot, o workflow `dependabot-locks` regenera os locks automaticamente no branch do PR após a configuração do GitHub App ([testes](docs/testing.md#locks-dos-prs-do-dependabot)); revise o diff antes do merge.

O runtime fixa Django 6.1.3, django-allauth 65.21.1, DRF 3.18.3, django-environ 0.14.0 e WhiteNoise 6.12.0. Protocolos de autenticação, MFA e criptografia reutilizam bibliotecas consolidadas em vez de implementações próprias.

## Configuração

| Variável | Uso |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.development`, `.test`, `.production` ou `.migrate` |
| `DJANGO_SECRET_KEY` | Segredo criptográfico do Django; obrigatório em produção |
| `DJANGO_ALLOWED_HOSTS` | Lista explícita de hosts aceitos; `*` é proibido em produção |
| `DATABASE_URL` | Conexão PostgreSQL com a role runtime (sem fallback); normalmente gravada pelo setup no SQLite da instância |
| `MIGRATION_DATABASE_URL` | Credencial offline da role owner para migrations; nunca exposta ao processo web |
| `CACHE_URL` | LocMem em desenvolvimento; Redis compartilhado em produção |
| `EMAIL_URL`, `DEFAULT_FROM_EMAIL` | Console em desenvolvimento; SMTP e remetente em produção. `EMAIL_URL` alimenta `MAILERS["default"]` ([esquemas aceitos](docs/deployment.md#health-email-e-operação)) |
| `SKELETON_PUBLIC_SIGNUP` | `false` por padrão; mesmo ativa, não cria vínculo organizacional |
| `SKELETON_ENABLE_EXAMPLE_APP` | `false` por padrão; liga a app de exemplo Notes |
| `SKELETON_REQUIRE_ADMIN_MFA` | `false` em dev; `true` por padrão em produção |
| `SKELETON_REQUIRE_MFA` | `false` por padrão; exige MFA de todos os usuários ([quem cadastra o MFA](docs/authentication-sso.md#mfa-cifrado-e-obrigatório)) |
| `SKELETON_REQUIRE_PLATFORM_ADMIN_MFA` | `false` em dev e testes; fixa em `true` em produção, sem opção para desligar |
| `MFA_ENCRYPTION_KEYS` | Lista ordenada de chaves Fernet (a primeira cifra; as demais leem valores antigos) |
| `IDENTITY_PROVIDER_ENCRYPTION_KEYS` | Keyring separado, no mesmo formato, para segredos de provedores de identidade gravados no banco |
| `SSO_PROVIDER_CONFIG_PATH` | Caminho do JSON privado com provedores SSO autorizados |
| `SKELETON_BOOTSTRAP_PASSWORD` | Senha temporária usada apenas pelo comando `bootstrap_skeleton` |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origens HTTPS confiáveis para CSRF, quando necessário |
| `DJANGO_TRUST_PROXY` | Ative somente atrás de proxy reverso que remova cabeçalhos forjados |
| `SKELETON_SETUP_TRUST_PROXY` | Equivalente usado apenas durante o modo setup atrás de proxy TLS ([setup](docs/deployment.md#primeira-instalação-pelo-setup-146)) |
| `DJANGO_HEALTH_HOST` | Host permitido enviado pelo healthcheck do Compose |
| `DJANGO_TRUSTED_PROXY_COUNT`, `DJANGO_TRUSTED_CLIENT_IP_HEADER` | Topologia explícita de proxies para extrair o IP real no rate limit (por padrão não confia em cabeçalhos encaminhados) |
| `SKELETON_JSON_MAX_BYTES` | Tamanho máximo aceito para payloads JSON na API, em bytes (padrão `1048576`) |

Leia [Deployment](docs/deployment.md) antes de operar com PostgreSQL ou Compose. `config.settings.migrate` isola a conexão de migrations, lida exclusivamente de `MIGRATION_DATABASE_URL` no ambiente. Depois do setup inicial, altere configurações da instância por `/platform/instance/` ou via `manage.py instance set`.

Após configurar ou migrar provedores de SSO, rode `validate_provider_bindings --database default` com settings runtime. Extensões carregam seu `manifest.json` canônico e registram especificações e ADRs; `/o/<org>/apps/` controla ativação e parâmetros sem executar DDL. Desativar uma app preserva os dados e bloqueia suas páginas e endpoints.

## Versões, pacotes e atualização

O núcleo é distribuído como o wheel `django-skeleton`, separado de `config` e `applications`. Sua versão tem fonte única em `pyproject.toml` e é lida dos metadados da distribuição instalada. Cada app declara sua identidade, versão, compatibilidade, arquivos e migrations no `manifest.json`.

Todo ambiente operacional roda o **agente de deployment**; apenas o desenvolvimento local dispensa o agente. Em Compose, `compose.sandbox.yaml` sobe tudo em um único host para avaliação, enquanto `compose.yaml` executa um nó de aplicação em alta disponibilidade com PostgreSQL e Redis em um servidor de dados dedicado ([alta disponibilidade](docs/high-availability.md); [backup e recuperação](docs/backup-recovery.md)). Em ambos os modos, o serviço `agent` instala o release publicado do núcleo e controla `web`, `worker` e `scheduler` por um inicializador próprio, sem montar o socket Docker. Com systemd, o operador provisiona o agente, seus diretórios privados e as chaves públicas confiáveis ([deployment](docs/deployment.md#primeira-partida-pelo-agente-containers)).

Em `/platform/updates/`, o administrador escolhe versões publicadas do núcleo, instala apps e aprova cada etapa a partir da prévia de impacto. O agente prepara o novo release em diretório isolado, gera backup, executa migrations e checks e reinicia os serviços. O processo web nunca recebe a credencial owner nem executa `pip` ou DDL.

```sh
.venv/bin/python manage.py package versions --manifest examples/notes/manifest.json
.venv/bin/python manage.py package build \
  --app-root examples/notes --manifest examples/notes/manifest.json \
  --private-key /caminho/privado/publisher.pem --output /caminho/privado/notes.skapp
```

Consulte [Publicação e comandos](docs/extending.md#publicar-apps-e-releases-assinados) e [Provisionamento e recuperação](docs/deployment.md#agente-independente-de-deployment). Retirar uma app remove o código ativo sem apagar seus dados: UUIDs, auditoria, configurações e migrations históricas são preservados, e uma reinstalação exige reativação explícita em cada organização. Arquivos locais e overlays do projeto derivado nunca são sobrescritos; qualquer divergência em relação à base conhecida bloqueia a atualização para proteger customizações.

O [perfil global gov.br](docs/authentication-sso.md#perfil-global-govbr) reutiliza allauth/OIDC sem conceder afiliação ou privilégios automáticos; credenciais e homologação governamentais continuam sendo pré-requisitos externos.

## Mapa e leitura

- [AGENTS.md](AGENTS.md): contrato normativo para agentes e projetos derivados.
- [Guia dos conceitos](docs/concepts.md): vocabulário do projeto, distinções essenciais e links para os guias de referência.
- [Arquitetura](docs/architecture.md), [Modelo de dados](docs/data-model.md) e [Catálogo de tabelas](docs/table-catalog.md).
- [Controle de acesso](docs/access-control.md), [Autenticação, SSO e MFA](docs/authentication-sso.md), [Portal de pessoas externas](docs/portal.md) e [Auditoria e exclusão lógica](docs/audit-soft-delete.md).
- [API e concorrência](docs/api.md), [Extensão](docs/extending.md), [Deployment](docs/deployment.md), [Testes](docs/testing.md) e [Evidência histórica das rodadas](docs/testing-evidence.md).
- Subsistemas do núcleo: [Jobs](docs/jobs.md), [Eventos](docs/events.md), [Notificações](docs/notifications.md), [Arquivos](docs/files.md), [Webhooks](docs/webhooks.md) e [Sequências](docs/sequences.md).
- [Exportação e importação CSV](docs/data-transfer.md) e [Direitos do titular (LGPD)](docs/privacy.md).
- [Limites da organização](docs/organization-limits.md): teto de membros e demais métricas, somando padrão da instância e fontes adicionais.
- [Endereços de organização](docs/organization-sites.md): subdomínio (`acme.<domínio base>`) e domínio próprio, incluindo DNS, TLS e escopo de site.
- [Checks](docs/checks.md): catálogo completo dos `skeleton.E/W`, o que validam e como corrigir.
- [API de serviços entre apps](docs/service-api.md): contrato obrigatório de serviços em processo entre apps e com o núcleo (procedimento no skill `service-api`).
- [Interface e design](docs/ui-design.md): tokens visuais, tipografia, componentes Flowbite, modo escuro e compilação do CSS.
- [Fluxo de trabalho](docs/workflow.md): regras específicas deste repositório sobre o skill `dev-workflow`.
- [Administração e interfaces de apps](docs/application-admin-ui-plan.md): telas de parâmetros, viewer padronizado de negócio, renderer SSR e dimensões de campos.

Os resultados históricos de integração ficam em [evidência histórica](docs/testing-evidence.md), e os comandos para reproduzi-los em [testes](docs/testing.md). As validações usam PostgreSQL e Keycloak reais — mocks não substituem essas provas. Credenciais de produção e dados de fixture não fazem parte do template.
