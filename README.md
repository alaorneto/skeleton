# Skeleton Django

Base reutilizável de Django 5.2 LTS para construir sistemas com login, várias organizações e controle de acesso aos dados. Ela resolve a infraestrutura comum: identificar a pessoa, separar dados de empresas/equipes, decidir quem pode ler ou alterar cada registro e conservar a história das mudanças. As regras do seu negócio ficam no projeto derivado, não no núcleo `skeleton`.

**Desenvolvimento não é produção.** SQLite permite começar sem serviços externos, mas não oferece RLS, a proteção PostgreSQL por linha, nem defesa contra quem controla o arquivo. Produção usa PostgreSQL com credenciais distintas para atender usuários e alterar a estrutura do banco, além de Redis compartilhado, SMTP e HTTPS. Trocar `DATABASE_URL` só troca a conexão: não transporta dados.

## Por onde começar

| Sua necessidade | Caminho de leitura |
|---|---|
| Experimentar o sistema | Siga “Iniciar localmente” abaixo e habilite o exemplo de anotações |
| Entender o vocabulário e as decisões | [Guia dos conceitos](docs/concepts.md), depois [arquitetura](docs/architecture.md) |
| Criar uma app de negócio | [Extensão](docs/extending.md), [modelo de dados](docs/data-model.md) e [controle de acesso](docs/access-control.md) |
| Integrar uma interface ou cliente HTTP | [API](docs/api.md) e [autenticação](docs/authentication-sso.md) |
| Instalar, atualizar ou operar produção | [Deployment](docs/deployment.md) e [testes/evidências](docs/testing.md) |

## Quem usa cada interface

Uma conta é global; o vínculo e o papel da pessoa pertencem a cada organização. “Administrar uma organização” não significa “administrar o servidor inteiro”.

| Interface | Quem a usa | Para quê |
|---|---|---|
| `/accounts/login/` | Pessoa com admissão autorizada | Entrar usando senha, SSO ou MFA conforme a política |
| `/o/<org>/` | Pessoa com vínculo ativo na organização indicada | Acessar dados e funcionalidades daquele tenant |
| `/o/<org>/apps/` | Administrador da organização | Ativar/desativar apps e ajustar configuração, sem instalar código |
| `/admin/` | Administrador da plataforma | Gerenciar identidade global, catálogo e operações da instalação |
| `/admin/system/releases/` | Administrador da plataforma, com agente provisionado | Ver versões, aprovar instalação/atualização e acompanhar operações |
| `/api/v1/` | Cliente com sessão Django válida | Operar recursos autorizados; não existe login independente por token |

No caminho `/o/<org>/`, substitua `<org>` pelo UUID da organização. O servidor valida o vínculo: conhecer o UUID não concede acesso.


## Iniciar localmente

Use Python >=3.12,<3.15; os locks de dependências são produzidos com Python 3.14. O virtualenv `.venv` separa as bibliotecas deste projeto das demais aplicações. Os locks fixam versões e hashes para repetir a instalação, não apenas instalar “a versão mais recente”.

SAML usa bibliotecas XML/xmlsec e suas dependências nativas. Se faltarem, instale os pré-requisitos; remover o provider esconderia uma instalação incompleta. Não instale as dependências do projeto globalmente.

Esse roteiro é para um checkout novo. Se já existe um `.env` configurado, preserve-o em vez de copiar o exemplo por cima. O checkout usa o código-fonte do núcleo; um projeto distribuído consome seu wheel, como explicado em [deployment](docs/deployment.md#distribuição-do-core-e-adoção-de-projetos-derivados).

```sh
python3.14 -m venv .venv
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
cp .env.example .env
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
.venv/bin/python manage.py bootstrap_skeleton --email admin@example.test --organization demo --name Demo
.venv/bin/python manage.py runserver 127.0.0.1:8000
```

`migrate` aplica mudanças versionadas de estrutura; `sync_table_catalog` descreve as tabelas existentes; `check_skeleton_schema` verifica os controles de integridade/segurança instalados. São tarefas diferentes: sincronizar o catálogo não substitui migrations ou checks.

O comando `bootstrap_skeleton` cria o administrador inicial da plataforma, seu perfil, email verificado, uma organização e seu vínculo com o papel de administrador organizacional. Ele pede a senha no terminal com `getpass`, sem exibi-la. Automação pode usar `SKELETON_BOOTSTRAP_PASSWORD` por segredo temporário, nunca como argumento de CLI ou arquivo versionado.

Abra <http://127.0.0.1:8000/accounts/login/> e entre com essa conta. Repetir o bootstrap não redefine senha nem dados existentes. A promoção de plataforma é própria desse bootstrap; conceder o papel `organization_admin` a outra pessoa não lhe dá `is_staff`/`is_superuser`.

No desenvolvimento, o email é impresso no console: isso ajuda a testar, mas não prova entrega SMTP. O cadastro público é fechado por padrão; admissão acontece por convite ou SSO autorizado. Habilitar `SKELETON_PUBLIC_SIGNUP=true` cria apenas conta/perfil após confirmação de email: não cria organização, vínculo ou permissão de negócio.

### Exemplo de negócio

O exemplo Notes é uma app de negócio fora do núcleo. Defina `SKELETON_ENABLE_EXAMPLE_APP=true`, repita `migrate`, `sync_table_catalog` e `check_skeleton_schema`. Sua chave registrada `example_notes.note` passa a existir no gateway; apenas pessoas com as permissões correspondentes podem usá-la. [Extensão](docs/extending.md) explica modelo, manifesto, especificação, migration, página e API do exemplo.

### Ferramentas LSP de desenvolvimento

A configuração local em `.omp/lsp.json` integra BasedPyright e Ruff para Python, os servidores VS Code para HTML/CSS/JSON, YAML Language Server, Taplo para TOML, Dockerfile Language Server, Marksman para Markdown e Postgres Language Server para SQL.

Os executáveis ficam em ambientes isolados do usuário: BasedPyright/Ruff via `uv tool install`, servidores Node em `~/.local/share/skeleton-lsp` e Taplo/Marksman nativos no subdiretório `bin`. São ferramentas de desenvolvimento, não dependências de runtime. `.omp/` é configuração local ignorada pelo Git; os caminhos absolutos precisam ser ajustados ao reproduzir em outra máquina. Recarregue os servidores LSP depois de instalar ou alterar sua configuração.

BasedPyright usa `.venv/bin/python` para resolver Django e as demais dependências. HTML oferece análise estrutural, não validação semântica das tags de template Django. `.omp/postgres-language-server.json` desabilita conexões ao banco: a análise SQL não recebe DSNs/segredos nem substitui verificações de schema com a role runtime.


### Atualizar locks (manutenção, não todo startup)

```sh
.venv/bin/python -m pip install pip-tools
.venv/bin/pip-compile --generate-hashes requirements.in
.venv/bin/pip-compile --generate-hashes --allow-unsafe requirements-dev.in
.venv/bin/python -m pip install --require-hashes -r requirements-dev.txt
```

O runtime fixa Django 5.2.18, django-allauth 65.19.7, DRF 3.18.3, django-environ 0.14.0 e WhiteNoise 6.12.0. Protocolos, MFA e cifragem reutilizam bibliotecas, não parsers caseiros.

## Configuração

| Variável | Uso |
|---|---|
| `DJANGO_SETTINGS_MODULE` | `config.settings.development`, `.test`, `.production` ou `.migrate` |
| `DJANGO_SECRET_KEY` | Segredo Django privado; obrigatório em produção |
| `DJANGO_ALLOWED_HOSTS` | Lista de hosts explícitos, sem `*` em produção |
| `DATABASE_URL` | SQLite local por padrão no desenvolvimento; PostgreSQL runtime em produção |
| `MIGRATION_DATABASE_URL` | Credencial offline do schema; nunca entregue ao processo web |
| `CACHE_URL` | LocMem local; Redis compartilhado em produção |
| `EMAIL_URL`, `DEFAULT_FROM_EMAIL` | Console local; SMTP e remetente em produção |
| `SKELETON_PUBLIC_SIGNUP` | `false` por padrão; não concede vínculo organizacional |
| `SKELETON_ENABLE_EXAMPLE_APP` | `false` por padrão |
| `SKELETON_REQUIRE_ADMIN_MFA` | `false` em dev; `true` por padrão em produção |
| `MFA_ENCRYPTION_KEYS` | Chaves Fernet ordenadas, nova primeira, antigas para leitura |
| `SSO_PROVIDER_CONFIG_PATH` | JSON privado dos provedores autorizados |
| `SKELETON_BOOTSTRAP_PASSWORD` | Secret apenas para o comando de bootstrap |
| `DJANGO_CSRF_TRUSTED_ORIGINS` | Origens HTTPS explícitas quando necessário |
| `DJANGO_TRUST_PROXY` | Apenas atrás de proxy que remova cabeçalhos forjados |
| `DJANGO_HEALTH_HOST` | Host permitido enviado pelo healthcheck Compose |
| `DJANGO_TRUSTED_PROXY_COUNT`, `DJANGO_TRUSTED_CLIENT_IP_HEADER` | Topologia explícita de IP para rate limit; defaults não confiam em headers |
| `SKELETON_JSON_MAX_BYTES` | Limite efetivo do JSON API em bytes; default 1048576 |

Leia [deployment](docs/deployment.md) antes de usar PostgreSQL/Compose. `settings.migrate` separa a conexão de migração; a configuração produtiva deve garantir `MIGRATION_DATABASE_URL` PostgreSQL explicitamente. O fallback SQLite dessa configuração existe para uso local, não para produção.

Após configuração/migrations de SSO, rode `validate_provider_bindings --database default` com settings runtime. Extensões carregam `manifest.json` canônico e registram specs/ADRs; `/o/<org>/apps/` administra versão declarada, ativação e configuração sem executar DDL. Desativar preserva dados e bloqueia navegação/gateway.


## Versões, pacotes e atualização

O núcleo é distribuído como wheel `django-skeleton`, separado de `config` e `applications`. Sua versão tem uma fonte de build em `pyproject.toml`; quando instalado, o código consulta os metadados da distribuição. Uma app usa `manifest.json` para declarar identidade, versão, compatibilidade, arquivos e migrations.

A plataforma envia um pacote assinado `.skapp` e aprova um plano em `/admin/system/releases/`. Um agente independente prepara outro release, cria backup, executa migrations/checks e reinicia os workers. O processo web não recebe credencial de migration nem executa pip/DDL. Antes de usar a interface, o operador precisa provisionar esse agente, suas raízes privadas e os publicadores confiáveis.

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

Os resultados observados da integração e os comandos para reproduzi-los estão em [testing](docs/testing.md). PostgreSQL e Keycloak reais foram exercitados; SQLite ou mocks não substituem essas provas. Credenciais de produção e dados de fixture não fazem parte do template.
