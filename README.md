# Skeleton Django

Base reutilizável de Django 6.1 para construir sistemas com login, várias organizações e controle de acesso aos dados. Ela resolve a infraestrutura comum: identificar a pessoa, separar dados de empresas/equipes, decidir quem pode ler ou alterar cada registro e conservar a história das mudanças. As regras do seu negócio ficam no projeto derivado, não no núcleo `skeleton`.

**Só PostgreSQL.** Todos os dados vivem no PostgreSQL, também no desenvolvimento e nos testes; o SQLite da instância guarda só a configuração (com segredos cifrados) e o estado do setup. Produção usa credenciais distintas para atender usuários (role runtime, sem `BYPASSRLS`) e alterar a estrutura do banco (owner, só nas migrations), além de Redis compartilhado, SMTP e HTTPS.

## Por onde começar

| Sua necessidade | Caminho de leitura |
|---|---|
| Experimentar o sistema | Siga “Iniciar localmente” abaixo e habilite o exemplo de anotações |
| Entender o vocabulário e as decisões | [Guia dos conceitos](docs/concepts.md), depois [arquitetura](docs/architecture.md) |
| Criar uma app de negócio | [Extensão](docs/extending.md), [modelo de dados](docs/data-model.md), [controle de acesso](docs/access-control.md) e [API de serviços](docs/service-api.md) |
| Fazer apps conversarem entre si ou usar notificações, identidade e outros serviços do núcleo | [API de serviços](docs/service-api.md) e o skill `service-api` |
| Integrar uma interface ou cliente HTTP | [API](docs/api.md) e [autenticação](docs/authentication-sso.md) |
| Instalar, atualizar ou operar produção | [Deployment](docs/deployment.md) e [testes/evidências](docs/testing.md) |

## Quem usa cada interface

Uma conta é global; o vínculo e o papel da pessoa pertencem a cada organização. “Administrar uma organização” não significa “administrar o servidor inteiro”.

| Interface | Quem a usa | Para quê |
|---|---|---|
| `/accounts/login/` | Pessoa com admissão autorizada | Entrar usando senha, SSO ou MFA conforme a política |
| `/o/<org>/` | Pessoa com vínculo ativo na organização indicada | Acessar dados e funcionalidades daquele tenant |
| `/notifications/`, `/o/<org>/notifications/` | Pessoa autenticada | Caixa de notificações de todas as organizações ou de uma, com lida, arquivada e contador no cabeçalho; abrir revalida o acesso ao conteúdo ([notificações](docs/notifications.md)) |
| `/o/<org>/apps/` | Administrador da organização | Ativar/desativar apps e ajustar configuração, sem instalar código |
| `/platform/` | Administrador da plataforma | Visão geral, usuários, organizações, aplicativos instalados, catálogo de tabelas e auditoria global, em views próprias do site |
| `/o/<org>/catalog/`, `/o/<org>/tables/<table_key>/` | Pessoa com permissão de tabela e vínculo ativo | Viewer de negócio: lista, detalhe e formulário, fora do admin |
| `/platform/updates/` | Administrador da plataforma, com agente provisionado | Versões instaladas, novas versões, envio de pacote, plano com prévia de impacto, aprovação e acompanhamento das operações |
| `/api/v1/` | Cliente com sessão Django válida | Operar recursos autorizados; não existe login independente por token |

No caminho `/o/<org>/`, substitua `<org>` pelo UUID da organização. O servidor valida o vínculo: conhecer o UUID não concede acesso.


## Iniciar localmente

Use Python >=3.12,<3.15; os locks de dependências são produzidos com Python 3.14. O virtualenv `.venv` separa as bibliotecas deste projeto das demais aplicações. Os locks fixam versões e hashes para repetir a instalação, não apenas instalar “a versão mais recente”.

SAML usa bibliotecas XML/xmlsec e suas dependências nativas. Se faltarem, instale os pré-requisitos; remover o provider esconderia uma instalação incompleta. Não instale as dependências do projeto globalmente.

Esse roteiro é para um checkout novo. Se já existe um `.env` configurado, preserve-o em vez de copiar o exemplo por cima. O checkout usa o código-fonte do núcleo; um projeto distribuído consome seu wheel, como explicado em [deployment](docs/deployment.md#distribuição-do-core-e-adoção-de-projetos-derivados).

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
.venv/bin/python tools/dev_postgres.py start   # PostgreSQL local descartável, sem Docker (usa uv)
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

Sem configuração, o `runserver` abre o **setup** em <http://127.0.0.1:8000/setup/>: copie o token que o processo imprimiu no terminal e siga as etapas. Use como runtime `postgres://skeleton_runtime:local-runtime@127.0.0.1:<porta>/local_skeleton_test` e como owner `postgres://skeleton_owner:local-owner@127.0.0.1:<porta>/local_skeleton_test` (a porta aparece no `start`). O setup migra, sincroniza o catálogo, confere o schema, cria o administrador e a organização e reinicia o `runserver` em modo normal ([deployment](docs/deployment.md#primeira-instalação-pelo-setup-146)).

Quem prefere a linha de comando continua podendo configurar pelo ambiente (o ambiente vence o SQLite da instância) e rodar, com `MIGRATION_DATABASE_URL` só nesse terminal:

```sh
DJANGO_SETTINGS_MODULE=config.settings.migrate .venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
.venv/bin/python manage.py bootstrap_skeleton --email admin@example.test --organization demo --name Demo
```

`migrate` aplica mudanças versionadas de estrutura; `sync_table_catalog` descreve as tabelas existentes; `check_skeleton_schema` verifica os controles de integridade/segurança instalados. São tarefas diferentes: sincronizar o catálogo não substitui migrations ou checks.

O comando `bootstrap_skeleton` cria o administrador inicial da plataforma, seu perfil, email verificado, uma organização e seu vínculo com o papel de administrador organizacional. Ele pede a senha no terminal com `getpass`, sem exibi-la. Automação pode usar `SKELETON_BOOTSTRAP_PASSWORD` por segredo temporário, nunca como argumento de CLI ou arquivo versionado.

Abra <http://127.0.0.1:8000/accounts/login/> e entre com essa conta. Repetir o bootstrap não redefine senha nem dados existentes. A promoção de plataforma é própria desse bootstrap; conceder o papel `organization_admin` a outra pessoa não lhe dá `is_staff`/`is_superuser`.

No desenvolvimento, o email é impresso no console: isso ajuda a testar, mas não prova entrega SMTP. O cadastro público é fechado por padrão; admissão acontece por convite ou SSO autorizado. Habilitar `SKELETON_PUBLIC_SIGNUP=true` cria apenas conta/perfil após confirmação de email: não cria organização, vínculo ou permissão de negócio.

### Exemplo de negócio

O exemplo Notes é uma app de negócio fora do núcleo. Defina `SKELETON_ENABLE_EXAMPLE_APP=true`, repita `migrate`, `sync_table_catalog` e `check_skeleton_schema`. Sua chave registrada `example_notes.note` passa a existir no gateway; apenas pessoas com as permissões correspondentes podem usá-la. [Extensão](docs/extending.md) explica modelo, manifesto, especificação, migration, página e API do exemplo.

O core/agente 0.4.0 lê `runtime.service_api`: toda app nova declara sua [API de serviços](docs/service-api.md), verificada pelos checks E018/E019 e pelo `package doctor`, com o kit `skeleton.testing.service_api` para os testes de contrato; manifestos históricos sem o campo continuam carregando com aviso.

O core/agente 0.3.0 integra `django-crispy-forms==2.7` com pack SSR próprio `skeleton_crud`, distribuído no wheel: apresentação apenas, sem mudar autorização, CAS ou persistência. O manifesto canônico `runtime.model_ui` define labels/abas/ordem e grade responsiva de 12 unidades, sem editor no admin ou arquivo UI paralelo. Notes 0.2.0 demonstra Código 4/Título 8/Texto 12, textarea com 8 linhas; apps elegíveis sem layout recebem defaults. [Contrato de dimensões e exemplos](docs/application-admin-ui-plan.md#5-formulário-e-detalhe-padronizados): `col_span` inteiro exato 1..12, `rows` 1..40 em textarea editável, omissão/`null` conserva defaults; `columns:1/2` define padrão 12/6 e mobile usa largura inteira. Atualize agente/core antes de dimensões: presença de suas chaves, inclusive `null`, exige piso >=0.3.0; v1/v2 históricos sem dimensões conservam compatibilidade e JSON assinado.

### Interface e CSS

A interface SSR usa Tailwind CSS 4 e Flowbite 4 com a paleta "Cobalto e Ardósia" e a fonte Inter, servidas da própria origem. O CSS compilado (`skeleton/web/static/skeleton/web/site.css`) e os assets vendorizados são versionados: o wheel não depende de Node. Depois de alterar `frontend/src/` ou templates, recompile e versione o resultado:

```sh
cd frontend && npm ci --ignore-scripts && npm run build
```

O job `assets` do CI roda `npm run check` e falha se o CSS estiver desatualizado. Tokens, componentes e regras: [interface e design](docs/ui-design.md).

### Ferramentas LSP de desenvolvimento

A configuração local em `.omp/lsp.json` integra BasedPyright e Ruff para Python, os servidores VS Code para HTML/CSS/JSON, YAML Language Server, Taplo para TOML, Dockerfile Language Server, Marksman para Markdown e Postgres Language Server para SQL.

Os executáveis ficam em ambientes isolados do usuário: BasedPyright/Ruff via `uv tool install`, servidores Node em `~/.local/share/skeleton-lsp` e Taplo/Marksman nativos no subdiretório `bin`. São ferramentas de desenvolvimento, não dependências de runtime. `.omp/` é configuração local ignorada pelo Git; os caminhos absolutos precisam ser ajustados ao reproduzir em outra máquina. Recarregue os servidores LSP depois de instalar ou alterar sua configuração.

BasedPyright usa `.venv/bin/python` para resolver Django e as demais dependências. HTML oferece análise estrutural, não validação semântica das tags de template Django. `.omp/postgres-language-server.json` desabilita conexões ao banco: a análise SQL não recebe DSNs/segredos nem substitui verificações de schema com a role runtime.

### Lint, pre-commit e CI

As regras do Ruff (lint, ordem de imports e formatter) ficam em `pyproject.toml`, e a mesma versão (0.17.4) roda no pre-commit e no CI. O Ruff não entra nos locks porque é ferramenta de desenvolvimento, isolada via `uv`/`pre-commit`. mypy, django-stubs e pytest-cov ficam no lock de desenvolvimento, porque precisam do Django instalado no mesmo ambiente.

```sh
uvx ruff@0.17.4 check
uvx ruff@0.17.4 format
uv tool install pre-commit && pre-commit install  # hooks pre-commit e commit-msg
.venv/bin/python -m mypy
eval "$(.venv/bin/python tools/dev_postgres.py env)"
SKELETON_ENABLE_EXAMPLE_APP=true .venv/bin/python -m pytest tests --cov
```

A tipagem é gradual: os módulos em `[tool.mypy] files` são checados em modo estrito. Ao anotar outro módulo, acrescente-o à lista e ao override estrito. A reformatação em massa está listada em `.git-blame-ignore-revs`; rode `git config blame.ignoreRevsFile .git-blame-ignore-revs` uma vez por checkout.

O hook `commit-msg` exige que cada commit referencie uma issue (`Refs #N`), e o workflow `.github/workflows/issue-link.yml` exige que o PR cite uma issue classificada; ver [fluxo de trabalho](docs/workflow.md).

O workflow `.github/workflows/ci.yml` roda o lint, a verificação do CSS compilado, a suíte inteira em PostgreSQL com a role runtime (Python 3.12 e 3.14), o smoke do runtime, `check --deploy`, a auditoria dos locks, o wheel e a imagem. Testes marcados `posix` e os do agente de deployment são pulados fora de Linux/macOS, porque o código de produção usa `os.O_NOFOLLOW` e `fcntl`. Ver [testes](docs/testing.md#integração-contínua).


### Atualizar locks (manutenção, não todo startup)

```sh
uv pip compile --python-version 3.14 --python-platform x86_64-manylinux_2_28 --generate-hashes requirements.in -o requirements.txt
uv pip compile --python-version 3.14 --python-platform x86_64-manylinux_2_28 --generate-hashes requirements-dev.in -o requirements-dev.txt
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

Ao mudar uma versão em `requirements.in`, `requirements-dev.in` ou `pyproject.toml`, regenere os dois locks no mesmo commit; para não atualizar outros pacotes de carona, acrescente `--upgrade-package <pacote>` aos dois comandos. Os PRs do Dependabot não regeneram os locks de forma confiável; o workflow `dependabot-locks` faz isso no branch do PR, depois de configurado o GitHub App ([testes](docs/testing.md#locks-dos-prs-do-dependabot)). Confira o diff antes do merge.

O runtime fixa Django 6.1.3, django-allauth 65.21.1, DRF 3.18.3, django-environ 0.14.0 e WhiteNoise 6.12.0. Protocolos, MFA e cifragem reutilizam bibliotecas, não parsers caseiros.

## Configuração

| Variável | Uso |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.development`, `.test`, `.production` ou `.migrate` |
| `DJANGO_SECRET_KEY` | Segredo Django privado; obrigatório em produção |
| `DJANGO_ALLOWED_HOSTS` | Lista de hosts explícitos, sem `*` em produção |
| `DATABASE_URL` | PostgreSQL, role runtime (sem fallback); normalmente gravado pelo setup no SQLite da instância |
| `MIGRATION_DATABASE_URL` | Credencial offline do schema; nunca entregue ao processo web |
| `CACHE_URL` | LocMem local; Redis compartilhado em produção |
| `EMAIL_URL`, `DEFAULT_FROM_EMAIL` | Console local; SMTP e remetente em produção |
| `SKELETON_PUBLIC_SIGNUP` | `false` por padrão; não concede vínculo organizacional |
| `SKELETON_ENABLE_EXAMPLE_APP` | `false` por padrão |
| `SKELETON_REQUIRE_ADMIN_MFA` | `false` em dev; `true` por padrão em produção |
| `MFA_ENCRYPTION_KEYS` | Chaves Fernet ordenadas, nova primeira, antigas para leitura |
| `IDENTITY_PROVIDER_ENCRYPTION_KEYS` | Keyring separado, no mesmo formato, para segredos de provedores de identidade gravados no banco; obrigatório quando houver esses segredos |
| `SSO_PROVIDER_CONFIG_PATH` | JSON privado dos provedores autorizados |
| `SKELETON_BOOTSTRAP_PASSWORD` | Secret apenas para o comando de bootstrap |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origens HTTPS explícitas quando necessário |
| `DJANGO_TRUST_PROXY` | Apenas atrás de proxy que remova cabeçalhos forjados |
| `DJANGO_HEALTH_HOST` | Host permitido enviado pelo healthcheck Compose |
| `DJANGO_TRUSTED_PROXY_COUNT`, `DJANGO_TRUSTED_CLIENT_IP_HEADER` | Topologia explícita de IP para rate limit; defaults não confiam em headers |
| `SKELETON_JSON_MAX_BYTES` | Limite efetivo do JSON API em bytes; default 1048576 |

Leia [deployment](docs/deployment.md) antes de usar PostgreSQL/Compose. `settings.migrate` separa a conexão de migração, que vem só de `MIGRATION_DATABASE_URL` no ambiente. Depois do setup, a configuração muda em `/platform/instance/` ou com `manage.py instance set`.

Após configuração/migrations de SSO, rode `validate_provider_bindings --database default` com settings runtime. Extensões carregam `manifest.json` canônico e registram specs/ADRs; `/o/<org>/apps/` administra versão declarada, ativação e configuração sem executar DDL. Desativar preserva dados e bloqueia navegação/gateway.


## Versões, pacotes e atualização

O núcleo é distribuído como wheel `django-skeleton`, separado de `config` e `applications`. Sua versão tem uma fonte de build em `pyproject.toml`; quando instalado, o código consulta os metadados da distribuição. Uma app usa `manifest.json` para declarar identidade, versão, compatibilidade, arquivos e migrations.

A plataforma envia um pacote assinado `.skapp` e aprova um plano em `/platform/updates/`. Um agente independente prepara outro release, cria backup, executa migrations/checks e reinicia os workers. O processo web não recebe credencial de migration nem executa pip/DDL. Antes de usar a interface, o operador precisa provisionar esse agente, suas raízes privadas e os publicadores confiáveis.

```sh
.venv/bin/python manage.py package versions --manifest examples/notes/manifest.json
.venv/bin/python manage.py package build \
  --app-root examples/notes --manifest examples/notes/manifest.json \
  --private-key /caminho/privado/publisher.pem --output /caminho/privado/notes.skapp
```

[Publicação e comandos](docs/extending.md#publicar-apps-e-releases-assinados); [provisionamento e recuperação](docs/deployment.md#agente-independente-de-deployment). Retirar uma app remove seu código ativo, não seus dados: UUIDs, auditoria, configurações e migrations históricas são conservados. Reinstalar exige reativação explícita por organização. Código local e overlays do derivado não são sobrescritos; divergência da base conhecida bloqueia a atualização em vez de apagar customizações.

O [perfil global gov.br](docs/authentication-sso.md#perfil-global-govbr) reutiliza allauth/OIDC, sem afiliação ou privilégios automáticos. Credenciais e homologação governamentais continuam pré-requisitos externos.

## Mapa e leitura

- [AGENTS.md](AGENTS.md): regras para agentes e projetos derivados.
- [Guia dos conceitos](docs/concepts.md): significado dos termos, diferenças importantes e ligação com os guias de referência.
- [Arquitetura](docs/architecture.md), [modelo de dados](docs/data-model.md) e [catálogo](docs/table-catalog.md).
- [Controle de acesso](docs/access-control.md), [autenticação/SSO/MFA](docs/authentication-sso.md) e [auditoria/soft delete](docs/audit-soft-delete.md).
- [API e concorrência](docs/api.md), [extensão](docs/extending.md), [deployment](docs/deployment.md) e [testes/evidências](docs/testing.md).
- [API de serviços entre apps](docs/service-api.md): contrato obrigatório de classes de serviço que cada app publica e que outras apps consomem em processo; procedimento no skill `service-api`.
- [Interface e design](docs/ui-design.md): tokens de cor, tipografia, componentes Flowbite, modo escuro, tema do admin e build do CSS.
- [Fluxo de trabalho](docs/workflow.md): plano → issues classificadas → branch/commits → PR → fechamento aprovado.
- [Administração e interfaces de apps](docs/application-admin-ui-plan.md): grupos de parâmetros no admin e viewer/layouts de negócio fora dele; renderer SSR e dimensões no core/agente 0.3.0.

Os resultados observados da integração e os comandos para reproduzi-los estão em [testing](docs/testing.md). PostgreSQL e Keycloak reais foram exercitados; mocks não substituem essas provas. Credenciais de produção e dados de fixture não fazem parte do template.
