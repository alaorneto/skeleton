# Criar um projeto e adicionar negócio

Uma app de negócio é um módulo Django do projeto derivado — como tarefas ou anotações — construído sobre identidade, isolamento multi-tenant, autorização e auditoria fornecidos pelo núcleo. Este guia organiza o desenvolvimento em três frentes: escrever modelos e serviços seguros, declarar como a app se integra ao núcleo e publicar artefatos assinados para instalação. Antes de começar, consulte [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados).

## Copiar o template sem renomear o núcleo

Crie seu projeto usando o botão de template Git do provedor ou um clone versionado. Nunca copie `.env`, `.venv`, bancos locais, `.private`, cookies ou chaves de fixture.

```sh
git clone "$TEMPLATE_REPO" meu-projeto
```

Ajuste a identidade visual e os nomes do projeto, as variáveis privadas, os hosts, o remetente de e-mail, os provedores de identidade (IdPs) e os segredos. Preserve intactos os módulos `skeleton`, os app labels do núcleo e `AUTH_USER_MODEL`. Registre as apps do projeto em `PROJECT_APPS` (`config/settings/project.py`) e suas rotas em `config/urls_project.py`. Não modifique o núcleo para acomodar regras de negócio específicas — o núcleo nunca importa `applications` nem `examples`.

```python
# config/settings/project.py
PROJECT_APPS = ["applications.tasks.apps.TasksConfig"]
```

Defina um `AppConfig` com `name="applications.tasks"` e um `label` exclusivo (como `project_tasks`), sem reutilizar labels do núcleo nem do exemplo, pois o label identifica a app no Django e nas migrations. Registre metadados em `ready()` sem consultar o banco. Tanto views quanto jobs devem chamar serviços autorizados, nunca managers internos que contornem as políticas.

## Começar uma app com `skeleton_startapp`

O ponto de partida recomendado para qualquer app nova é o gerador do núcleo, em vez de copiar `examples/notes` ou criar arquivos manualmente:

```bash
python manage.py skeleton_startapp project_tasks --app-id com.example.tasks --publisher example-publisher --model Task
```

O `.gitignore` deste repositório ignora o conteúdo de `applications/`, exceto `applications/__init__.py`, para que checkouts locais de apps desenvolvidas em repositórios próprios não sujem o `git status` do núcleo. Em um projeto derivado que versiona suas apps no mesmo repositório, remova do `.gitignore` as linhas `applications/*` e `!applications/__init__.py`; do contrário, o Git não rastreará a app gerada.

O gerador cria `applications/<package>/` (o nome do pacote segue o label por padrão; altere com `--package-name` e o diretório-pai com `--directory`) contendo:

- `apps.py`: `AppConfig` com `name = __package__` e o label informado; `ready()` registra o manifesto com `register_app` e um `TableSpec` com `read_fields`, `write_fields` e `sensitive_fields` explícitos (tuplas de nomes; listas são convertidas, mas string solta ou item inválido levanta `TypeError`). O check `E008` exige declarar como sensível todo campo não relacional chamado `password`, `token`, `token_digest`, `secret`, `private_key` ou `recovery_codes` (os mesmos nomes redigidos pela auditoria);
- `models.py`: modelo `SecuredRecord` mínimo (`code` único entre registros vivos da organização e `title`);
- `service_api.py`: `<Model>Service` (`ServiceAPI`) com `<model>_count`, `list_<model>_summaries` e `get_<model>`, DTO `<Model>Summary` e `app_label` da app;
- `views.py`, `urls.py` e `api_urls.py`: página `tenant_page` e resumo `TenantAPI` chamando o serviço, sem consultar o modelo diretamente;
- `tests/test_service_api.py`: teste de contrato `assert_service_contract(<Model>Service)` e os seis testes de cenário para os três métodos (caminho autorizado, default deny, registro privado como inexistente, isolamento A/B, app desativada e ator divergente);
- `tests/conftest.py`: fixture `skeleton_db` distribuída no wheel (`skeleton.testing.database`), que recria o schema a cada teste (migra uma vez por processo, restaura de um template e usa um banco por worker com `pytest -n`) executando as asserções na role runtime;
- `templates/<app_label>/home.html` e `migrations/__init__.py`;
- `docs/spec.md`, `docs/adr.md` e `docs/service-api.md` com as seções exigidas pelo check `E012` (texto inicial para substituir). Links para a documentação do núcleo usam endereços absolutos na tag da versão instalada (`https://github.com/alaorneto/skeleton/blob/v<versão>/docs/...`), nunca caminhos relativos externos à app, funcionando também em repositórios próprios e pacotes publicados;
- `manifest.json` v2 gerado por `manifest_template`, com `runtime.urls`, `runtime.api_urls`, `runtime.service_api` e uma entrada de navegação apontando para os módulos criados. A compatibilidade nasce no piso de `runtime.service_api` (`SERVICE_API_CORE_MINIMUM`, `0.0.0`) até `<1.0.0`, e `default_enabled` nasce `false` (o campo é obsoleto e ignorado, e `package doctor` alerta quando está `true`).

O comando aplica os mesmos validadores de `skeleton.system.packages` sobre label, pacote, IDs e modelo, recusa namespaces reservados (`sk_*`, `skeleton`, `config`…) e diretórios já existentes, e nunca edita settings. Os templates de geração vivem em `skeleton/system/app_template/` dentro do wheel do core. Ao concluir, imprime os próximos passos:

1. adicionar `applications.<package>.apps.<Package>Config` em `PROJECT_APPS` (`config/settings/project.py`);
2. `python manage.py makemigrations <app_label>`;
3. `python manage.py record_infrastructure <app_label> --write` (migration de infraestrutura; veja [Migration de segurança](#migration-de-segurança));
4. `python manage.py migrate`;
5. `python manage.py sync_table_catalog`;
6. `python manage.py check_skeleton_schema`;
7. confirmar que as migrations da app não criam view, função acessível ao runtime, privilégio por coluna nem trigger em tabela técnica no schema `skeleton` (reprovado por `check_skeleton_schema`); atualizar `migrations.leaves` no manifesto, revisar `spec.md` e `adr.md` e completar a API de serviços (`service_api.py` e `docs/service-api.md`; veja [API de serviços da app](#api-de-serviços-da-app));
8. executar a suíte da app: `python -m pytest applications/<package>/tests` com `TEST_DATABASE_URL` (role `skeleton_runtime`), `TEST_MIGRATION_DATABASE_URL` (role `skeleton_owner`) e `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`, em um banco `*_skeleton_test` provisionado como em `deploy/postgres/bootstrap.sql` (incluindo `GRANT CREATE ON DATABASE <banco> TO skeleton_owner` para recriar o schema); veja [Isolamento da suíte](testing.md#isolamento-da-suíte);
9. `python manage.py package doctor applications/<package>` (veja [Diagnosticar antes de empacotar](#diagnosticar-antes-de-empacotar));
10. publicar com `package build` (veja [Publicar apps e releases assinados](#publicar-apps-e-releases-assinados)).

**Repositório próprio (`--standalone`).** Com `--standalone`, o gerador cria a raiz de um repositório Git em que a app é o próprio pacote (`<directory>/<package>/`), acrescentando:

- `.gitattributes` com `export-ignore` para `.gitattributes`, `.gitignore`, `.github`, `pyproject.toml` e `README.md` (garantindo que `package build --git-ref` empacote apenas a app);
- `.gitignore` (caches, `.venv/`, `dist/`, `*.skapp`, `*.pem`, `*.key`, `.env`);
- `pyproject.toml` apenas com configuração de `pytest` (`DJANGO_SETTINGS_MODULE = "skeleton.testing.host.settings"` e plugin `skeleton.testing.host.pytest_plugin`, que importa os testes como `applications.<package>.tests.*`) e `ruff`, sem `[project]`;
- `.github/workflows/ci.yml` (serviço PostgreSQL, checkout no diretório com nome do pacote, instalação do wheel do núcleo via `SKELETON_VERSION`, `provision`, `pytest` e `doctor --git-ref HEAD`; o job de publicação vem comentado para o operador escolher onde guardar a chave);
- `README.md` com os passos locais, substituindo `PROJECT_APPS` por `SKELETON_DEV_APPS`.

O código gerado é apenas um ponto de partida: revise allowlists e `sensitive_fields` a cada campo novo e implemente todos os testes de isolamento, autorização e concorrência exigidos pelo `AGENTS.md`.

### Manifest, ativação e superfícies próprias

O manifesto (`manifest.json`) é o contrato versionado da app: identifica o componente e declara tudo o que ele exige e expõe. Cada app lê o arquivo canônico com `AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self)` e o registra em `AppConfig.ready()`, separadamente dos `TableSpec` de suas tabelas. O contrato declara ID estável, versão, `publisher`, label e pacote, faixas de compatibilidade e dependências **por app ID com faixa de versão** (nunca por nome de distribuição Python).

A seção `runtime` define campos de configuração escalares, navegação, módulos de URLs e API, política de habilitação e documentação. No formato 2, inclui também `administration` (seções e telas de parâmetros) e `model_ui` (apresentação de negócio fora do admin). `ConfigField`, `MenuEntry` e specs imutáveis de apresentação derivam diretamente do manifesto canônico — não mantenha definições duplicadas. O registro em `ready()` nunca consulta o banco nem executa DDL, e todas as dependências precisam estar instaladas em versões compatíveis e sem ciclos.

**Ícones de menu.** No formato 2, cada entrada de `runtime.navigation` pode declarar seu ícone usando um de dois campos opcionais (mutuamente exclusivos): `icon` (um conceito do núcleo em `CONCEPT_ICONS` de `skeleton/common/icons.py`, como `calendar` ou `files`) ou `icon_svg` (caminho para um `.svg` próprio da app fora de `migrations/`, `templates/`, `docs/` e `wheels/`). O SVG segue o estilo de traço do núcleo: `viewBox="0 0 24 24"`, até 32 primitivas geométricas diretas (`path`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `rect`) e até 8 KiB; preenchimentos, cores, `style`, grupos, texto, referências, scripts, DOCTYPE e entidades são recusados (`skeleton/common/stroke_icons.py`). O arquivo original nunca é entregue ao navegador: o núcleo reconstrói a geometria em seu próprio `<svg>`. Pela regra **um conceito, um ícone**, um SVG igual ao do núcleo ou ao de outra app é recusado. Sem o campo, a entrada recebe o ícone `apps`. O recurso exige piso `NAVIGATION_ICON_CORE_MINIMUM` (0.10.0); o conceito e o arquivo são validados por `skeleton.E051`, e ícones inválidos nunca derrubam o processo (fazem fallback para `apps`).

**Temas da instância embarcados.** No formato 2, `runtime.instance_themes` declara até 4 temas trazidos pela app (`{"slug": "aurora", "path": "themes/aurora"}`), com `slug` único em `[a-z][a-z0-9-]{0,31}` e `path` dentro da app (fora de `migrations/`, `templates/`, `static/`, `docs/` e `wheels/`, sem sobreposição entre temas), no formato do [pacote de tema](appearance.md#pacote-de-tema). Exige piso `INSTANCE_THEMES_CORE_MINIMUM` (0.3.0) em `compatibility.skeleton`. O `theme.json` deve declarar `id` igual a `<app_id>.theme.<slug>` e faixa `compatibility.skeleton` cobrindo a menor versão do núcleo aceita pela app. Temas inválidos são bloqueados em `package build`, reportados em `package doctor` (`instance_theme_invalid`) e convertidos em `skeleton.E047` na carga (ficando fora do catálogo sem derrubar o processo). O roteiro completo de autoria — incluindo `skeleton_startapp --with-theme <slug>` e `skeleton.testing.appearance.assert_app_themes_valid` — está em [criar e publicar um tema](themes.md#publicar-junto-de-uma-app), com exemplo em `examples/themes/caderno/`.

**Rotas, guardas e templates.** As rotas da app são montadas antes dos gateways, exclusivamente sob `/o/<org>/apps/<app_label>/` e `/api/v1/o/<org>/apps/<app_label>/`, sempre recebendo contexto organizacional validado. Antes de executar qualquer view (mesmo sem `tenant_page`), o guarda do núcleo exige:

- login (visitante anônimo redireciona para login com `next`; API responde 403 JSON);
- organização válida no contexto (404 para quem não possui vínculo);
- app ativa na organização;
- MFA cadastrado quando `SKELETON_REQUIRE_ADMIN_MFA` está ativo e o usuário possui alguma capacidade (redirecionamento para `mfa_index` no HTML; 403 `mfa_required` na API).

Ainda assim, `tenant_page` e `TenantAPI` continuam recomendados pelo tratamento padronizado de erros e fuso horário. `config.urls_project` também precede os gateways para não perder rotas do projeto. Não crie rotas organizacionais fora desse contrato.

Rotas web de organização declaradas manualmente (por exemplo em `config/urls_project.py`) devem começar com `ORG` de `skeleton.tenancy.routing` (`path(f"{ORG}relatorios/", ...)`), nunca com o literal `o/<uuid:organization_id>/`: assim `reverse` e `{% url %}` produzem `/o/<uuid>/…` no domínio principal e a URL limpa no endereço próprio da organização. Nunca monte `/o/<uuid>/` por concatenação de string; use `organization_prefix(organization_id)`. Rotas de apps montadas pelo núcleo já utilizam o conversor, e rotas de API permanecem em `api/v1/o/<uuid:organization_id>/…`. No endereço próprio da organização, rotas globais do projeto fora de `ORG` só respondem se seu prefixo constar em `SKELETON_ORGANIZATION_SITE_EXTRA_PREFIXES` ([controle de acesso](access-control.md#host-de-organização)); as demais retornam 404 lá, mantendo funcionamento normal no domínio principal.

Nos templates de apps:

- finalize o título com `· {{ site_name }}` ([interface](ui-design.md#título-e-marca-no-endereço-da-organização)), nunca com o literal "Skeleton": no endereço da organização exibe o nome dela, e no domínio principal o nome do produto configurado pela plataforma ([marca da instância](appearance.md));
- para logo, cor ou rodapé, utilize `instance_brand` (`name`, `footer_text`, `color`, `logo_url`, `logo_dark_url`, `icon_url`), disponibilizado por `organization_shell`, sem fixar "Skeleton" nem cores literais no HTML; nunca declare rotas sob a raiz reservada `brand/`;
- siga o [vocabulário visual](ui-design.md#vocabulário-visual) e o mapa de [ícones](ui-design.md#ícones): estados via `status_badge`, marcadores via `marker` e ícones de conceito via `"<conceito>"|concept_icon` (ícones de ação, como `plus` e `trash`, vão pelo nome), sem redesenhar ícones para conceitos que o núcleo já possui.

**Instalação global vs. ativação por organização.** Instalar torna o código e as migrations disponíveis no ambiente compartilhado; ativar autoriza uma organização específica a usar a app, sem instalar pacotes Python nem executar DDL. `/o/<org>/apps/` gerencia ativação e configuração exibindo a versão declarada. O estado fica persistido em `AppInstallation` (`OrganizationRecord` com UUID, infraestrutura de migration e auditoria). Se uma configuração persistida deixar de caber no manifesto (por exemplo, campo removido), apenas aquela app naquela organização é bloqueada (`configuration_valid` falso, recusa em `require_application_enabled` e remoção do menu): a tela explica o problema e nem página nem API permitem sobrescrever — a correção deve vir por data migration.

A configuração JSON é integralmente redigida na auditoria; segredos pertencem exclusivamente à configuração privada do deployment, nunca ao JSON organizacional. Sem linha persistida em `AppInstallation`, a app permanece **desativada** na organização (`default_enabled` no manifesto não ativa apps). Na API, `If-Match` é obrigatório — use revisão virtual `"0"` na primeira gravação e a revisão atual nas seguintes — garantindo CAS, enquanto o bloqueio de `Organization` serializa a validação de dependências.

Configurar apps exige a capacidade `apps.manage` (gestor de apps `app_manager` ou administrador); quem não a possui recebe 404 no painel e na API. O painel lista apenas apps disponíveis para a organização; uma app ativa posteriormente retirada pela plataforma aparece sinalizada, com negócio bloqueado, aceitando apenas desativação (outros `PUT` retornam 400 `app_unavailable`). Telas organizacionais `custom` com `organization_admin_only` no manifesto também exigem `apps.manage` (o nome do campo permanece por compatibilidade).

O estado das apps de uma organização (`application_states()`: disponibilidade, ativação, configuração e estado efetivo com dependências) é lido em número fixo de consultas e memoizado apenas durante a requisição atual (qualquer escrita na requisição desativa o memo, e nada é cacheado entre requisições, garantindo efeito imediato de revogações). `navigation_for_user`, `require_application_enabled` e `authorized_queryset` consomem esse estado, e o menu avalia permissões de tabela em consulta única (`table_permissions`).

Desativar uma app bloqueia suas rotas próprias, o gateway e as policies e remove suas entradas de menu, mas preserva registros, UUIDs e ACLs. Como o bloqueio ocorre na camada de URL (`guard_application_urls` para `runtime.urls` e `guard_application_api_urls` para `runtime.api_urls`), até endpoints que não chamam `authorized_queryset` fecham automaticamente. Com a app (ou dependência transitiva) desativada, a API retorna 404 neutro (`{"code": "not_found"}`), enquanto `/api/v1/o/<org>/apps/<app_label>/configuration/` permanece acessível ao administrador para reativação. Reativar não concede `TablePermission`. Operações globais de instalação, atualização e retirada do código ocorrem em `/platform/updates/` via agente separado; o processo web nunca roda instalador nem DDL, e remoção de dados ou schema é estritamente offline e revisada.

Não existe interface genérica do Django admin: modelos registrados com `@admin.register` não são roteados, evitando contornar serviços de lifecycle, CAS e auditoria. Telas globais de uma app são declaradas em `administration.screens` com `scope: "platform"` e `kind: "custom"` (handler registrado por `register_admin_handler`), montadas em `/platform/apps/<app_label>/` sob o guarda da plataforma (com reautenticação quando `sensitive`). Telas de negócio organizacional vivem no viewer em `/o/<org>/catalog/` e `/o/<org>/tables/<table_key>/`.

O [contrato de administração e interfaces](application-admin-ui-plan.md) detalha grupos automáticos, telas de parâmetros, layouts e adaptadores de serviço. Manifestos novos usam `manifest_version=2` com compatibilidade a partir do piso de `runtime.service_api` até `<1.0.0`; artefatos assinados v1 e v2 permanecem legíveis sem normalização nem alteração de bytes/digest. Embora `col_span` e `rows` (inclusive `null`) exijam `FIELD_DIMENSIONS_CORE_MINIMUM`, tanto ele quanto o piso v2 são `0.0.0` em todo núcleo publicado. `administration.screens` aceita apenas `configuration` ou `custom` de parâmetros, nunca `model_list` ou metadados de tabela; `TableSpec` — nunca o layout — define leitura, escrita e sensibilidade. Campos em `configuration_fields` do manifesto v2 podem declarar `"managed_by": "platform"` (padrão `"organization"`): a organização apenas lê e o administrador da plataforma grava por organização, exigindo o piso `PLATFORM_MANAGED_CONFIG_CORE_MINIMUM` (0.6.0), verificado no build e no `package doctor` ([configurações geridas pela plataforma](managed-settings.md#campos-de-app-geridos-pela-plataforma)).

O núcleo distribui `django-crispy-forms==2.7` e o pack SSR `skeleton_crud` no wheel. Customize a apresentação exclusivamente via `runtime.model_ui` do manifesto canônico, sem arquivos paralelos nem editor no admin. O exemplo Notes demonstra abas Identificação e Conteúdo, rótulos Código, Título e Texto e spans `code:4`, `title:8` e `body:12` com `rows:8` (veja o [exemplo JSON completo](application-admin-ui-plan.md#3-manifesto-canônico-v2)). `col_span` aceita inteiro exato de 1 a 12 e `rows` inteiro exato de 1 a 40 (omissão ou `null` mantém padrões; bool, string e float são recusados). `rows` exige textarea editável efetivo (ou override `textarea`/`json` compatível), nunca campos somente leitura, inputs, selects ou relações, sem alterar validators. Seções com `columns` 1 ou 2 definem largura padrão 12 ou 6, campos largos usam 12 e spans explícitos prevalecem. A grade preserva ordem de DOM e foco e colapsa para linha inteira até 600px.

Modelos elegíveis sem `model_ui` recebem defaults automáticos sem exigir `Form` nem templates próprios. O renderer mantém CSRF, `media`, validação, CAS e persistência em todos os formulários, incluindo perfil, acesso e configuração. O viewer genérico atende apenas `SecuredRecord` exposto via `TableSpec.expose_api=True`; `SystemRecord` e `OrganizationRecord` globais ou técnicos seguem com serviços e superfícies dedicados e não ganham CRUD por declarar layout.

### Dependências entre apps

`dependencies` mapeia cada app ID exigido para uma faixa de versão (como `{"com.example.customers": ">=1.2,<2.0"}`), e `conflicts` usa o mesmo formato para versões incompatíveis. Pré-releases dentro da faixa a satisfazem (`1.2.0-rc.1` satisfaz `>=1.0.0,<2.0.0`) em qualquer versão suportada de `packaging`; pela regra PEP 440, pré-releases do próprio limite ficam de fora (`1.0.0rc1` não satisfaz `<1.0.0` nem `>=1.0.0`). O núcleo não entra em `dependencies` — sua faixa vive em `compatibility.skeleton`, e declarar `django-skeleton` como dependência é recusado. Não existem dependências opcionais: toda integração que exige o código de outra app deve declará-la.

O mesmo resolvedor (`skeleton.system.dependencies.resolve_dependencies`) executa em quatro pontos e relata todos os problemas de uma vez com códigos estáveis:

| Momento | O que acontece com um problema |
|---|---|
| `package doctor --inventory` | Listado antes de empacotar, junto com sugestões do catálogo, se houver. |
| Prévia no agente | Recusa o plano antes de copiar o projeto ou extrair pacotes. |
| Carga do inventário no startup | Dependência faltante, fora da faixa, ciclo ou conflito falha fechado. |
| `manage.py check` (E011) | Um erro por problema, incluindo incompatibilidade de plataforma. |

| Código | Significado |
|---|---|
| `missing_dependency` | A app exigida não está instalada nem incluída no plano. |
| `incompatible_dependency_version` | A app exigida existe, mas a versão resultante está fora da faixa. |
| `dependency_cycle` | As dependências formam um ciclo; o detalhe traz o caminho. |
| `conflicting_release` | Uma app declarada em `conflicts` está presente numa versão da faixa. |
| `incompatible_platform` | `compatibility` não aceita o core, o Python ou o Django do alvo. |
| `identity_collision`, `namespace_collision` | Duas apps com o mesmo ID, label ou pacote Python. |
| `wheel_pin_conflict` | Duas apps fixam versões ou builds diferentes da mesma distribuição Python. |

A ordem resolvida define o inventário e `INSTALLED_APPS`: toda app é carregada depois de suas dependências, garantindo que seu `ready()` já encontre as dependências registradas.

Acima da ativação organizacional está a **disponibilidade** controlada pela plataforma (`skeleton/system/availability.py`). `set_app_availability` (CAS, revisão virtual `0`), `grant_app` e `revoke_app` exigem administrador da plataforma e gravam apenas linhas globais:

- `grant_app` recusa conceder uma app cuja dependência esteja indisponível para a organização (`dependency_unavailable`);
- `revoke_app` recusa revogar enquanto existir dependente concedida (`dependent_granted`);
- `application_state` expõe `available`, `require_application_enabled` exige disponibilidade da app e de suas dependências (respondendo 404 via `ApplicationUnavailable` na ausência) e `configure_application` recusa ativar app indisponível (`app_unavailable`), permitindo apenas desativar;
- telas de parâmetro `custom` de apps indisponíveis para a organização somem da lista e respondem 404 direto (`ApplicationUnavailable`), mantendo acessível apenas a tela de configuração do núcleo para desativação;
- `configure_application` adquire lock via `organization_lock(user, capability="apps.manage")`, revalidando organização e capacidade após o lock em qualquer superfície (página, API ou serviço);
- sem linha de disponibilidade, a app é **indisponível** para todas as organizações; concedê-la a uma organização cria `selected` (nunca `all`), e reconceder restaura a concessão e a escolha prévia de ativação da organização.

### Default fechado e upgrade da ativação (#22)

Instalar uma app não a disponibiliza nem a ativa: toda app recém-instalada nasce em modo `selected` sem concessões (`synchronize_inventory` cria o registro quando o inventário traz um novo componente), ficando inacessível até a plataforma conceder e a organização ativar. O campo `runtime.default_enabled` continua aceito no schema v2 para preservar pacotes assinados, mas é ignorado; `AppManifest.default_enabled` serve exclusivamente ao comando de upgrade abaixo.

Organizações que utilizavam apps ativas por padrão preservam seu estado executando `manage.py upgrade_app_activation` (use `--dry-run` para simular sem gravar). Como lê o registro vivo de manifestos e o inventário, o procedimento é um comando retomável via `AppAvailability.upgrade_state` (não uma data migration):

1. em transação global, cria `mode=all` (`pending`) para cada app instalada antes do upgrade sem linha de disponibilidade e marca linhas anteriores como `pending`;
2. em uma transação por organização sob `system_context(reason="upgrade:app-activation")`, cria `AppInstallation(enabled=True)` onde o antigo default era verdadeiro e a organização nunca havia configurado a app;
3. marca o estado como `done`.

Executar novamente após interrupção conclui sem duplicar registros, e organizações criadas depois do upgrade nunca recebem apps pré-ativadas. O conjunto "instalada antes" vem de `ComponentInstallation` quando há inventário do agente (ou de todas as apps carregadas na primeira execução, em deploy manual ou desenvolvimento). Não altere `default_enabled` no mesmo release em que rodar o upgrade. Como o exemplo Notes declara `false`, bancos antigos de desenvolvimento não o reativam automaticamente — use `set_app_availability`.

Em desenvolvimento, `manage.py set_app_availability <app_label> --mode all|selected [--grant SLUG] [--enable SLUG]` libera e ativa a app nas organizações informadas (uma transação por organização). `check_skeleton_schema` avisa quando uma app carregada não possui linha de disponibilidade. Em testes, `skeleton.testing.applications` oferece `open_app`, `activate_app` e `apps_open_by_default`.

Para registrar tipos temporariamente em testes, `skeleton.testing.registries` isola os registries do núcleo e restaura o estado original ao sair (mesmo em falha): `isolated_job_types()` (tipos de job e periódicos), `isolated_event_types()` (copia os do núcleo; consumidores vazios ou preservados com `keep_consumers=True`), `isolated_notification_types()`, `isolated_token_scopes()` e `table_spec_override(table_key, **mudanças)`. Utilize-os em uma fixture com `yield` dentro do bloco `with`, sem misturar com `monkeypatch` no mesmo registry (a ordem de teardown corromperia o estado dos testes seguintes).

Tabelas `SecuredRecord` podem habilitar anexos via `TableSpec(attachments=AttachmentPolicy(max_bytes=..., types=(...)))` — com autorização derivada do registro alvo, contagem na cota da organização e bloqueio de download até passar pelo antivírus quando configurado ([arquivos e anexos](files.md#anexos)) — e comentários via `TableSpec(comments=CommentPolicy())`, permitindo leitura e criação a quem lê o registro, edição apenas ao autor e moderação ao administrador ([comentários](comments.md)).

A ativação organizacional segue o fecho transitivo: habilitar uma app exige todas as suas dependências diretas e transitivas habilitadas; desabilitá-la exige que nenhuma app habilitada dependa dela (a mensagem lista as bloqueadoras).

Sempre que o código utiliza outra app, a dependência no manifesto é obrigatória. Os checks abaixo (que rodam sem consultar o banco) aplicam essas garantias (lista completa em [checks](checks.md)):

| Check | Regra |
|---|---|
| E014 | Uma migration da app depende de migration de outra app registrada que não está em `dependencies`. |
| E015 | Um modelo da app tem FK para modelo de outra app registrada que não está em `dependencies`. |
| E016 | `sensitive_fields` do `TableSpec` difere da especificação congelada em `InstallRecordInfrastructure`; crie uma migration nova com `record_infrastructure --write`. |
| E017 | Um `SystemRecord` declara um campo `organization`; esse nome ativa a semântica tenant em querysets e migrations. Use outro nome, como `granted_organization`. |
| W017 | `migrations.leaves` do manifesto difere das folhas reais do grafo de migrations da app. É aviso, e não erro, porque o manifesto é atualizado depois do `makemigrations`; o `build`, o `doctor` e a prévia do agente recusam a divergência. |
| E018 | A API de serviços declarada em `runtime.service_api` não resolve ou está malformada: módulo ausente ou fora do pacote da app, classe ausente, definida fora do módulo ou que não deriva de `ServiceAPI`, `app_label` diferente da app, nenhuma `@service_method`, método público sem `@service_method`, sem docstring ou com argumento posicional. |
| W018 | O manifesto (histórico) não declara `runtime.service_api`. É aviso para que pacotes assinados antigos continuem carregando; `package build` e `package doctor` já recusam a ausência. |
| E019 | Um módulo da app importa de outra app algo que não seja o `service_api` declarado dela, ou importa esse `service_api` sem declarar a app em `dependencies`. Análise estática (AST) de todos os `.py` da app, inclusive imports relativos e `from pacote import modulo`. |
| W019 | A app importa um módulo do núcleo fora das [superfícies públicas](service-api.md#superfícies-públicas-do-núcleo-para-apps). É aviso nesta versão e vira erro numa próxima. |
| E020 | Um modelo `OperationalRecord` tem `expose_api=True` ou `write_fields`; tabelas operacionais são estado de pipeline e nunca vão para o gateway. |
| E029 | Um [fluxo de processo](flows.md#validação-estrutural) declarado com `register_flow` é inválido: chave duplicada, etapa inalcançável, beco sem saída, bifurcação sem junção, gateway sem transição padrão, aprovação sem as transições de aprovado e rejeitado, condição com operador ou campo desconhecido, handler, condição ou regra de atribuição não registrados pela própria app, campo de formulário divergente ou registro de negócio que não é um `SecuredRecord` exposto da app. |
| E026 | Um `EncryptedTextField` não está em `TableSpec.sensitive_fields`, ou um keyring (`MFA_ENCRYPTION_KEYS`, `IDENTITY_PROVIDER_ENCRYPTION_KEYS`) tem chave Fernet inválida. |
| E027 | Check de banco (`check --database`): há segredos cifrados gravados, mas o keyring deles está vazio. |

Declarar uma dependência no manifesto não libera o import livre dos módulos da outra app: o único ponto de consumo permitido é o `service_api` dela.

### API de serviços da app

Toda app expõe suas operações consumíveis por outras apps em `service_api.py`, por meio de classes de serviço em processo. O contrato normativo completo está em [API de serviços entre apps](service-api.md), e o roteiro de implementação no skill `service-api`. Em resumo:

- métodos públicos são keyword-only, recebem `actor` e devolvem DTOs imutáveis ou `Page` de DTOs, nunca modelos nem querysets;
- a autorização ocorre dentro do serviço (`authorized_queryset`, `require_record_action`, capacidades), exigindo a app provedora ativa na organização;
- o serviço participa da transação do chamador e delega efeitos externos a jobs, eventos ou `on_commit`;
- outras apps importam exclusivamente `<app>.service_api` e declaram a dependência no manifesto;
- views e endpoints da própria app também chamam `service_api`, mantendo um único caminho de regra de negócio.

A declaração no manifesto é obrigatória para novos releases:

```json
"runtime": {"service_api": {"module": "applications.tasks.service_api", "classes": ["TaskService"]}}
```

O módulo deve estar dentro do pacote da app e `classes` deve conter nomes simples. Declarar o campo exige `compatibility.skeleton` com piso mínimo `SERVICE_API_CORE_MINIMUM` (suportado por todo núcleo publicado; `package build` grava o piso no artefato). Os checks `E018`/`E019` e o `package doctor` validam o contrato, enquanto o [kit de testes](service-api.md#kit-de-testes-de-contrato) comprova autorização, isolamento e CAS. O exemplo Notes (`examples/notes/service_api.py`) e o gerador já seguem esse padrão. Imports dinâmicos (`importlib`, `__import__`) escapam da análise estática e continuam sendo verificados em revisão de código.

### Apps first-party do núcleo (`skeleton/contrib`)

Algumas apps genéricas — como a agenda e o livro contábil — são distribuídas **dentro do wheel do núcleo**, em `skeleton/contrib/<pacote>`, evitando pacotes `.skapp` separados e matrizes de compatibilidade com o core. Agenda e livro contábil são **bibliotecas** (`kind: "bundled_library"`): capacidades consumidas por outras apps via API de serviços, nunca escolhidas isoladamente pela organização. O contrato:

| Item | Regra |
|---|---|
| Manifest | `kind: "bundled_app"` ou `kind: "bundled_library"`, manifest v2, `app_label` com o prefixo reservado `skeleton_`, `app_config` em `skeleton.contrib.<pacote>.apps.<Config>` e módulos de runtime sob `skeleton.contrib.<pacote>.`. Sem `files` nem `python_dependencies` próprios. Bibliotecas também não possuem `configuration_fields` nem `default_enabled: true`, e só podem depender de outras bibliotecas (`library_dependency` no E011) |
| Local e versão | O manifesto só é aceito dentro do pacote `skeleton` instalado, e sua versão deve ser **exatamente igual** à do core (divergência interrompe o startup com `ImproperlyConfigured`) |
| Prefixo reservado | Apps de terceiros com label `skeleton_*` ou `AppConfig` em `skeleton.contrib` são recusadas; `package build` recusa manifestos `bundled_app` ou `bundled_library` |
| Instalação | Uma biblioteca entra automaticamente em `INSTALLED_APPS` sempre que uma app instalada depende dela (seja do inventário do agente, `SKELETON_DEV_APPS` ou `PROJECT_APPS`), trazendo suas bibliotecas transitivas. O opt-in explícito continua via `SKELETON_BUNDLED_APPS=calendar,ledger` (nomes do mapa fixo `skeleton/conf/bundled.py`; nomes desconhecidos levantam `ImproperlyConfigured`). Variáveis de ambiente nunca informam import paths |
| Rotas do portal | Qualquer app em manifesto v2 pode declarar `runtime.portal_urls` e `runtime.portal_api_urls`, montando `/portal/<org>/apps/<label>/` e `/api/v1/portal/<org>/apps/<label>/` para pessoas externas com piso `PORTAL_CORE_MINIMUM` ([portal](portal.md#superfície-533)) |
| Rotas do usuário | Exclusivo de apps first-party: `runtime.user_urls` monta rotas globais da pessoa em `/me/apps/<label>/` com login obrigatório, sem cache e namespace `app_name` próprio (distinto do `app_name` organizacional). Sem organização na URL, cada view percorre as organizações do ator uma transação por vez (`for_each_organization`), ignorando onde a app não está ativa |
| Ciclo de vida | Uma `bundled_app` segue o fluxo padrão ([#67](https://github.com/alaorneto/skeleton/issues/67)): nasce indisponível (`selected` sem concessões) e desativada. Já uma **biblioteca** não possui disponibilidade nem ativação próprias: fica efetiva em uma organização enquanto houver ao menos uma app efetiva dependente dela (direta ou transitivamente) e fecha quando a última sai, preservando os dados. Não aparece no painel de apps da organização, pacotes, cobrança, limites ou travas; na plataforma aparece somente para leitura listando as apps consumidoras, e tentativas de configurar disponibilidade, concessão, limites, ativação ou parâmetros retornam `code="library"`. Suas rotas, navegação e quadros valem enquanto estiver efetiva. Retirada com preservação de dados e reinstalação estão em [deployment](deployment.md#apps-first-party) |

O núcleo nunca importa `skeleton.contrib` (garantido em CI por `tests/unit/test_bundled_apps.py`), e a suíte do núcleo roda sem nenhuma app first-party instalada.

**Depender de uma app first-party.** Declare a first-party em `dependencies` pelo `app_id` com uma faixa compatível com a versão do core (pois ela acompanha sempre a versão do core): `"dependencies": {"org.skeleton.calendar": ">=0.10.0,<0.11.0"}`. Bibliotecas sempre satisfazem a dependência por serem instaladas junto com a app; já `bundled_app` exige presença em `SKELETON_BUNDLED_APPS`. O mapa `nome → app_id` é fixo em `skeleton/conf/bundled.py` (`BUNDLED_APP_IDS`). A organização ativa apenas a sua app — a biblioteca acompanha automaticamente. Startup (`load_deployment_inventory`), plano do agente e `package doctor --bundled-app` produzem os mesmos diagnósticos (`missing_dependency` se ausente; `incompatible_dependency_version` se fora da faixa). Nenhuma app de terceiro pode reutilizar o `app_id` de uma first-party instalada (`identity_collision`), e apps first-party entram em `INSTALLED_APPS` antes das apps gerenciadas.

**Calendário gerenciado por uma app.** Uma app que cria calendários na agenda mantendo o controle de acesso por regras próprias (por exemplo, espelhando equipe de atendimento) informa seu label em `CalendarService.create_shared_calendar(..., source_app="<label>", source_path="apps/<label>/...")`. O calendário torna-se **gerenciado**: telas da agenda exibem "gerenciado por <app>" e "abrir na origem", impedindo renomeação, edição de acesso ou exclusão pela agenda ou pelo gateway — apenas a app dona o altera via serviço. Calendários preexistentes podem ser assumidos de forma idempotente com `claim_calendar(..., source_app="<label>", expected_revision=...)` sem migration de dados na app (calendários de outra app retornam `Conflict("managed_calendar")`). Para gerar caminhos relativos à organização até o calendário ou seu link de assinatura, use `calendar_links` ([API de serviços da agenda](../skeleton/contrib/calendar/docs/service-api.md)).

#### Depender do livro contábil (`org.skeleton.ledger`)

O livro contábil é a biblioteca first-party de partidas dobradas (livros, plano de contas, lançamentos selados, títulos a pagar e receber, baixas, recorrências e relatórios). Apps que movimentam valores financeiros lançam nele pelos seus próprios registros, sem tocar diretamente nas tabelas do livro. A referência completa está em `skeleton/contrib/ledger/docs/service-api.md`.

1. **Manifesto.** Declare a dependência — o livro contábil é instalado automaticamente com a app, inclusive no CI de repositório próprio com `SKELETON_DEV_APPS`:

   ```json
   "dependencies": {"org.skeleton.ledger": ">=0.10.0,<0.11.0"}
   ```

2. **Registro em `ready()`.** Chaves de conta e regras de lançamento são declaradas em código com o prefixo `<app_label>.`; cada organização mapeia as chaves às contas de seu plano. A regra informa a tabela de origem e a ação exigida no registro (validada por `E041`), aceitando tabelas expostas ou [tabelas derivadas](access-control.md#tabelas-derivadas) (como linhas de um documento, autorizadas pela âncora com a ação mapeada):

   ```python
   from skeleton.contrib.ledger.service_api import (
       AccountKey,
       Key,
       Param,
       PostingRule,
       RuleLine,
       register_account_key,
       register_posting_rule,
   )

   register_account_key(AccountKey(code="acme_services.revenue", label="Receita de atendimentos", kind="revenue"))
   register_posting_rule(
       PostingRule(
           code="acme_services.service_paid",
           label="Atendimento pago",
           source_table_key="acme_services.appointment",
           source_action="update",
           lines=(
               RuleLine(side="debit", account=Param("payment_account"), amount="total"),
               RuleLine(side="credit", account=Key("acme_services.revenue"), amount="total"),
           ),
       )
   )
   ```

3. **Chamadas.** O ator vem do contexto; quando há origem, a autorização decorre do registro de origem, não do livro. Valores monetários são sempre inteiros na menor unidade da moeda (`skeleton.common.money`):

   ```python
   from skeleton.contrib.ledger.service_api import LedgerService

   ledger = LedgerService()
   title = ledger.create_title(
       actor=actor,
       direction="receivable",
       amount=15000,
       due_date=day,
       description="Atendimento 42",
       category_key="acme_services.revenue",
       idempotency_key=f"appointment:{appointment.pk}",
       source_table="acme_services.appointment",
       source_id=appointment.pk,
       counterparty_name=client_name,
   )
   ledger.settle_title(
       actor=actor,
       title_id=title.sysid,
       amount=15000,
       date=day,
       payment_account_id=cash_account,
       method="pix",
       expected_revision=title.revision,
       idempotency_key=f"appointment:{appointment.pk}:paid",
   )
   # Ou um lançamento direto pela regra:
   ledger.post(
       actor=actor,
       rule="acme_services.service_paid",
       amounts={"total": 15000},
       date=day,
       description="Atendimento 42",
       source_id=appointment.pk,
       accounts={"payment_account": cash_account},
       idempotency_key=f"appointment:{appointment.pk}:posted",
   )
   ```

   O livro participa da mesma transação do chamador: se a gravação da app falhar, o lançamento é desfeito junto. Correções são sempre feitas via `reverse`, nunca editando lançamentos selados.

4. **Testes mínimos.** Com `skeleton.contrib.ledger.testing` (`enable_ledger`, `prepare_book`, `account_id`), em PostgreSQL sob a role runtime: comprove que o ator autorizado na origem lança e consulta em `entries_for_source`; que o ator sem permissão é recusado; que repetir `idempotency_key` não duplica; que a organização B nada enxerga de A; e que o livro desativado gera `Unavailable`. Veja o cenário de referência em `tests/contrib/ledger/test_scenario.py`.

### Carregar uma app em desenvolvimento (`SKELETON_DEV_APPS`)

Uma app mantida em repositório Git próprio (veja [Desenvolver uma app em repositório próprio](#desenvolver-uma-app-em-repositório-próprio)) pode ser carregada em desenvolvimento sem editar arquivos versionados: basta listar os diretórios de checkout separados por vírgula em `SKELETON_DEV_APPS`:

```sh
export SKELETON_DEV_APPS=/home/dev/src/acme_clientes
python manage.py check
python manage.py makemigrations acme_clientes   # grava em /home/dev/src/acme_clientes/migrations/
```

| Regra | Comportamento |
|---|---|
| Perfis | Aceita apenas nos perfis de desenvolvimento e teste. Perfis de produção, migração, build e setup interrompem o startup com `ImproperlyConfigured` se não estiver vazia: em produção, apps entram apenas por pacote assinado |
| Checkout | O diretório é o próprio pacote da app, com `manifest.json` na raiz, e seu nome deve coincidir com `package_name` no manifesto (clone o repositório em um diretório com esse nome) |
| Manifesto | Validado como qualquer app (`kind: "app"`); labels `skeleton_*`/`sk_*` e `AppConfig` em `skeleton.` são recusados |
| Import | Importada como `applications.<pacote>` (mesmo namespace de um release) por um *finder* dedicado a esse módulo, sem alterar `sys.path` — diretórios vizinhos não ficam importáveis e o núcleo nunca é sombreado |
| Colisão | Qualquer pacote `applications.<pacote>` já importável de outro local (como cópia em `applications/`) ou duplicado entre checkouts é recusado |

Todas as recusas levantam `ImproperlyConfigured` citando o caminho do checkout. Apps de `SKELETON_DEV_APPS` entram em `INSTALLED_APPS` depois das first-party e antes de `PROJECT_APPS`. Para rodar a suíte de uma app sem o pacote `config` de um projeto hospedeiro, utilize `skeleton.testing.host` ([testing](testing.md#projeto-hospedeiro-para-apps-em-repositório-próprio)).

### Documentação obrigatória por app

`documentation_root` aponta para um diretório versionado contendo `spec.md` (`## Contract`, `## Acceptance`) e `adr.md` (`## Context`, `## Decision`, `## Consequences`). Documente persistência, autorização, ativação e configuração, endpoints, sensibilidade, concorrência e critérios de aceite observáveis na spec, e decisões e tradeoffs no ADR. Os checks recusam documentação incompleta (veja `examples/notes/docs/`).

O mesmo diretório deve incluir `service-api.md` com a referência completa da API de serviços da app (serviços, métodos com Autorização, Erros e Efeitos, DTOs, eventos e histórico de versões; modelo no skill `service-api`). O check `E012` exige o arquivo com as seções `## Serviços`, `## DTOs` e `## Histórico de versões` em toda app com `runtime.service_api`, exigência repetida por `package build` e `package doctor`. A tela `/platform/apps/<app_label>/documentation/` renderiza o documento quando presente.

### Diagnosticar antes de empacotar

`package doctor` inspeciona o código-fonte de uma app e aponta todos os problemas que o build, a instalação ou os checks encontrariam — sem importar código, gerar artefatos nem gravar arquivos. Funciona com ou sem Django:

```sh
.venv/bin/python -m skeleton.system.packages doctor applications/tasks
.venv/bin/python manage.py package doctor applications/tasks \
  --inventory /private/target/inventory.json \
  --catalog /private/index.json --trust-store /private/trusted-publishers.json
```

Sem opções extras, valida manifesto, arquivos proibidos pelo build, `apps.py`, seções obrigatórias de `spec.md` e `adr.md` e `migrations.leaves` contra os arquivos em `migrations/`. Com `--inventory`, resolve dependências contra o ambiente alvo; com `--catalog`, sugere releases compatíveis para dependências faltantes. A saída é JSON (`ok` e lista `problems` com `code` e `message`), retornando código de saída 1 em caso de erro.

O cálculo das folhas lê `dependencies` de cada migration como literal. Se alguma migration calcular dependências dinamicamente em código, o `doctor` reporta `migration_leaves_unverified` e o `build` não consegue conferir automaticamente (exigindo conferência manual); quando determináveis e divergentes do manifesto, o `build` recusa gerar o pacote.

### Publicar apps e releases assinados

Publicar gera um artefato `.skapp` verificável — etapa distinta de instalá-lo ou aprová-lo para produção. O fluxo abrange gerar chaves do publicador, criar e validar o manifesto, construir o pacote e conferir integridade e procedência. Cada operador mantém seu próprio trust store de publicadores confiáveis: o SHA-256 prova que os bytes não foram alterados, enquanto a assinatura comprova que uma chave confiável aprovou aquele manifesto.

Os comandos funcionam tanto via `manage.py package` quanto de forma independente (sem Django configurado) por `.venv/bin/python -m skeleton.system.packages`. O agente de deployment aplica as mesmas funções puras de validação e nunca importa código de um upload antes de verificar assinatura, inventário e hashes. Assinatura prova procedência, **não** cria sandbox para Python ou migrations: aprove somente publicadores autorizados a rodar código privilegiado.

Defina `name = __package__` no `AppConfig` para que a app funcione tanto no checkout local quanto no namespace gerenciado `applications.<package_name>`. No manifesto JSON, `app_config` e módulos de runtime usam sempre o caminho gerenciado (`applications.tasks.apps.TasksConfig`, `applications.tasks.urls`); o loader adapta ao `AppConfig` local e valida a identidade. Coloque templates em `templates/<app_label>/` e arquivos estáticos em `static/<app_label>/`. Sob a política de conteúdo de mesma origem, evite `<script>` inline, atributos `style=` e handlers `on*=` ([Content-Security-Policy](deployment.md#content-security-policy)). Projetos que sobrescreverem `FILE_UPLOAD_HANDLERS` devem manter `skeleton.common.uploads.BoundedUploadHandler` em primeiro lugar para respeitar `SKELETON_UPLOAD_MAX_BYTES`. Views decoradas com `tenant_page` herdam `Cache-Control: no-store, private` (páginas que precisem de cache no navegador não devem usar esse decorador). Apps nunca gravam em `config`, settings ou arquivos de outra app e preservam todas as migrations históricas.

```sh
# Chaves ficam FORA da app e do Git; a privada é criada com modo 0600.
.venv/bin/python -m skeleton.system.packages keygen \
  --private-key /private/publisher.pem --public-key /private/publisher.pub.pem
.venv/bin/python manage.py package init \
  --output applications/tasks/manifest.json --app-id com.example.tasks \
  --app-label project_tasks --package-name tasks --publisher example-publisher \
  --app-config-class TasksConfig --version 0.1.0
# Edite o JSON canônico: nome/descrição, compatibilidade, runtime e folhas de migrations.
.venv/bin/python manage.py package validate-manifest applications/tasks/manifest.json
.venv/bin/python manage.py package build \
  --app-root applications/tasks --manifest applications/tasks/manifest.json \
  --output /private/artifacts/tasks-0.1.0.skapp --private-key /private/publisher.pem
.venv/bin/python manage.py package validate /private/artifacts/tasks-0.1.0.skapp \
  --trust-store /private/trusted-publishers.json
.venv/bin/python manage.py package inspect /private/artifacts/tasks-0.1.0.skapp \
  --trust-store /private/trusted-publishers.json
.venv/bin/python manage.py package versions --manifest applications/tasks/manifest.json
```

**Empacotar a partir de uma revisão Git.** Em apps mantidas em repositório próprio, passar `--git-ref <revisão>` faz `build` e `doctor` operarem sobre a árvore commitada exportada via `git archive`, em vez do diretório de trabalho:

```sh
.venv/bin/python -m skeleton.system.packages doctor ../acme_clientes --git-ref HEAD
.venv/bin/python -m skeleton.system.packages build --app-root ../acme_clientes --git-ref v1.2.0   --output /private/artifacts/acme_clientes-1.2.0.skapp --private-key /private/publisher.pem
```

- **Somente o que está commitado entra no `.skapp`:** alterações locais não commitadas ou arquivos não rastreados são ignorados.
- **`export-ignore` define exclusões:** marque arquivos auxiliares do repositório (`.gitattributes`, `.gitignore`, `.github/`, `pyproject.toml`, `README.md`) com `export-ignore` em `.gitattributes`. Dotfiles commitados sem essa marca são recusados como `private_file` (detectado por `doctor --git-ref HEAD` no CI). Sem `--git-ref`, todo dotfile continua sendo `private_file`.
- O manifesto é lido da árvore exportada (`manifest.json` na raiz ou `--manifest` dentro de `--app-root`). A saída inclui o hash completo `commit`, sem gravá-lo no manifesto (esquema fechado).
- `--app-root` também aceita subdiretórios do repositório (como `applications/tasks`), exportando apenas aquela subárvore.
- A revisão aceita apenas letras, dígitos e `. _ / @ -`, sem hífen inicial; `git` é executado sem shell, e a extração via `tarfile` com filtro `data` em diretório temporário privado bloqueia symlinks, hard links, arquivos especiais e travessia de caminho. Ausência de `git`, diretório fora de repositório e revisão inexistente produzem erros de pacote sem traceback.

Apps são publicadas como pacotes de componente (`kind: "app"`) contendo seus wheels em `python_dependencies`. Pacotes de release (`kind: "release"`, veja [Pacote de release](deployment.md#pacote-de-release)) são exclusivos do core, reunindo o pacote do core, lock, wheels por alvo e o wheel do agente.

O **trust store** é um JSON mantido pelo operador mapeando `publisher` para sua chave pública, como `{"example-publisher": "/private/publisher.pub.pem"}`. Valores aceitam PEM literal, caminho de arquivo PEM (relativo ao arquivo de configuração no CLI) ou `{"public_key": "/private/publisher.pub.pem", "publisher": "example-publisher"}`; a API também aceita instâncias de `Ed25519PublicKey`. Chaves privadas ficam restritas a quem publica, e chaves públicas embarcadas no pacote nunca são tratadas como âncora de confiança. Revogar ou rotacionar chaves exige atualizar explicitamente esse arquivo privado. `inspect` sem trust store sempre reporta `"trusted": false`, mesmo com hashes íntegros.

`package build` gera o inventário completo (excluindo apenas o manifesto e caches/artefatos de build conhecidos) e bloqueia symlinks, segredos, chaves privadas, arquivo de saída dentro da app, namespaces reservados, documentação incompleta e dependências fora do inventário. O `.skapp` (ZIP) contém exatamente `manifest.json`, `manifest.sig`, `app/` e wheels opcionais, com assinatura Ed25519 sobre os bytes canônicos do manifesto e SHA-256 e tamanhos sobre todos os payloads. A validação recusa entradas extras ou duplicadas, colisões normalizadas, path traversal, links, dispositivos, conflitos arquivo/diretório e excessos de limite (512 MiB comprimido, 1 GiB expandido, 256 MiB por arquivo, 4 096 membros e razão de expansão 200:1).

Dependências Python são declaradas em `python_dependencies` como `{"name": "distribution", "version": "1.2.3", "wheel": "wheels/distribution-1.2.3-py3-none-any.whl", "sha256": "<SHA256>", "marker": "python_version >= '3.12'"}` (`marker` opcional) e anexadas com `build --wheel /private/wheelhouse/distribution-1.2.3-py3-none-any.whl`. O build recalcula o SHA-256, valida `METADATA` e namespace de cada wheel e recusa sdists, diretórios `.data` e arquivos que invadam `core`, `config` ou `applications`. Na instalação, o ambiente novo instala os wheels pelo lock aprovado — nunca dentro do diretório da app. Incluir um wheel (`.whl`) fixa os bytes da distribuição para instalação offline, sem dispensar a revisão de segurança do código de terceiros.

Versões de componentes seguem `major.minor.patch`, enquanto distribuições Python e markers seguem PEP 440 (incluindo versões de duas partes como `8.2` ou `python_version="3.14"`). O wheelhouse completo do ambiente possui teto agregado separado de 65 536 membros e 1 GiB expandido. Apps `project-local` permanecem em `PROJECT_APPS`, precisam bater exatamente com manifesto e caminho do inventário no startup (falha fechada) e nunca são sobrescritas por pacotes de mesmo nome.

Extração offline exige um diretório destino novo e vazio: `package extract ARTIFACT --trust-store TRUST --destination /staging/applications/tasks --wheels-destination /staging/app-wheels`. Não faz merge nem sobrescrita e revalida o artefato antes da cópia. `extract_package(verified, destination, wheels_destination=...)` devolve os caminhos criados e grava o contrato canônico e a assinatura como metadados do instalador. Artefatos do core contêm apenas wheels e usam `destination=None` (omita `--destination` no CLI); o wheel do core só pode conter `skeleton/` e seu `.dist-info`, nunca sobrescrevendo arquivos do projeto derivado.

```sh
# Reassinar app/core existente: inspeção completa antes da nova assinatura,
# output novo obrigatório; aprovação da procedência é decisão do operador.
.venv/bin/python manage.py package sign /private/artifacts/tasks-0.1.0.skapp \
  --output /private/artifacts/tasks-approved.skapp --private-key /private/publisher.pem
# Publicação estática; geração monotônica e expiração são obrigatórias.
.venv/bin/python manage.py package index /private/artifacts/tasks-approved.skapp \
  --output /private/index.json --private-key /private/publisher.pem \
  --trust-store /private/trusted-publishers.json --publisher example-publisher \
  --generation 1 --expires-at 2027-01-01T00:00:00Z --channel stable \
  --artifact-base https://releases.example.test/apps/
.venv/bin/python manage.py package validate-index /private/index.json \
  --trust-store /private/trusted-publishers.json --minimum-generation 1 --channel stable
```

**Índice acumulado.** `package index` aceita também pacotes de release (gerando a entrada `kind: "core"` com digest e assinatura do release e compatibilidade e migrations do core embutido). A opção `--previous <índice anterior>` acumula releases já publicados: preserva entradas anteriores, acrescenta as novas e substitui entradas com a mesma chave (`component_id`, versão e canal). O índice anterior deve ter assinatura válida do mesmo `--publisher` e geração estritamente menor que `--generation` (pode estar expirado durante a publicação via `validate_catalog(..., allow_expired=True)`, mas o agente continua recusando índices vencidos). Passar `--previous` sem novos artefatos apenas renova geração e validade. Com `--artifact-base v0.6.0`, o campo `artifact` de cada entrada vira `v0.6.0/<arquivo>`.

```sh
.venv/bin/python manage.py package index /private/artifacts/skeleton-release-0.6.1.skapp \
  --previous /private/index/skeleton-index.json --artifact-base v0.6.1 \
  --output /private/index/skeleton-index-new.json --private-key /private/publisher.pem \
  --trust-store /private/trusted-publishers.json --publisher example-publisher \
  --generation 12 --expires-at 2027-01-31T00:00:00Z
```

O [guia de atualização](updates.md#apps) explica como publicar o índice de uma app em um repositório do GitHub e configurá-lo nas instâncias. Como boa prática, declare `migrations.from_versions` seguindo a política do core (linha atual e linha anterior, como `>=1.4.0,<=1.5.2` para `1.5.2` após a linha `1.4`); em apps, o agente respeita exatamente o `from_versions` do publicador sem forçar parada em cada linha.

O índice tem o formato `{catalog: {...}, signature: "<base64>"}`, assinado sobre o JSON canônico do corpo, com entradas contendo componente, tipo, versão, canal, compatibilidade, caminho HTTPS ou relativo do artefato, SHA-256 do ZIP, assinatura do manifesto, publicador, notas e migrations. O formato 2 (`catalog_version: 2`, gerado por `package index`) acrescenta `dependencies` e `conflicts` para que a prévia sugira dependências; índices no formato 1 continuam válidos e, em ambos, o manifesto dentro do artefato baixado é a autoridade final. Índices expirados, adulterados ou com geração inferior à última confirmada são rejeitados; canais seguem allowlist do operador e o agente consulta apenas fontes previamente autorizadas. `package versions` diferencia distribuições instaladas de checkouts (`origin: source`) e manifestos locais (`project-local`); um JSON isolado nunca comprova release instalado.

Para testar a publicação com o exemplo Notes, rode `package build --app-root examples/notes --manifest examples/notes/manifest.json --output /private/notes.skapp --private-key /private/publisher.pem` e cadastre o publicador `skeleton-examples` no trust store. Ativação e configuração organizacionais continuam independentes da instalação global.

## Registrar um cliente nativo

Um projeto derivado com aplicativo desktop (nativo) realiza dois registros operacionais no servidor, sem alterar código do núcleo:

1. **Login e API.** Configure a chave do provedor OIDC e cadastre o cliente com `python manage.py native_client register --client-id acme-desktop --name "Acme Desktop" --redirect-uri http://127.0.0.1/callback` ([provedor OIDC](authentication-sso.md#provedor-oidc-para-clientes-nativos-603)). O aplicativo descobre o servidor em `GET /api/v1/server/`, autentica pelo navegador do sistema com PKCE e acessa `/api/v1/` com o access token ([API](api.md#access-tokens-oidc-de-clientes-nativos)). Para restringir o cliente a tabelas específicas, passe `--scope`.
2. **Atualizações.** Publique cada versão como GitHub Release com feed Velopack, pacotes e envelope assinado, declarando o cliente em `SKELETON_NATIVE_CLIENTS_CONFIG` ([clientes nativos](updates.md#clientes-nativos-606)).

Lembre-se de declarar `token_methods` nas views das apps consumidas pelo aplicativo nativo; sem isso, tokens recebem 403 `token_not_allowed`.

## Desenvolver uma app em repositório próprio

Apps de negócio podem viver em repositórios Git independentes, fora de qualquer projeto derivado. Nesse modelo, o repositório **é** o pacote Python da app e nunca contém cópia do núcleo — fornecido pelo wheel de uma release ou por um checkout do Skeleton ao lado.

```text
acme_clientes/            <- checkout; o nome do diretório é o nome do pacote
  .gitattributes          <- export-ignore do que não vai no pacote
  .gitignore
  .github/workflows/ci.yml
  pyproject.toml          <- só configuração de pytest/ruff (export-ignore)
  README.md               <- (export-ignore)
  manifest.json
  __init__.py  apps.py  models.py  service_api.py  views.py  urls.py  api_urls.py
  migrations/  templates/  docs/  tests/
```

| Passo | Como |
|---|---|
| Criar | `python manage.py skeleton_startapp acme_clientes --standalone --directory ../ --app-id com.example.clientes --publisher example-publisher` gera a estrutura ([Começar uma app](#começar-uma-app-com-skeleton_startapp)); depois execute `git init`, `git add -A` e `git commit` |
| Carregar no desenvolvimento | Defina `SKELETON_DEV_APPS=/caminho/acme_clientes` em um projeto Skeleton e rode `makemigrations`, `record_infrastructure --write`, `migrate` e `runserver` normalmente ([Carregar uma app em desenvolvimento](#carregar-uma-app-em-desenvolvimento-skeleton_dev_apps)); válido apenas nos perfis de desenvolvimento e teste |
| Testar sem projeto | Use `DJANGO_SETTINGS_MODULE=skeleton.testing.host.settings` (projeto hospedeiro do wheel) com o plugin de pytest configurado no `pyproject.toml` gerado ([testing](testing.md#projeto-hospedeiro-para-apps-em-repositório-próprio)) |
| Banco de teste | `python -m skeleton.testing.database provision --database <nome>_skeleton_test [--workers N]`, a partir do SQL distribuído no wheel ([em paralelo](testing.md#fixture-distribuída-em-paralelo-e-com-template)) |
| Diagnosticar e empacotar | Rode `package doctor --git-ref HEAD` no CI e `package build --git-ref <tag>` na publicação: somente arquivos commitados e sem `export-ignore` entram no `.skapp` ([Publicar](#publicar-apps-e-releases-assinados)) |

**Fixar a versão do núcleo no CI da app.** Cada release do núcleo publica `django_skeleton-<versão>-py3-none-any.whl` como asset, com SHA-256 no corpo da release e em `SHA256SUMS` ([workflow](workflow.md#artefatos-de-cada-release-496)). O `ci.yml` gerado lê `SKELETON_VERSION` e baixa o wheel diretamente de `https://github.com/<repositório do núcleo>/releases/download/v<versão>/` (repositório público, sem token). Atualize `SKELETON_VERSION` em uma mudança dedicada da app acompanhada do ajuste em `compatibility.skeleton`, conferindo `sha256sum` contra `SHA256SUMS`.

**Depender de uma app first-party.** Declare o `app_id` em `dependencies` (por exemplo `"org.skeleton.calendar": ">=0.10.0,<0.11.0"`, veja [Apps first-party](#apps-first-party-do-núcleo-skeletoncontrib)) e habilite a app no CI via `SKELETON_BUNDLED_APPS` no `env` do job (como `SKELETON_BUNDLED_APPS: calendar`); sem ela instalada, o startup recusa a dependência. Importe apenas o `service_api` dela.

**`AGENTS.md` da app.** Instruções específicas da app para agentes ficam em um `AGENTS.md` na raiz do repositório, referenciando o `AGENTS.md` do núcleo na versão de `compatibility.skeleton` sem repetir regras gerais ([contrato](../AGENTS.md#contrato-de-agentes-em-apps-e-projetos-derivados)). Como o gerador não cria esse arquivo, ao adicioná-lo inclua `AGENTS.md export-ignore` em `.gitattributes`.

**Limites.** Nunca versione cópias do núcleo nem arquivos de `config/` no repositório da app; nunca defina `SKELETON_DEV_APPS` em produção (onde apps entram somente via pacote assinado instalado pelo agente); mantenha a chave privada de assinatura fora do repositório (o job de publicação em `ci.yml` vem comentado justamente para o operador definir o armazenamento da chave).

## Exemplo completo existente: Note

O exemplo didático em `examples/notes` (`app_label` `example_notes`) é opt-in e pode permanecer desativado em projetos reais. Nunca coloque módulos de negócio próprios nesse namespace. Para ativá-lo localmente:

```sh
export SKELETON_ENABLE_EXAMPLE_APP=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
```

### Modelo

O modelo utiliza herança abstrata de `SecuredRecord`: metadados sistêmicos, organização e campos de acesso são colunas diretas da tabela `Note`, sem tabela-pai compartilhada:

```python
from django.core.validators import MaxLengthValidator
from django.db import models
from django.db.models import Q
from skeleton.common.models import SecuredRecord


class Note(SecuredRecord):
    code = models.CharField(max_length=64)
    title = models.CharField(max_length=200)
    body = models.TextField(blank=True, validators=[MaxLengthValidator(10000)])

    class Meta(SecuredRecord.Meta):
        constraints = SecuredRecord.Meta.constraints + [
            models.UniqueConstraint(
                fields=("organization", "code"),
                condition=Q(deleted_at__isnull=True),
                name="example_notes_note_live_code",
            ),
        ]
        ordering = ("created_at", "sysid")
```

O campo `body` pode ser vazio (até 10 000 caracteres) e é marcado como sensível: usuários autorizados leem seu conteúdo normalmente, mas a auditoria grava `[REDACTED]` preservando o registro de que houve alteração (isso não cifra a coluna na tabela de negócio). A unicidade em `(organization, code)` vale apenas entre registros vivos: organizações diferentes podem usar o mesmo código, e excluir logicamente uma nota libera o código para outra — nesse caso, tentar restaurar a nota antiga retorna conflito sem sobrescrever a nova.

### Spec allowlist

`NotesConfig.ready` registra a tabela com allowlists explícitas:

```python
from skeleton.system.registry import TableSpec, register
from .models import Note

register(
    TableSpec(
        model=Note,
        description="Anotações de demonstração do contrato de registros",
        label="Anotações",
        scope="organization",
        read_fields=("code", "title", "body", "owner", "visibility", "owning_group"),
        write_fields=("code", "title", "body"),
        sensitive_fields=("body",),
        expose_api=True,
    )
)
```

As allowlists aceitam apenas campos concretos, sem `eval` nem travessia arbitrária de relações. Na leitura, `owner` e `owning_group` retornam UUIDs (nunca perfis ou grupos privados); eles e `visibility` ficam fora de `write_fields`, pois alterar acesso requer o endpoint/serviço de `manage_access`. Campos sistêmicos são sempre somente leitura. `expose_api=True` habilita as rotas do gateway, mas não concede permissão (`TablePermission` continua obrigatória).

- **Numeração automática:** códigos gerados por sequência entram em `sequence_fields=(("code", "<chave da sequência>"),)` e ficam fora de `write_fields`, sendo preenchidos na criação pelo formulário e pela API ([sequências](sequences.md#campos-preenchidos-pelo-gateway-156)).
- **Busca textual:** declare `search_fields=("code", "title")` apenas com campos de texto de `read_fields` não sensíveis (`E031`) e instale `InstallSearchIndex("<app>", "<model>", fields=[...], label="<campo do rótulo do picker>")` na migration. A lista passa a aceitar `?q=` com busca por prefixo na última palavra ([#580](https://github.com/alaorneto/skeleton/issues/580)) e o picker aceita `q` ([#180](https://github.com/alaorneto/skeleton/issues/180), [#181](https://github.com/alaorneto/skeleton/issues/181)). Índices ausentes ou divergentes reprovam `check_skeleton_schema`.

### Busca global

Tabelas com `search_fields` podem participar da busca global ([#585](https://github.com/alaorneto/skeleton/issues/585)), permitindo pesquisar um termo simultaneamente em todas as tabelas marcadas que o ator lê na organização:

```python
from skeleton.system.registry import GlobalSearchPolicy, TableSpec

TableSpec(..., expose_api=True, search_fields=("code", "title"), global_search=GlobalSearchPolicy(order=100))
```

Requisitos verificados por `E044`: ser `SecuredRecord` com `expose_api=True`, declarar `search_fields` e possuir campo de título legível (`list.title_field` em `model_ui` ou, na ausência, `title`, `name` ou `code` em `read_fields`). `order` (0 a 9999, padrão 100) define a ordem dos grupos, com desempate pelo rótulo da tabela. A marcação **não** concede acesso: a busca aplica `apply_search` sobre `authorized_queryset` apenas nas tabelas em que o ator tem leitura (`TablePermission`, escopos do token e app ativa) e retorna somente `sysid` e título agrupados por tabela (pois rankings entre tabelas distintas não são comparáveis). Como cada tabela marcada executa uma consulta por busca, marque apenas tabelas onde faz sentido procurar por nome ou código. O gateway fica em `skeleton.records.search.global_search` ([#581](https://github.com/alaorneto/skeleton/issues/581)).

Com a marcação ativa, a tabela passa a responder em `GET /api/v1/o/<org>/search/` ([busca global na API](api.md#busca-global)), via `SearchService` ([API de serviços](service-api.md#searchservice)) e pela caixa de busca no cabeçalho web em `/o/<org>/search/` ([navegação](ui-design.md#navegação)).

### Migration de segurança

`examples/notes/migrations/0001_initial.py` cria o modelo e instala sua infraestrutura de segurança no mesmo arquivo, dependendo de `("sk_audit", "0002_record_infrastructure")`. Mantenha todos os campos herdados, constraints e managers gerados pelo Django. Exemplo da operação:

```python
from skeleton.common.migration_operations import InstallRecordInfrastructure

InstallRecordInfrastructure(
    "example_notes",
    "note",
    {
        "scope": "organization",
        "sensitive_fields": ["body"],
        "columns": [
            {"name": "sysid", "type": "UUIDField", "sensitive": False},
            {"name": "created_at", "type": "DateTimeField", "sensitive": False},
            {"name": "updated_at", "type": "DateTimeField", "sensitive": False},
            {"name": "created_by", "type": "UUIDField", "sensitive": False},
            {"name": "updated_by", "type": "UUIDField", "sensitive": False},
            {"name": "deleted_by", "type": "UUIDField", "sensitive": False},
            {"name": "deleted_at", "type": "DateTimeField", "sensitive": False},
            {"name": "revision", "type": "PositiveBigIntegerField", "sensitive": False},
            {"name": "organization_id", "type": "UUIDField", "sensitive": False},
            {"name": "owner_id", "type": "UUIDField", "sensitive": False},
            {"name": "visibility", "type": "CharField", "sensitive": False},
            {"name": "owning_group_id", "type": "UUIDField", "sensitive": False},
            {"name": "code", "type": "CharField", "sensitive": False},
            {"name": "title", "type": "CharField", "sensitive": False},
            {"name": "body", "type": "TextField", "sensitive": True},
        ],
    },
)
```

Nunca escreva essa especificação à mão. Após `makemigrations` e revisão do histórico, use o comando dedicado:

```bash
python manage.py record_infrastructure <app_label>          # só lista
python manage.py record_infrastructure <app_label> --write  # cria a migration
```

Sem `--write`, o comando compara cada `TableSpec` da app com a última `InstallRecordInfrastructure` nas migrations em disco (sem tocar no banco) e relata o que está ausente (`missing`) ou divergente (`columns`, `sensitive`, `scope`, `immutable_fields`, `append_only`, `seal`, `seal_parent`, `writes`). Com `--write`, gera uma migration **nova** (`NNNN_record_infrastructure.py`) dependendo da folha atual da app e de `("sk_audit", "0002_record_infrastructure")` apenas com as operações pendentes. A especificação é construída por `build_infrastructure_specification(model, spec)` e congelada no arquivo, garantindo que o DDL durante o `migrate` dependa exclusivamente dela e do modelo histórico.

O comando nunca altera migrations existentes nem cria arquivos vazios. Ele recusa escrever se houver mais de uma folha, se ainda não existir migration para o modelo, se as colunas do modelo histórico divergirem do modelo atual (rode `makemigrations` antes) ou se o alvo for uma app do núcleo. Revise o arquivo gerado — especialmente a lista `sensitive` — antes de commitar.

Use o mesmo fluxo sempre que adicionar ou alterar colunas e relações auditáveis (a operação nova entra depois da alteração, sem substituir `CreateModel`), nunca consultando o catálogo editável nem importando o modelo atual dentro de migrations.

### Tabela global da app e comando do operador

Uma tabela de referência compartilhada entre todas as organizações (como um catálogo global) deriva de `SystemRecord` com `TableSpec(scope="global")`, sem gateway. Ela só aceita escrita em contexto de sistema sem organização, reforçado no modelo e no guard congelado com `"writes": "system"` ([tabelas globais de apps](table-catalog.md#tabelas-globais-de-apps)): views, serviços e consumidores de eventos não conseguem gravá-la. O exemplo `examples/notes` inclui `NoteCategory` (migration `0003_note_category`, gerada com `makemigrations` + `record_infrastructure --write` e consolidada) e o comando de carga correspondente:

```python
# examples/notes/management/commands/load_note_categories.py
from skeleton.tenancy.context import command_context

with command_context(reason="example_notes:load_note_categories"):
    NoteCategory(code=code, title=title).save()
```

```bash
python manage.py load_note_categories ADM=Administrativa OPS=Operação
```

`command_context` é a única superfície pública que abre contexto de sistema para uma app: permitida exclusivamente em `management/commands/` (`E019` bloqueia nos demais módulos) e apenas no topo da execução (dentro de requisição, tenant, job, consumidor ou outro contexto levanta `InvalidContext`). O motivo vai para a auditoria como `source=system:command:<motivo>`. Apps que gravavam tabelas globais por outro caminho devem seguir a nota de migração em [tabelas globais de apps](table-catalog.md#tabelas-globais-de-apps).

## Registros que não mudam depois de um ato

Registros que se tornam definitivos após um ato formal de negócio (como assinar um documento ou efetivar um lançamento) combinam `SealableMixin` e `seal_fields`: após o selo, o próprio banco impede alterações nessas colunas, no selo e, por padrão, na exclusão lógica. As regras completas estão em [modelo de dados](data-model.md#registros-selados-526). Exemplo de implementação em uma app:

```python
# models.py
from django.db import models

from skeleton.common.models import SealableMixin, SecuredRecord


class Document(SealableMixin, SecuredRecord):
    title = models.CharField(max_length=200)
    body = models.TextField()
    delivered = models.BooleanField(default=False)  # Fora de seal_fields: continua editável.
    corrects = models.ForeignKey("self", null=True, blank=True, on_delete=models.PROTECT, related_name="+")
    seal_fields = ("title", "body", "corrects")
    immutable_fields = ("corrects_id",)


class DocumentSection(SecuredRecord):
    document = models.ForeignKey(Document, on_delete=models.PROTECT, related_name="sections")
    text = models.TextField()
    seal_parent = "document"  # Congelada com o documento (#528).
    immutable_fields = ("document_id",)
```

```python
# apps.py, em ready()
from skeleton.system.registry import AttachmentPolicy, SealPolicy, TableSpec, register

register(
    TableSpec(
        model=Document,
        description="Documentos assinados",
        read_fields=("title", "body", "delivered", "corrects"),
        write_fields=("title", "body", "delivered"),  # Nunca sealed_at, sealed_by nem seal_digest (E040).
        expose_api=True,
        attachments=AttachmentPolicy(max_bytes=5_000_000, types=("application/pdf",)),
        seal=SealPolicy(attachments="frozen", comments="open"),
    )
)
```

```sh
python manage.py makemigrations acme_docs
python manage.py record_infrastructure acme_docs --write   # congela a chave seal e o guard
```

```python
# services.py: o ato de assinar, autorizado e com CAS
from skeleton.common.sealing import digest
from skeleton.records.sealing import seal_record


def sign(document, *, user, expected_revision):
    sections = [{"sysid": s.pk, "text": s.text} for s in document.sections.order_by("sysid")]
    return seal_record(document, user=user, expected_revision=expected_revision, extra={"sections": sections})
```

- `seal_record` exige `update` no documento, bloqueia a linha, valida a revisão e grava o selo. O resumo (`seal_digest`) cobre campos congelados, anexos congelados e `extra`, servindo de entrada para uma assinatura digital. Outras apps operam via `RecordSealService`, enviando `extra_digest` calculado com `digest(...)`.
- Invariantes que dependem de linhas relacionadas (como "o documento precisa ter ao menos uma seção") podem ser declaradas em `seal_condition_sql`: expressão SQL sobre `NEW` congelada na migration e validada pelo banco na transição, restrita a tabelas da própria app (recusa `;`, comentários e `$`).
- **Correções ocorrem sempre por adendo**, nunca editando nem desselando o registro original: crie um novo documento selado apontando `corrects` para o original.
- `corrects` figura em `seal_fields` e `immutable_fields` simultaneamente (não muda desde a criação e entra no selo), combinação aceita pelo [modelo de dados](data-model.md#registros-selados-526) e pelo kit de testes.
- Valide o contrato com `skeleton.testing.sealing.assert_sealed_contract` ([testes](testing.md#kit-do-selo)).

## Contêiner e tabelas derivadas

Quando uma equipe colabora em itens filhos pertencentes a um contêiner comum (itens de uma área de trabalho, linhas de um documento), apenas o contêiner é um `SecuredRecord` exposto; os filhos são `OrganizationRecord` não expostos com `access_parent`, herdando toda a autorização da **âncora** ([tabelas derivadas](access-control.md#tabelas-derivadas)). O exemplo abaixo usa os modelos de teste do núcleo (`tests/integration/derived_support.py`):

```python
# models.py
from django.db import models

from skeleton.common.models import OrganizationRecord, SecuredRecord


class Workspace(SecuredRecord):  # A âncora: dono, visibilidade e concessões valem aqui.
    title = models.CharField(max_length=100)


class WorkItem(OrganizationRecord):  # Derivada: sem dono, visibilidade nem concessão próprios.
    workspace = models.ForeignKey(Workspace, on_delete=models.PROTECT, related_name="items")
    title = models.CharField(max_length=100)
    status = models.CharField(max_length=20, default="open")
    immutable_fields = ("workspace_id",)  # Um filho nunca muda de pai (E042).


class WorkItemLine(OrganizationRecord):  # Dois saltos: linha -> item -> área.
    item = models.ForeignKey(WorkItem, on_delete=models.PROTECT, related_name="lines")
    text = models.TextField(default="")
    immutable_fields = ("item_id",)
```

```python
# apps.py, em ready()
from skeleton.system.registry import TableSpec, register

register(
    TableSpec(
        model=Workspace,
        description="Área de trabalho.",
        read_fields=("title",),
        write_fields=("title",),
        expose_api=True,
    )
)
register(
    TableSpec(
        model=WorkItem,
        description="Item de uma área de trabalho.",
        read_fields=("workspace", "title", "status"),
        write_fields=(),
        expose_api=False,
        access_parent="workspace",
    )
)
register(
    TableSpec(
        model=WorkItemLine,
        description="Linha de um item.",
        read_fields=("item", "text"),
        write_fields=(),
        expose_api=False,
        access_parent="item",
    )
)
```

```sh
python manage.py makemigrations acme_work
python manage.py record_infrastructure acme_work --write   # a infraestrutura de sempre; access_parent não muda o banco
```

```python
# services.py: o serviço autoriza o filho como qualquer tabela
from skeleton.access.policies import authorized_queryset, require_parent_action, require_record_action


def open_items(*, user, workspace_id):
    return authorized_queryset(WorkItem, user=user, action="read").filter(workspace_id=workspace_id, status="open")


def add_item(*, user, workspace, title):
    require_parent_action(WorkItem, workspace, user=user, action="create")  # update na área
    item = WorkItem(organization=workspace.organization, workspace=workspace, title=title)
    item.save()
    return item


def close_item(item, *, user, expected_revision):
    require_record_action(item, user=user, action="update")  # update na área
    item.status = "closed"
    item.revision = expected_revision
    item.save(update_fields={"status"})
    return item
```

```python
# tests/test_work_items.py (PostgreSQL com a role runtime)
from skeleton.testing.derived import assert_derived_access_contract
from skeleton.testing.service_api import build_service_scenario


def test_items_follow_the_workspace(isolated_db):
    scenario = build_service_scenario(WorkItem, slug="work")  # As permissões vão para a tabela âncora.

    def make_anchor(*, organization, owner):
        workspace = Workspace(organization=organization, owner=owner, visibility="private", title="Área")
        workspace.save()
        return workspace

    def make_item(workspace):
        item = WorkItem(organization=workspace.organization, workspace=workspace, title="Item")
        item.save()
        return item

    assert_derived_access_contract(
        child_model=WorkItem, make_anchor=make_anchor, make_child=make_item, scenario=scenario
    )
```

- `TablePermission`, escopos de token e concessões são avaliados na âncora com mapeamento de ação: `read` no filho exige `read` na âncora; `create`, `update`, `delete` e `restore` exigem `update`; `manage_access` no filho é sempre negado.
- Eventos de domínio, notificações, anexos, comentários, selo, `AccessService.can`/`require`, histórico de auditoria e origem de lançamento no livro contábil suportam filhos derivando a autorização da âncora.
- Permanecem fechados para tabelas derivadas: gateway genérico de registros, API REST genérica e OpenAPI, seletor de referências, `RecordOperations`, concessão direta de `TablePermission`/`RecordGrant`, CSV, interface declarativa e sujeito de fluxo. A app apresenta e edita os filhos em suas próprias páginas.

## Quadros do painel da organização

Uma app pode oferecer quadros para o painel inicial interno da organização ([páginas em blocos](ui-design.md#páginas-em-blocos)) registrando um `DashboardWidget` em `AppConfig.ready()` ([#620](https://github.com/alaorneto/skeleton/issues/620)). `skeleton.system.dashboard_widgets` é [superfície pública](service-api.md#superfícies-públicas-do-núcleo-para-apps) expondo `DashboardWidget` e `register_dashboard_widget`. Exemplo completo do quadro `example_notes.recent` em `examples/notes`:

```python
# examples/notes/widgets.py
from django.utils.translation import gettext_lazy

from skeleton.system.dashboard_widgets import DashboardWidget, register_dashboard_widget

from .service_api import NoteService


def recent_context(*, request, actor):
    # Autoriza pela API de serviços da própria app: só o que o ator lê, default deny.
    return {"notes": NoteService().recent_notes(actor=actor, limit=5)}


RECENT = DashboardWidget(
    name="example_notes.recent",  # "<owner_app>.<slug>", slug [a-z0-9_]{1,40}
    owner_app="example_notes",  # app_label de quem registra (prefixo de name)
    label=gettext_lazy("Anotações recentes"),  # editor e título padrão do quadro
    template="example_notes/widgets/recent.html",
    context=recent_context,
    table_key="example_notes.note",  # porta opcional, como uma entrada de menu
    action="read",
)


def register():
    register_dashboard_widget(RECENT)  # chamado no NotesConfig.ready()
```

```django
{# examples/notes/templates/example_notes/widgets/recent.html #}
{% load i18n %}{% if notes %}<ul class="space-y-2 text-sm/6">{% for note in notes %}<li><a href="{% url 'web:record' organization_id 'example_notes.note' note.sysid %}">{{ note.title }}</a></li>{% endfor %}</ul>{% else %}<p class="text-sm/6 text-body-subtle">{% translate "Nenhuma anotação ainda." %}</p>{% endif %}
```

- **Nome e validação:** nome repetido com declaração divergente, slug inválido ou prefixo diferente de `owner_app` levanta `ImproperlyConfigured` no `ready()`. O check `skeleton.E045` recusa `owner_app` não instalada, `table_key` sem `TableSpec`, `action` inválida, template ausente ou `context` não chamável.
- **Visibilidade:** `visible_widgets(user)` retorna apenas quadros de apps efetivas na organização atual (instalada, disponível, ativa e com dependências; somente apps com manifesto) cuja porta o ator satisfaz (`(table_key, action)` ou `capability`). As permissões são consultadas em uma única query. A porta apenas oculta o quadro — nunca concede acesso, pois o `context` deve autorizar via API de serviços da própria app.
- **Renderização segura:** `skeleton.web.widgets.render_widget(request, widget)` executa `context` no contexto de tenant da requisição (nunca em `system_context`) dentro de um savepoint e renderiza o template da app com o dicionário retornado somado a `organization_id` e `widget`, sem processadores de contexto. Qualquer exceção degrada para o quadro neutro "Este quadro não está disponível agora." (erros inesperados são registrados por `log_internal_error` com classe e pilha sanitizada, sem a mensagem; recusas da API de serviços como `NotFound`, `Forbidden` e `Unavailable` não contam como erro interno), garantindo que nenhum quadro cause erro 500 na página.
- **Uso no painel:** o administrador adiciona o quadro ao [início interno](ui-design.md#início-da-organização) em `/o/<org>/admin/pages/home/` (bloco `widget`). No painel, ele é exibido como cartão com `label` como título apenas para quem passa na porta. Desativar a app oculta o quadro sem invalidar a página, e religá-la o restaura.

## Permissões e fluxo HTML

Bootstrap e sincronização garantem automaticamente `TablePermission` para `organization_admin` em tabelas expostas; o papel `member` não recebe concessões automáticas. Para conceder acesso a outros usuários, acesse `/o/<org>/access/roles/`, crie um papel, atribua as ações desejadas (`read`, `create`, `update`, `delete`, `restore` e, se aplicável, `manage_access` e `audit`) em `table-permissions` e vincule pessoas via `MemberRole` ou `GroupRole` + `GroupMember`. Todas essas operações exigem administração da organização e, em produção, MFA.

Fluxo ponta a ponta na interface HTML:

1. faça login real em `/accounts/login/`;
2. acesse o painel `/o/<org>/` e descubra as tabelas disponíveis em `/o/<org>/catalog/`;
3. abra a listagem `/o/<org>/tables/example_notes.note/`;
4. crie uma anotação em `/o/<org>/tables/example_notes.note/new/` com `code`, `title` e `body` (CSRF obrigatório; nasce com `owner=ator` e `visibility=private`);
5. consulte o detalhe em `.../<sysid>/` e edite em `.../<sysid>/edit/` (formulário inclui campo oculto de revisão);
6. gerencie o compartilhamento em `.../<sysid>/access/` (`owner`, `visibility` e `owning_group`, exigindo `manage_access` + propriedade ou administração);
7. exclua logicamente em `.../<sysid>/delete/` e restaure em `.../<sysid>/restore/` via POST com revisão (`GET` nunca altera estado; tombstones saem da lista viva);
8. confira o histórico em `/o/<org>/audit/`, onde `body` aparece redigido e contagem e listagem respeitam a autorização.

As páginas de erro do Django (`400.html`, `403.html`, `403_csrf.html`, `404.html` e `500.html`) vêm do núcleo (`skeleton/web/templates/`) e estendem `skeleton/web/error_base.html` ([interface](ui-design.md#páginas-de-erro)). Um projeto derivado pode sobrescrevê-las colocando arquivos de mesmo nome em `templates/` na raiz do projeto (lembrando que `500.html` não pode usar `{% url %}` nem variáveis de contexto). Erros operacionais em views devem usar `skeleton.web.errors.error_response`, nunca `render` direto com `error.html`. Fora as páginas de erro, nunca sobrescreva os templates do shell do núcleo (`skeleton/web/base.html`, `error_base.html`, `platform/base.html`, `skeleton/portal/base.html`, `skeleton/email/base.html`, `account/base_entrance.html`, `account/base_manage.html`): `manage.py check` alerta com `skeleton.W046` porque cópias locais deixam de receber evoluções do núcleo. A marca é configurada em [aparência da instância](appearance.md), e temas seguem [criar e publicar um tema](themes.md).

A interface exibe apenas os botões autorizados, mas o servidor revalida todas as ações. Um membro comum que ganha leitura via visibilidade `organization` continua sem permissão de escrita. Novos relacionamentos de negócio exigem `PROTECT`, guarda de mesma organização e seletor autorizado de FK.

## Fluxo API de ponta a ponta

Use os cookies e o token CSRF de uma sessão real do allauth, como explicado em [api](api.md). Mantenha `BASE`, `ORG`, `SESSION` e `CSRF` exclusivamente em variáveis locais privadas:

```sh
URL="$BASE/api/v1/o/$ORG/tables/example_notes.note/records"
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" "$URL/"
curl -i -X POST -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H 'Content-Type: application/json' \
  --data '{"code":"tutorial","title":"Exemplo","body":"Não vai para audit"}' "$URL/"
# NOTE é sysid devolvido; ETAG é o header atual incluindo aspas.
curl -i -b "sessionid=$SESSION; csrftoken=$CSRF" "$URL/$NOTE/"
curl -i -X PATCH -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H "If-Match: $ETAG" \
  -H 'Content-Type: application/json' --data '{"title":"Editado"}' "$URL/$NOTE/"
# Releia ETAG antes de cada mutação seguinte.
curl -i -X DELETE -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H "If-Match: $ETAG" "$URL/$NOTE/"
# DELETE não devolve corpo; soft delete incrementa revision. Se ETAG antes era
# "4", restore precisa "5", não o ETag stale do registro vivo.
curl -i -X POST -b "sessionid=$SESSION; csrftoken=$CSRF" \
  -H "X-CSRFToken: $CSRF" -H "Referer: $BASE/" -H 'If-Match: "5"' "$URL/$NOTE/restore/"
```

O valor `"5"` acima ilustra apenas a transição `4 → 5`, não uma revisão fixa. Como alterações de acesso ou concessões também podem avançar a revisão, nunca adivinhe revisões em fluxos concorrentes e releia o registro após o `restore`. Tentar alterar organização ou `owner` no `PATCH` comum gera erro, e nomes físicos SQL ou `table_key` desconhecidos nunca abrem tabelas.

## Novas operações de negócio

Implemente toda regra de negócio nos serviços da própria app, nunca dentro de `skeleton`. Cada serviço roda dentro de um contexto já aberto (organização e ator confiáveis), parte de `authorized_queryset` para a ação pretendida, valida as regras de domínio e persiste com `save`/CAS. Em views, o contexto vem da camada tenant; em jobs, o próprio job abre `tenant_context` (uma transação independente por organização) e materializa os resultados antes de sair. Nunca recorra a `all_objects` nem a SQL direto como atalho em endpoints. Sempre que uma operação precisar ser consumida por outra app, exponha-a na [API de serviços](#api-de-serviços-da-app) em vez de expor o módulo interno.

Toda referência a outro registro organizacional deve apontar para um registro vivo da mesma organização e legível pelo ator. Combine validação no serviço, seletor autorizado de FK e constraints/guards no banco — comparar UUIDs isoladamente não basta. Relações entre registros de negócio devem usar `PROTECT` (evitando remoção com referências vivas), e associações muitos-para-muitos devem ser modeladas como tabelas explícitas com UUID, nunca M2M implícito. Declare todos os campos sensíveis no `TableSpec` e na especificação congelada da migration antes de gravar dados auditáveis.

Siga o checklist em [AGENTS](../AGENTS.md) e [testing](testing.md), mantendo testes reais de API pública, isolamento A/B, default deny, auditoria redigida, concorrência, referências e schema checks — inspeção textual de código-fonte não comprova esses contratos.

## Texto traduzível e outros idiomas

O núcleo é distribuído apenas em `pt-BR`, e as *msgids* do gettext correspondem ao próprio texto em português ([#171](https://github.com/alaorneto/skeleton/issues/171), [#172](https://github.com/alaorneto/skeleton/issues/172)). Adote as mesmas convenções nas apps:

- **Python:** use `gettext("…")` dentro de funções e `gettext_lazy("…")` em constantes de módulo ou classe (rótulos de campos, `TextChoices`, `TableSpec`, tipos de notificação, `verbose_name` e registros em `ready()`). Para interpolação, use `gettext("Use %(campo)s.") % {"campo": valor}`, nunca f-strings dentro do gettext.
- **Templates:** carregue `{% load i18n %}` e use `{% translate "…" %}` (ou `_("…")` em argumentos de tags).
- **O que fica fora do gettext:**
  - descrições e resumos do schema OpenAPI (`openapi = {...}`, `Operation(...)`), que formam contrato estável para integradores independentemente do idioma do usuário;
  - literais verificados estruturalmente pelo código, como cabeçalhos de documentação (`## Serviços`) e seções de docstring da API de serviços (`Autorização:`);
  - códigos de erro (`code`) e chaves JSON;
  - logs e eventos de segurança;
  - diagnósticos técnicos em inglês (system checks, manifestos, pacotes e `PermissionDenied` internos já convertidos em 403/404 neutros nas fronteiras).
- **JSON:** textos *lazy* não são serializados por `json.dumps`; converta com `str()` sempre que serializar fora de `JsonResponse` e do DRF.
- **Validação:** `python tools/i18n_check.py` (requer GNU gettext) extrai o catálogo e valida via `msgfmt --check-format`; `tests/integration/test_i18n_coverage.py` detecta textos sem tradução nas páginas principais por pseudolocalização.

Para oferecer idiomas adicionais, o projeto derivado deve:

1. incluir o idioma em `LANGUAGES`;
2. gerar o catálogo em `LOCALE_PATHS` com `manage.py makemessages -l <idioma>`, traduzindo a partir das msgids em `pt-BR`;
3. compilar com `compilemessages`;
4. caso queira formatos de data personalizados, criar `formats/<idioma>/formats.py` e posicioná-lo **antes** do núcleo em `FORMAT_MODULE_PATH` (`["config.formats", "skeleton.conf.formats"]`), pois o núcleo define apenas `pt-BR` (`DD/MM/AAAA HH:mm`) — omitir `skeleton.conf.formats` reverte ao formato longo padrão do Django.

E-mails aceitam ainda templates específicos por idioma (`body.<idioma>.html`), que têm precedência sobre o gettext.
