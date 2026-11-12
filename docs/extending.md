# Criar um projeto e adicionar negócio

Uma app de negócio é um módulo Django do projeto derivado, como tarefas ou anotações; o núcleo fornece identidade, isolamento, autorização e auditoria. Este roteiro separa três responsabilidades: escrever modelos e serviços seguros, declarar como a app se integra e publicar código para instalação. Consulte [apps, pacotes e deployment](concepts.md#apps-pacotes-e-deployment) e [registros e banco de dados](concepts.md#registros-e-banco-de-dados).

## Copiar o template sem renomear o núcleo

Use a função Git template do seu provedor ou clone/cópia versionada. Não copie .env, .venv, bancos, .private, cookies ou chaves de fixture.

```sh
git clone "$TEMPLATE_REPO" meu-projeto
```

Configure identidade visual/nomes do projeto, env privado, hosts/remetente/IdPs e secrets. Preserve módulos `skeleton`, app labels e AUTH_USER_MODEL. Apps próprias entram em `PROJECT_APPS` de `config/settings/project.py`; rotas do projeto em `config/urls_project.py`. Não edite núcleo para cada negócio; núcleo nunca importa applications/examples.

```python
# config/settings/project.py
PROJECT_APPS = ["applications.tasks.apps.TasksConfig"]
```

Crie um `AppConfig` com `name="applications.tasks"` e um label único, como `project_tasks`; o label identifica a app no Django e nas migrations, por isso não reutilize os labels do núcleo ou do exemplo. Registre metadados em `ready()` sem consultas ao banco. URLs e tarefas de segundo plano devem chamar serviços autorizados, não managers internos que escapem das políticas.

## Começar uma app com `skeleton_startapp`

O ponto de partida recomendado para uma app nova é o gerador do núcleo, em vez de copiar `examples/notes` ou escrever os arquivos à mão:

```bash
python manage.py skeleton_startapp project_tasks --app-id com.example.tasks --publisher example-publisher --model Task
```

Ele cria `applications/<package>/` (por padrão o package é o label; mude com `--package-name`, e o diretório-pai com `--directory`) com:

- `apps.py`: `AppConfig` com `name = __package__` e o label informado; `ready()` registra o manifesto com `register_app` e um `TableSpec` com `read_fields`, `write_fields` e `sensitive_fields` explícitos;
- `models.py`: um modelo `SecuredRecord` mínimo (`code` único entre registros vivos da organização e `title`);
- `service_api.py`: `<Model>Service` (`ServiceAPI`) com `<model>_count`, `list_<model>_summaries` e `get_<model>`, DTO `<Model>Summary` e `app_label` da app;
- `views.py`, `urls.py` e `api_urls.py`: uma página `tenant_page` e um sumário `TenantAPI`, ambos chamando o serviço, sem consultar o modelo;
- `tests/test_service_api.py`: o teste de contrato `assert_service_contract(<Model>Service)`;
- `templates/<app_label>/home.html`, `migrations/__init__.py`;
- `docs/spec.md`, `docs/adr.md` e `docs/service-api.md` com as seções exigidas pela check E012 (texto inicial a substituir);
- `manifest.json` v2 gerado por `manifest_template`, com `runtime.urls`, `runtime.api_urls`, `runtime.service_api` e uma entrada de navegação apontando para os módulos gerados. A compatibilidade começa em `>=0.4.0,<1.0.0` e `default_enabled` começa `false`: o campo é obsoleto e ignorado (#22), e `package doctor` avisa quando ele é `true`.

O comando valida label, package, IDs e nome do modelo com os mesmos validadores de `skeleton.system.packages`. Ele recusa namespaces reservados (`sk_*`, `skeleton`, `config`…) e diretório existente. Também não edita settings. Os modelos de código ficam em `skeleton/system/app_template/` e são distribuídos no wheel do core. Ao final, o comando imprime os próximos passos:

1. adicionar `applications.<package>.apps.<Package>Config` a `PROJECT_APPS` em `config/settings/project.py`;
2. `python manage.py makemigrations <app_label>`;
3. `python manage.py record_infrastructure <app_label> --write` (migration de infraestrutura, veja [Migration de segurança](#migration-de-segurança));
4. `python manage.py migrate`;
5. `python manage.py sync_table_catalog`;
6. `python manage.py check_skeleton_schema`;
7. atualizar `migrations.leaves` no manifesto, revisar spec/ADR e escrever a API de serviços da app (`service_api.py` e `docs/service-api.md`, veja [API de serviços da app](#api-de-serviços-da-app));
8. `python manage.py package doctor applications/<package>` (seção [Diagnosticar antes de empacotar](#diagnosticar-antes-de-empacotar));
9. publicar com `package build` (seção [Publicar apps e releases assinados](#publicar-apps-e-releases-assinados)).

O código gerado é um começo, não uma app aprovada: revise allowlists e `sensitive_fields` a cada campo novo e escreva os testes de isolamento, autorização e concorrência do checklist do `AGENTS.md`.

### Manifest, ativação e superfícies próprias

O manifesto é o contrato versionado da app: identifica o componente e declara o que ele precisa e o que oferece ao projeto. Cada app carrega o `manifest.json` canônico com `AppManifest.from_json(Path(self.path) / "manifest.json", app_config=self)` e registra o resultado em `AppConfig.ready()`, separadamente dos `TableSpec` que descrevem suas tabelas. O contrato declara ID estável, versão, `publisher` (responsável pela publicação), label/package, intervalos de compatibilidade e dependências **por app ID com intervalo de versão**. Dependências de apps não são nomes de distribuições Python.

A seção de runtime descreve configuração escalar, navegação, módulos de URLs/API, política de habilitação e documentação. No formato 2, acrescenta `administration` para seções/telas de parâmetros e `model_ui` para apresentação de negócio fora do admin. `ConfigField`, `MenuEntry` e specs imutáveis de apresentação são representações do manifesto canônico; não mantenha uma segunda versão manual. Registrar esses contratos em `ready()` não consulta banco nem executa DDL. Dependências devem estar instaladas, com versões compatíveis e sem ciclos.

As rotas declaradas são incluídas antes dos gateways, exclusivamente sob `/o/<org>/apps/<app_label>/` e `/api/v1/o/<org>/apps/<app_label>/`. Views usam `tenant_page`/`TenantAPI` e serviços autorizados; esses prefixes recebem contexto tenant validado. `config.urls_project` também precede os gateways para não capturar rotas do projeto. Não crie rotas organizacionais fora deste contrato.

**Instalação global e ativação por organização são operações diferentes.** Instalar disponibiliza código e migrations no ambiente compartilhado; ativar autoriza que uma organização use uma app já instalada, sem instalar Python nem criar tabelas. `/o/<org>/apps/` administra ativação/configuração e apresenta a versão declarada. `AppInstallation`, um `OrganizationRecord` com UUID, guarda esse estado com infraestrutura de migration e auditoria.

A configuração JSON é redigida integralmente na auditoria. Segredos pertencem à configuração privada do deployment, nunca à configuração organizacional. Sem linha persistida, a app está **desativada** na organização: o `default_enabled` do manifesto não ativa mais nada (#22). A API exige `If-Match`: use a revisão virtual `"0"` na primeira escrita e, depois, a revisão atual. CAS evita sobrescrever configuração concorrente; o bloqueio de Organization serializa também as validações de dependências. Configura quem tem a capacidade `apps.manage`: o gestor de apps (`app_manager`) ou o administrador (#24). Quem não a tem recebe 404 no painel e na API. O painel lista só as apps disponíveis para a organização; uma app ativa que a plataforma retirou aparece sinalizada, com o negócio bloqueado, e aceita apenas a desativação (qualquer outro PUT responde 400 `app_unavailable`). Telas tenant `custom` marcadas `organization_admin_only` no manifesto também exigem `apps.manage`: o nome do campo continua aceito por compatibilidade.

O estado das apps de uma organização (`application_states()`: disponibilidade, ativação, configuração e uso efetivo com as dependências) é lido em consultas fixas, qualquer que seja o número de apps, e memoizado só durante a requisição; uma escrita na própria requisição desliga o memo, e nada fica em cache entre requisições, de modo que uma revogação vale na requisição seguinte. `navigation_for_user`, `require_application_enabled` e `authorized_queryset` usam esse estado; o menu avalia as permissões de tabela numa única consulta (`table_permissions`).

Desativar bloqueia endpoints próprios, gateway e políticas e remove o menu, mas preserva registros, IDs e ACL (regras de acesso). O bloqueio fica na URL, antes da view: `runtime.urls` passa por `guard_application_urls` e `runtime.api_urls` por `guard_application_api_urls`, então um endpoint que não chama `authorized_queryset` também fecha. Com a app, ou uma dependência transitiva dela, desativada, a API responde 404 JSON neutro (`{"code": "not_found"}`); a configuração do núcleo em `/api/v1/o/<org>/apps/<app_label>/configuration/` continua disponível ao administrador para reativar. Reativar não concede `TablePermission`. Instalação, atualização e retirada global do código são solicitadas pela plataforma em `/platform/updates/` e executadas por um agente separado; a requisição web não executa instalador nem DDL. Remoção de dados/schema permanece exclusivamente offline e revisada.

Não há editor genérico do Django admin: modelos registrados com `@admin.register` não são roteados, porque um ModelAdmin contornaria os serviços de lifecycle, CAS e auditoria. Telas globais de uma app são declaradas no manifesto como `administration.screens` de escopo `platform` e `kind: custom`, com handler registrado por `register_admin_handler`; elas aparecem em `/platform/apps/<app_label>/` sob o mesmo guard da plataforma (e reautenticação quando `sensitive`). Negócio tenant fica no viewer: `/o/<org>/catalog/` e `/o/<org>/tables/<table_key>/`.

O [contrato de administração e interfaces](application-admin-ui-plan.md) documenta grupos automáticos, telas de parâmetros, listas/layouts e serviços próprios. Novos manifestos usam `manifest_version=2` e compatibilidade inicial `>=0.4.0,<1.0.0` (piso exigido por `runtime.service_api`); o piso histórico v2 sem dimensões permanece 0.2.0, e v1/v2 assinados continuam legíveis sem normalizar JSON ou alterar bytes/digest. A presença de `col_span`/`rows`, inclusive `null`, exige piso 0.3.0 declarado na fonte antes de construir o pacote. Atualize primeiro agente e core para 0.3.0. `administration.screens` admite somente `configuration` ou `custom` de parâmetros, nunca `model_list`/metadados de tabela. `TableSpec`, não o layout, autoriza leitura/escrita/sensibilidade.

O core 0.3.0 distribui `django-crispy-forms==2.7` e o pack SSR `skeleton_crud` no wheel. Customize somente `runtime.model_ui` do manifesto canônico, sem arquivo UI paralelo nem editor no admin. Notes 0.2.0 demonstra abas Identificação/Conteúdo, labels Código/Título/Texto e spans `code:4`, `title:8`, `body:12` com `rows:8`; o [exemplo JSON completo](application-admin-ui-plan.md#3-manifesto-canônico-v2) mantém IDs, seções e ordem. `col_span` aceita inteiro exato 1..12, `rows` inteiro exato 1..40; omissão/`null` mantém defaults, mas bool/string/float são recusados. Rows exige textarea editável efetivo (ou override `textarea`/`json` compatível), nunca readonly/input/select/relação, sem mudar validators. Seção `columns:1/2` define largura padrão 12/6, campos largos usam 12 e spans explícitos prevalecem. A grade preserva DOM/foco e, até 600px, todos os campos ocupam a linha inteira.

Modelos elegíveis sem `model_ui` recebem defaults sem Form/template próprios. O renderer conserva CSRF, media, validação, CAS e persistência dos formulários existentes, incluindo perfil, acesso e configuração. O viewer genérico exige `SecuredRecord` exposto por `TableSpec.expose_api=True`; `SystemRecord`/`OrganizationRecord` globais ou técnicos continuam com serviços/superfícies dedicados, não ganham CRUD por declarar layout.

### Dependências entre apps

`dependencies` mapeia o app ID de cada app exigida para uma faixa de versão, como `{"com.example.customers": ">=1.2,<2.0"}`. `conflicts` usa o mesmo formato para versões que não podem coexistir. O core não entra em `dependencies`: a faixa do núcleo é `compatibility.skeleton`, e o manifesto que tentar declarar `django-skeleton` como dependência é recusado. Não há dependências opcionais; uma integração que só funciona com outra app presente precisa declará-la.

O mesmo resolvedor (`skeleton.system.dependencies.resolve_dependencies`) roda em quatro momentos e relata todos os problemas de uma vez, cada um com um código estável:

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

A ordem resolvida vale para o inventário e para `INSTALLED_APPS`: uma app sempre vem depois das que ela exige, então o `ready()` dela encontra as dependências já registradas.

Acima da ativação há a **disponibilidade** decidida pela plataforma (`skeleton/system/availability.py`): `set_app_availability` (CAS, revisão virtual 0), `grant_app` e `revoke_app` exigem administrador de plataforma e só escrevem linhas globais. `grant_app` recusa uma app cuja dependência esteja indisponível para a organização (`dependency_unavailable`); `revoke_app` recusa enquanto uma dependente estiver concedida (`dependent_granted`). `application_state` informa `available`, `require_application_enabled` exige disponibilidade da app e das dependências e responde 404 quando falta (`ApplicationUnavailable`), e `configure_application` recusa ativar app indisponível (`app_unavailable`), mas permite desativar. Sem linha de disponibilidade, a app está **indisponível** para todas as organizações (#22); conceder uma organização a uma app sem linha cria `selected`, nunca `all`. Re-conceder restaura a mesma concessão e devolve a escolha de ativação anterior da organização.

### Default fechado e upgrade da ativação (#22)

Instalar não disponibiliza nem ativa: uma app recém-instalada nasce `selected`, sem concessões (`synchronize_inventory` cria a linha quando o inventário traz um componente novo), e nenhuma organização a usa até a plataforma conceder e o administrador da organização ativar. `runtime.default_enabled` continua aceito no schema v2 (pacotes assinados não mudam), mas é ignorado; `AppManifest.default_enabled` existe só para o upgrade abaixo.

As organizações que usavam apps ativas por padrão mantêm essas apps com `manage.py upgrade_app_activation` (`--dry-run` lista o que faria, sem escrever). O comando lê o registro de manifestos e o inventário vivos, por isso não é uma data migration, e é retomável por `AppAvailability.upgrade_state`: (1) numa transação global, cria `mode=all` (`pending`) para cada app instalada antes do upgrade e sem linha, e marca `pending` as linhas anteriores ao #22; (2) uma transação por organização, em `system_context(reason="upgrade:app-activation")`, cria `AppInstallation(enabled=True)` onde o default antigo era verdadeiro e a organização nunca configurou a app; (3) marca `done`. Interromper e rodar de novo conclui sem duplicar linhas, e uma organização criada depois do upgrade não ganha app ativa. "Instalada antes" vem de `ComponentInstallation` quando há inventário do agente; sem inventário (deploy manual ou desenvolvimento), todas as apps carregadas contam, só na primeira execução. O default antigo é o `default_enabled` do manifesto carregado: não mude esse valor no mesmo release em que o upgrade roda. O exemplo Notes passou a declarar `false`, então um banco de desenvolvimento antigo não o reativa sozinho; use `set_app_availability`.

Em desenvolvimento, `manage.py set_app_availability <app_label> --mode all|selected [--grant SLUG] [--enable SLUG]` abre a app e a ativa nas organizações indicadas, uma transação por organização. `check_skeleton_schema` avisa sobre app carregada sem linha de disponibilidade (fechada para todos). Nos testes, `skeleton.testing.applications` oferece `open_app`, `activate_app` e `apps_open_by_default`.

Para declarar tipos só dentro de um teste, `skeleton.testing.registries` isola os registries do núcleo e devolve os objetos originais na saída, mesmo quando o teste falha: `isolated_job_types()` (tipos de job e jobs periódicos), `isolated_event_types()` (tipos de evento, copiando os do núcleo; consumidores vazios, ou copiados com `keep_consumers=True`), `isolated_notification_types()`, `isolated_token_scopes()` (escopos nomeados de token) e `table_spec_override(table_key, **mudanças)`, que troca o `TableSpec` de uma tabela por uma cópia alterada. Use-os num fixture com `yield` dentro do `with`; não combine com `monkeypatch` no mesmo registry, porque a ordem de restauração dos dois deixaria o valor alterado para o teste seguinte.

Uma tabela `SecuredRecord` pode aceitar anexos com `TableSpec(attachments=AttachmentPolicy(max_bytes=..., types=(...)))`; a autorização vem do registro alvo. Os anexos contam na cota de armazenamento da organização e, com antivírus configurado, só podem ser baixados depois da verificação. Veja [arquivos e anexos](files.md#anexos).

Do mesmo modo, `TableSpec(comments=CommentPolicy())` abre comentários na tabela: quem lê o registro lê e escreve comentários, só o autor edita e o administrador modera. Veja [comentários](comments.md).

A ativação por organização segue o fecho transitivo. Para habilitar uma app, todas as que ela exige, direta ou indiretamente, precisam estar habilitadas; para desabilitar, nenhuma app habilitada pode depender dela. A mensagem de erro lista as apps que bloqueiam.

Declarar a dependência no manifesto é obrigatório sempre que o código a usa. As checks abaixo, que não consultam o banco, cobram isso das apps registradas (lista completa: [checks](checks.md)):

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

Dependência declarada não autoriza importar qualquer módulo da outra app. O único ponto de consumo é a API de serviços dela (próxima seção).

### API de serviços da app

Toda app publica as operações que outras apps podem chamar num módulo `service_api.py`, com classes de serviço em processo. O contrato completo está em [API de serviços entre apps](service-api.md), e o procedimento no skill `service-api`. Em resumo:

- métodos públicos são keyword-only, recebem `actor` e devolvem DTOs imutáveis ou `Page` de DTOs, nunca modelos ou querysets;
- a autorização acontece dentro do serviço (`authorized_queryset`, `require_record_action`, capacidades), e a app provedora precisa estar ativa na organização;
- o serviço participa da transação do chamador e deixa efeitos externos para jobs, eventos ou `on_commit`;
- outras apps importam só `<app>.service_api` e declaram a dependência acima;
- views e endpoints da própria app também chamam o `service_api`, para haver um único caminho de regra.

A declaração no manifesto é obrigatória para novos releases:

```json
"runtime": {"service_api": {"module": "applications.tasks.service_api", "classes": ["TaskService"]}}
```

O módulo tem de estar dentro do pacote da app, e as classes são nomes simples. Declarar o campo exige `compatibility.skeleton` com piso `>=0.4.0`, a primeira versão do core que o lê; `package build` acrescenta o piso ao artefato. Os checks E018/E019 e o `package doctor` verificam o contrato; o [kit de testes](service-api.md#kit-de-testes-de-contrato) prova autorização, isolamento e CAS. O exemplo Notes (`examples/notes/service_api.py`) e o gerador já seguem o contrato. A análise de imports não alcança imports dinâmicos (`importlib`, `__import__`), que continuam questão de revisão.

### Apps first-party do núcleo (`skeleton/contrib`)

Algumas apps genéricas (a agenda é a primeira, #84) vêm **dentro do wheel do núcleo**, em `skeleton/contrib/<pacote>`, em vez de um `.skapp` separado: um só trem de release, sem matriz de compatibilidade entre elas e o core. O contrato (#66):

| Item | Regra |
|---|---|
| Manifest | `kind: "bundled_app"`, manifest v2, `app_label` com o prefixo reservado `skeleton_`, `app_config` em `skeleton.contrib.<pacote>.apps.<Config>` e módulos de runtime sob `skeleton.contrib.<pacote>.`. Sem `files` nem `python_dependencies` próprios |
| Local e versão | O manifest só é aceito dentro do pacote `skeleton` instalado, e a versão tem de ser **igual** à do core: uma divergência para o startup (`ImproperlyConfigured`) |
| Prefixo reservado | Uma app de terceiro com label `skeleton_*`, ou com AppConfig em `skeleton.contrib`, é recusada; `package build` recusa um manifest `bundled_app` |
| Instalação | Opt-in pelo operador: `SKELETON_BUNDLED_APPS=calendar` (lista de nomes do mapa fixo de `skeleton/conf/bundled.py`; nome desconhecido é `ImproperlyConfigured`). O ambiente nunca fornece import path |
| Rotas do usuário | Só first-party: `runtime.user_urls` monta rotas globais da pessoa em `/me/apps/<label>/`, com login obrigatório e sem cache, no namespace `app_name` do módulo (diferente do `app_name` das rotas da organização). Como não há organização na URL, cada view lê as organizações da pessoa uma transação por vez (`for_each_organization`, #74) e pula as que não ativaram a app |
| Ciclo de vida | Igual ao das demais apps ([#67](https://github.com/alaorneto/skeleton/issues/67)): recém-instalada, fica indisponível (`selected` sem concessão) e desativada em todas as organizações. Retirar com dados preservados e reinstalar estão em [deployment](deployment.md#apps-first-party) |

O núcleo nunca importa `skeleton.contrib` (`tests/unit/test_bundled_apps.py` prova no CI), e a suíte do núcleo roda sem nenhuma app first-party instalada.

### Documentação obrigatória por app

`documentation_root` aponta para um diretório versionado com `spec.md` (`## Contract`, `## Acceptance`) e `adr.md` (`## Context`, `## Decision`, `## Consequences`). Especifique persistência, autorização, ativação/configuração, endpoints, sensibilidade, concorrência e aceites observáveis; registre decisões e tradeoffs no ADR. Checks rejeitam documentação faltante. Veja `examples/notes/docs/`.

O mesmo diretório tem `service-api.md` com a referência da API de serviços da app: serviços, métodos (Autorização, Erros, Efeitos), DTOs, eventos e histórico de versões. O modelo está no skill `service-api`. A check E012 exige o arquivo, com as seções `## Serviços`, `## DTOs` e `## Histórico de versões`, em toda app que declara `runtime.service_api`; `package build` e `package doctor` também o exigem. A página `/platform/apps/<app_label>/documentation/` mostra o arquivo quando ele existe.

### Diagnosticar antes de empacotar

`package doctor` revisa a fonte de uma app e lista todos os problemas que o `build`, a instalação ou as checks encontrariam, sem importar código, construir pacote nem escrever arquivos. Ele roda com ou sem Django:

```sh
.venv/bin/python -m skeleton.system.packages doctor applications/tasks
.venv/bin/python manage.py package doctor applications/tasks \
  --inventory /private/target/inventory.json \
  --catalog /private/index.json --trust-store /private/trusted-publishers.json
```

Sem opções, ele confere o manifesto, os arquivos que o `build` recusaria, `apps.py`, as seções de `spec.md` e `adr.md` e `migrations.leaves` contra os arquivos de `migrations/`. Com `--inventory`, resolve as dependências contra a instalação de destino; com `--catalog`, também sugere releases compatíveis para cada dependência faltante. A saída é JSON com `ok` e `problems` (cada um com `code` e `message`), e o processo termina com status 1 quando há problema.

As folhas são calculadas lendo `dependencies` de cada migration como literal. Se uma migration calcular as dependências em código, o `doctor` reporta `migration_leaves_unverified` e o `build` não consegue conferir; nesse caso confira as folhas à mão. Quando as folhas são determináveis e divergem do manifesto, o `build` recusa o pacote.

### Publicar apps e releases assinados

Publicar produz um artefato `.skapp` verificável; não significa instalá-lo nem aprová-lo para produção. O fluxo abaixo cria as chaves do publicador, gera e revisa o manifesto, constrói o pacote e verifica sua integridade e procedência. O operador que instala mantém sua própria lista de publicadores confiáveis. Um hash SHA-256 detecta alteração de bytes; a assinatura demonstra que a chave confiável aprovou aquele manifesto. Hash correto, sozinho, não identifica quem publicou.

Os utilitários funcionam tanto por `manage.py package` quanto sem settings/Django inicializado por `.venv/bin/python -m skeleton.system.packages`. O agente externo usa as mesmas funções puras de validação; nunca importa código de um upload antes de verificar assinatura, inventário e hashes. Assinatura controla procedência, **não** transforma Python/migrations em sandbox: aprove somente publishers que podem executar código privilegiado.

Uma app usa `name = __package__` em seu AppConfig para funcionar no checkout e no namespace gerenciado `applications.<package_name>`. O `app_config` e os módulos declarados no JSON são sempre gerenciados (`applications.tasks.apps.TasksConfig`, `applications.tasks.urls`); o loader adapta módulos ao AppConfig local e valida identidade. Templates/static ficam em `templates/<app_label>/` e `static/<app_label>/`; como a política de conteúdo é de mesma origem, templates de página não usam `<script>` inline, atributo `style=` nem handlers `on*=` ([Content-Security-Policy](deployment.md#content-security-policy)). A app não escreve em config, settings ou arquivos de outra app. Versione `spec.md` e `adr.md` com as seções obrigatórias acima e mantenha todas as migrations históricas.

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

O trust store, ou lista de chaves de publicadores confiáveis, é um JSON administrado pelo operador, por exemplo `{"example-publisher": "/private/publisher.pub.pem"}`. A chave do mapa é o `publisher` do manifesto; valores aceitam PEM público literal, caminho PEM ou `{"public_key": "/private/publisher.pub.pem", "publisher": "example-publisher"}`. Caminhos relativos do CLI são relativos ao arquivo de configuração. A API também aceita objetos `Ed25519PublicKey`. A chave pública pode verificar assinaturas; a privada deve ficar somente com quem está autorizado a publicar. Chaves públicas trazidas pelo próprio pacote nunca são âncoras de confiança. Revogação/rotação exige alterar explicitamente esse arquivo privado. `inspect` sem trust store mostra `"trusted": false`, mesmo quando os hashes são íntegros.

Build gera o inventário completo, excluindo somente o próprio manifesto e caches/artefatos de build conhecidos; recusa symlinks, secrets/private keys, output dentro da app, namespaces reservados, docs incompletos e dependências não inventariadas. O ZIP tem exatamente `manifest.json`, `manifest.sig`, `app/` e wheels opcionais. Assinatura Ed25519 cobre os bytes canônicos exatos do manifesto; SHA-256/tamanhos cobrem todos os payloads. Validação recusa membros extras/duplicados, colisões normalizadas, travessia, links/dispositivos, arquivos/diretórios conflitantes e limites de ZIP (512 MiB comprimido, 1 GiB expandido, 256 MiB por arquivo, 4096 membros, expansão máxima 200:1).

Declare dependências Python em `python_dependencies` como `{"name": "distribution", "version": "1.2.3", "wheel": "wheels/distribution-1.2.3-py3-none-any.whl", "sha256": "<SHA256>", "marker": "python_version >= '3.12'"}`; marker é opcional. Forneça cada wheel com `build --wheel /private/wheelhouse/distribution-1.2.3-py3-none-any.whl`. Build recalcula SHA-256; valida identidade/versão do METADATA e namespace de cada wheel. Não aceita sdists, wheels `.data` ou arquivos que se apropriem de core/config/applications. O ambiente novo instala wheels pelo lock final aprovado, nunca dentro do diretório da app.

Um wheel (`.whl`) é uma distribuição Python pronta para instalação, não a ativação de uma app no tenant. No exemplo de dependência, `name` e `version` identificam a distribuição, `wheel` identifica o arquivo inventariado e `sha256` fixa seus bytes. O marcador opcional limita os ambientes em que a dependência se aplica. Incluir esses arquivos no pacote permite preparar o ambiente aprovado sem buscar versões diferentes durante a instalação; isso não transforma uma dependência em código confiável sem revisão.

Versões de componentes exigem major.minor.patch; versões de distribuições Python e markers seguem PEP 440, inclusive duas partes como `8.2`/`python_version="3.14"`. O wheelhouse completo do ambiente tem limite agregado separado de 65536 membros e 1 GiB expandido; não recebe indevidamente o limite de um artefato isolado. Apps `project-local` continuam em `PROJECT_APPS`; manifesto e caminho carregados precisam coincidir com seu inventário, ou o startup falha fechado. Elas não são sobrescritas por um pacote com nome coincidente.

Extração offline exige raiz nova e separada: `package extract ARTIFACT --trust-store TRUST --destination /staging/applications/tasks --wheels-destination /staging/app-wheels`. Não faz merge/overwrite e revalida o artefato antes da cópia. `extract_package(verified, destination, wheels_destination=...)` retorna os roots criados; copia o contrato canônico e assinatura para a app como metadados gerados pelo instalador. Artefatos core têm apenas wheels e usam `destination=None` na API (omita `--destination` no CLI). O core wheel somente pode possuir `skeleton/` e seu `.dist-info`; atualização não substitui arquivos do projeto derivado.

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

O índice é `{catalog: {...}, signature: "<base64>"}`; assinatura cobre JSON canônico do corpo. Entradas contêm componente, tipo, versão, canal, compatibilidade, referência HTTPS/relativa ao artefato, SHA-256 do ZIP, assinatura do manifesto, publisher, notas e migrations. O formato 2 (`catalog_version: 2`, gerado por `package index`) acrescenta `dependencies` e `conflicts`, que permitem à prévia sugerir dependências; índices no formato 1 continuam válidos, e em ambos o manifesto do artefato baixado é a fonte que vale. Índices expirados, adulterados e gerações abaixo da última confirmada são rejeitados; canais são allowlist do operador. O agente só consulta fontes previamente autorizadas, não URLs fornecidas pelo upload. `versions` distingue metadata da distribuição instalada de checkout (`origin: source`) e manifests locais (`project-local`); um arquivo JSON isolado nunca prova release instalado.

O exemplo pode ser publicado de verdade com `package build --app-root examples/notes --manifest examples/notes/manifest.json --output /private/notes.skapp --private-key /private/publisher.pem`; autorize o publisher `skeleton-examples` no trust store correspondente. Ativação/configuração tenant permanece independente da instalação física global.



## Exemplo completo existente: Note

Ative somente se desejado:

```sh
export SKELETON_ENABLE_EXAMPLE_APP=true
.venv/bin/python manage.py migrate
.venv/bin/python manage.py sync_table_catalog
.venv/bin/python manage.py check_skeleton_schema
```

O exemplo fica em `examples/notes`, app label `example_notes`; pode permanecer desabilitado num projeto real. Não coloque seu módulo de negócio nesse namespace.

### Modelo

O modelo existente usa herança abstrata; metadata, tenant/access fields são colunas da tabela Note, não tabela-pai:

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

`body` pode ser vazio e tem limite de 10000 caracteres. É sensível: usuários autorizados podem ler seu conteúdo na nota, mas a auditoria o substitui por `[REDACTED]`, mantendo a evidência de que mudou. Isso não cifra o conteúdo da tabela de negócio. A restrição de unicidade usa `(organization, code)` apenas entre registros vivos: duas organizações podem usar o mesmo código. A exclusão lógica libera o código para uma nova nota; se isso ocorrer, restaurar a nota antiga retorna conflito, sem sobrescrever a nova.

### Spec allowlist

`NotesConfig.ready` registra:

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

A allowlist é a lista explícita dos campos permitidos. Ela contém somente campos concretos, sem `eval` ou caminhos arbitrários para objetos relacionados. A leitura de owner/grupo retorna UUID, não um perfil ou grupo privado inteiro. `owner`, `visibility` e `owning_group` não entram em `write_fields`, pois mudar acesso exige o endpoint/serviço de `manage_access`. Campos sistêmicos são automaticamente somente leitura. `expose_api=True` disponibiliza a rota, mas não concede permissão: `TablePermission` continua obrigatória. Um código comercial gerado pelo núcleo entra em `sequence_fields=(("code", "<chave da sequência>"),)` e fica fora de `write_fields`: a criação genérica (formulário e API) o preenche com o próximo número ([sequências](sequences.md#campos-preenchidos-pelo-gateway-156)). Para busca textual, declare `search_fields=("code", "title")`, só com campos de texto de `read_fields` e nunca sensíveis (E031). Na migration, instale `InstallSearchIndex("<app>", "<model>", fields=[...], label="<campo do rótulo do picker>")`. A lista passa a aceitar `?q=` e o picker `q` ([#180](https://github.com/alaorneto/skeleton/issues/180), [#181](https://github.com/alaorneto/skeleton/issues/181)). O índice ausente ou divergente reprova o `check_skeleton_schema`.

### Migration de segurança

`examples/notes/migrations/0001_initial.py` cria o modelo e depois instala infraestrutura no mesmo arquivo; depende de `("sk_audit", "0002_record_infrastructure")`. O gerador Django cria campos herdados/constraints/managers históricos; não omita os constraints da base. Operação relevante:

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

Não escreva essa especificação à mão. Depois de `makemigrations` e da revisão do histórico, use o comando do autor:

```bash
python manage.py record_infrastructure <app_label>          # só lista
python manage.py record_infrastructure <app_label> --write  # cria a migration
```

Sem `--write`, o comando compara cada `TableSpec` da app com a última `InstallRecordInfrastructure` encontrada nas migrations em disco (sem consultar o banco) e lista os modelos com `missing` ou com a divergência encontrada (`columns`, `sensitive`, `scope`, `immutable_fields`, `append_only`). Com `--write`, ele cria uma migration **nova** (`NNNN_record_infrastructure.py`) que depende da folha atual da app e de `("sk_audit", "0002_record_infrastructure")` e contém só as operações pendentes. A especificação é gerada por `build_infrastructure_specification(model, spec)` a partir do modelo e do `TableSpec` no momento da autoria e fica congelada no arquivo; ao migrar, o DDL continua usando só essa especificação e o modelo histórico.

O comando nunca altera migrations existentes e não cria arquivo quando nada falta. Ele recusa a escrita se a app tiver mais de uma folha, se ainda não houver migration para o modelo ou se as colunas do modelo histórico na folha forem diferentes das do modelo atual (rode `makemigrations` antes). Também recusa escrever nas apps do núcleo. Revise o arquivo gerado, sobretudo `sensitive`, antes do commit.

Isso vale tanto para app nova quanto para alteração de coluna ou relação auditável: a operação nova vai depois da alteração, com a especificação atualizada. Ela não substitui `CreateModel`. Não use o catálogo editável nem imports do modelo atual em migrations.

## Permissões e fluxo HTML

Bootstrap/sync reconcilia TablePermission de organization_admin para a tabela exposta. Member não recebe grants automaticamente. Em `/o/<org>/access/roles/` crie role e atribua read/create/update/delete/restore e, se desejado, manage_access/audit via table-permissions. Atribua MemberRole ou GroupRole + GroupMember. Tudo exige org admin e, em produção, MFA.

1. Login real em `/accounts/login/`.
2. Dashboard `/o/<org>/`; catálogo `/o/<org>/catalog/` fornece tabela disponível.
3. Lista `/o/<org>/tables/example_notes.note/`.
4. Criar `/o/<org>/tables/example_notes.note/new/` com code/title/body; CSRF, owner=ator, private.
5. Detalhe `.../<sysid>/`; editar `.../<sysid>/edit/`, form com revision escondida.
6. Acesso `.../<sysid>/access/`: owner/visibility/grupo só com manage_access + ownership/admin.
7. Exclusão `.../<sysid>/delete/` e restore `.../<sysid>/restore/` via POST/revisão; GET não apaga. Tombstone não volta à lista viva.
8. Audit `/o/<org>/audit/`: body redigido; contagem/lista autorizadas.

Só botões permitidos aparecem, mas servidor revalida cada ação. Usuário comum que recebe visibilidade organization pode ler, não ganha escrita. Relacionamentos de novos apps exigem PROTECT + guard same-tenant e FK picker autorizado.

## Fluxo API de ponta a ponta

Use cookie/CSRF de login allauth real como em [api](api.md). Variáveis BASE/ORG/SESSION/CSRF são locais privadas. Exemplo com notas:

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

O `"5"` exemplifica a transição exata 4→5, não revisão fixa para qualquer nota. Grants/access podem também mudar revisão; não adivinhe num fluxo concorrente. Recarregue registro após restore. Alterar organização/owner no PATCH normal retorna erro; table_key físico/desconhecido não abre tabela.

## Novas operações de negócio

Implemente a regra de negócio em um serviço da app, não em `skeleton`. O serviço roda dentro de um contexto já aberto com organização e ator confiáveis, obtém `authorized_queryset` para a ação pretendida, valida a regra e grava com `save`/CAS. Em uma view, o contexto é o validado pela superfície tenant; em um job, quem abre o `tenant_context` (uma transação independente por organização) é o job, e os resultados são materializados antes de sair do contexto. Nunca use `all_objects` ou SQL direto como atalho em endpoints. Se a operação interessa a outras apps, publique-a na [API de serviços](#api-de-serviços-da-app) em vez de deixar outra app importar o serviço interno.

Uma referência a outro registro organizacional precisa apontar para a mesma organização e para um alvo vivo que o ator possa ler. Combine validação no serviço, seleção autorizada de FK e infraestrutura de integridade no banco: apenas comparar UUIDs não assegura isso. Relações entre registros próprios usam `PROTECT`, para impedir remoção com referências vivas, e links de negócio são tabelas UUID explícitas, não M2M implícito. Marque campos sensíveis no `TableSpec` e na especificação congelada de migration antes de persistir dados em auditoria.

Use o checklist de [AGENTS](../AGENTS.md) e [testing](testing.md). Mantenha testes de API pública, A/B/default deny, auditoria redigida, concorrência, referências e schema checks. Testes por inspeção de strings de source não provam esses contratos.

## Texto traduzível e outros idiomas

O núcleo entrega só pt-BR, e as msgids do gettext são o próprio texto em português ([#171](https://github.com/alaorneto/skeleton/issues/171), [#172](https://github.com/alaorneto/skeleton/issues/172)). Siga as mesmas regras na app.

- **Python:** dentro de funções, use `gettext("…")`; em constantes de módulo ou de classe (rótulos de campo, `TextChoices`, `TableSpec`, tipos de notificação, `verbose_name`), use `gettext_lazy("…")`. Isso inclui os registros feitos em `ready()`, que rodam na inicialização. Com variáveis, use `gettext("Use %(campo)s.") % {"campo": valor}`, nunca uma f-string dentro do gettext.
- **Templates:** use `{% load i18n %}` e `{% translate "…" %}`; nos argumentos de tags, `_("…")`.
- **O que fica fora do gettext:**
  - a documentação da API: resumos e descrições do schema OpenAPI (`openapi = {...}`, `Operation(...)`), que é contrato para integradores e não muda conforme o idioma de quem pede;
  - texto que o código compara literalmente, como cabeçalhos de documentação (`## Serviços`) e seções de docstring da API de serviços (`Autorização:`);
  - códigos de erro (`code`) e chaves de JSON;
  - logs e eventos de segurança;
  - diagnósticos técnicos em inglês: system checks, manifestos, pacotes e os `PermissionDenied` internos, que as fronteiras já transformam em 403/404 neutros.
- **JSON:** um texto lazy não passa por `json.dumps`. Converta com `str()` antes de serializar fora do `JsonResponse` e do DRF, que já sabem lidar com ele.
- **Validação:** `python tools/i18n_check.py` (exige o GNU gettext) extrai o catálogo e o valida com `msgfmt --check-format`. O teste de pseudolocalização `tests/integration/test_i18n_coverage.py` falha quando sobra texto fora do gettext nas páginas-chave.

Para entregar outro idioma, o projeto derivado:

1. acrescenta o idioma em `LANGUAGES`;
2. cria o catálogo em `LOCALE_PATHS` com `manage.py makemessages -l <idioma>`, traduzindo a partir das msgids em pt-BR;
3. compila com `compilemessages`.

Os emails aceitam também templates por idioma (`body.<idioma>.html`), que têm precedência sobre o gettext.
