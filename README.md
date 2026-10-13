# Skeleton Django

Template reutilizável de Django 5.2 LTS com páginas Django e API REST de sessão, identidade global, organizações, autorização por registros, catálogo físico, UUIDs, exclusão lógica e auditoria transacional. O núcleo não contém regras de negócio do projeto derivado. O exemplo de anotações é opt-in e fica fora dele.

**Limites importantes:** SQLite serve para desenvolvimento e início sem serviços. Ele não oferece RLS nem proteção contra quem controla o arquivo. PostgreSQL é o backend de produção, com credenciais separadas de runtime/migrations e RLS. Redis compartilhado, SMTP e HTTPS são requisitos de produção. Alterar `DATABASE_URL` não transporta dados.

## Iniciar localmente

Requisitos: Python >=3.12,<3.15; os locks são gerados com Python 3.14. Use dependências nativas de XML/xmlsec exigidas pelo extra SAML; não remova o provider para contornar uma instalação incompleta. Não instale pacotes globalmente.

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

O bootstrap solicita a senha com `getpass`; automação pode fornecer `SKELETON_BOOTSTRAP_PASSWORD` por secret temporário, nunca argumento de CLI ou arquivo versionado. Abra <http://127.0.0.1:8000/accounts/login/>. O bootstrap cria identidade de plataforma, email verificado, perfil, organização, vínculo e papel administrador. Repeti-lo não redefine a senha nem os dados existentes. O usuário administrador da organização não recebe `is_staff`/`is_superuser` por esse papel.

O backend de email local imprime mensagens no console. Isso é conveniência de desenvolvimento, não prova de entrega SMTP. Cadastro público está fechado; use convite ou SSO autorizado. Se habilitar `SKELETON_PUBLIC_SIGNUP=true`, o cadastro exige confirmação de email e não cria organização/vínculo.

### Exemplo de negócio

Defina `SKELETON_ENABLE_EXAMPLE_APP=true` no ambiente e reaplique `migrate`, `sync_table_catalog` e `check_skeleton_schema`. O exemplo `example_notes.note` aparece apenas a quem tem permissões de tabela e registro. [Extensão completa](docs/extending.md) apresenta modelo, spec, infraestrutura, HTML e API.

### Ferramentas LSP de desenvolvimento

A configuração local em `.omp/lsp.json` integra BasedPyright e Ruff para Python, os servidores VS Code para HTML/CSS/JSON, YAML Language Server, Taplo para TOML, Dockerfile Language Server, Marksman para Markdown e Postgres Language Server para SQL.

Os executáveis ficam em ambientes isolados do usuário: BasedPyright/Ruff via `uv tool install`, os servidores Node em `~/.local/share/skeleton-lsp` e Taplo/Marksman nativos no subdiretório `bin`. Não fazem parte dos locks nem das dependências de runtime. Os caminhos absolutos dessa configuração são locais à estação; ajuste-os ao reproduzir em outra máquina. Após instalar ou alterar a configuração, recarregue os servidores LSP.

BasedPyright usa `.venv/bin/python` para resolver Django e as demais dependências. HTML oferece análise estrutural, não validação semântica das tags de template Django. `.omp/postgres-language-server.json` desabilita conexões ao banco: a análise SQL não recebe DSNs/segredos nem substitui verificações de schema com a role runtime.

Smoke local: os dez servidores iniciaram; definição Python resolveu Django dentro da `.venv`, símbolos de HTML/CSS/JSON/YAML/TOML/Dockerfile/Markdown responderam e os diagnósticos de `manage.py` e `deploy/postgres/bootstrap.sql` retornaram sem problemas. Isso verifica as ferramentas, não a aplicação inteira.

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

`django-skeleton` é distribuído como wheel separado de `config`/`applications`; sua versão vem de `pyproject.toml`/metadata instalada. Apps declaram identidade, versão, compatibilidade, arquivos e migrations no manifesto. `/admin/system/releases/` mostra inventário, consulta índices assinados e oferece upload, preview, aprovação, histórico, instalação, atualização, retirada e reinstalação de código. O agente independente precisa ser provisionado com publishers aprovados, ambientes separados e backup; o web não recebe a credencial de migration nem executa DDL/pip.

```sh
.venv/bin/python manage.py package versions --manifest examples/notes/manifest.json
.venv/bin/python manage.py package build \
  --app-root examples/notes --manifest examples/notes/manifest.json \
  --private-key /caminho/privado/publisher.pem --output /caminho/privado/notes.skapp
```

[Publicação e demais utilitários](docs/extending.md#publicar-apps-e-releases-assinados); [provisionamento/recuperação do agente](docs/deployment.md#agente-independente-de-deployment). Retirada conserva dados, audit, configurações e migrations históricas; reinstalação não reativa organizações automaticamente. Atualizações não sobrescrevem overlays nem apps locais. Forks e alterações não inventariadas bloqueiam a operação.

O [perfil global gov.br](docs/authentication-sso.md#perfil-global-govbr) reutiliza allauth/OIDC, sem afiliação ou privilégios automáticos. Credenciais e homologação governamentais continuam pré-requisitos externos.

## Mapa e leitura

- [AGENTS.md](AGENTS.md): regras para agentes e projetos derivados.
- [Arquitetura](docs/architecture.md), [modelo de dados](docs/data-model.md) e [catálogo](docs/table-catalog.md).
- [Controle de acesso](docs/access-control.md), [autenticação/SSO/MFA](docs/authentication-sso.md) e [auditoria/soft delete](docs/audit-soft-delete.md).
- [API e concorrência](docs/api.md), [extensão](docs/extending.md), [deployment](docs/deployment.md) e [testes/evidências](docs/testing.md).

Os resultados observados da integração e os comandos para reproduzi-los estão em [testing](docs/testing.md). PostgreSQL e Keycloak reais foram exercitados; SQLite ou mocks não substituem essas provas. Credenciais de produção e dados de fixture não fazem parte do template.
