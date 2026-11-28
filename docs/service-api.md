# API de serviços entre apps

**Estado: contrato entregue (epic [#112](https://github.com/alaorneto/skeleton/issues/112), presente em todo núcleo publicado): base em `skeleton.service_api`, `runtime.service_api` no manifesto, checks E018/E019, kit de testes `skeleton.testing.service_api` e adoção no exemplo Notes e no gerador. Serviços do núcleo entregues (epic [#113](https://github.com/alaorneto/skeleton/issues/113), métodos `since="0.1.0"` ou posteriores): veja o [catálogo do núcleo](#catálogo-do-núcleo).**

Este documento estabelece o contrato normativo para apps que oferecem operações a outras apps ou consomem serviços do núcleo. O passo a passo prático está no skill `service-api` (`.claude/skills/service-api/`). As regras que a análise estática não consegue provar (veja [Limites da verificação](#limites-da-verificação)) continuam obrigatórias por revisão de código.

## Por que existe

Uma app de agenda precisa notificar participantes; uma app de chamados precisa exibir o nome de quem abriu o registro; uma app de faturamento precisa criar uma tarefa na app de tarefas. Sem um contrato explícito, cada app importaria modelos, managers e funções internas das demais — módulos que mudam sem aviso, expõem instâncias de modelo com campos sensíveis e facilitam contornar a autorização.

A **API de serviços** resolve isso como o contrato em processo entre apps: classes Python com métodos tipados chamadas diretamente por outras apps. Ela cumpre papel distinto da [API REST](api.md):

| | API de serviços | API REST (`/api/v1/`) |
|---|---|---|
| Quem chama | Código de outra app, no mesmo processo | Navegador ou cliente HTTP |
| Transporte | Chamada de método Python | HTTP + JSON |
| Transação | A do chamador (mesmo commit ou rollback) | Uma por requisição |
| Contexto | O `request_context`/`tenant_context` já aberto | Aberto pelo middleware a partir da URL |
| Autorização | Revalidada dentro do serviço | Revalidada pela view, que deve chamar o serviço |

Tanto a API REST quanto as páginas HTML devem atuar como camadas finas sobre a API de serviços da própria app, garantindo um único ponto de aplicação das regras de negócio.

## Termos

- **Provedor:** a app (ou subsistema do núcleo) que publica uma API de serviços.
- **Consumidor:** a app que consome a API de outro provedor.
- **Serviço:** classe derivada de `ServiceAPI`, publicada no módulo `service_api` do provedor.
- **Método público:** método de serviço decorado com `@service_method`; é a única porta de entrada para outras apps.
- **DTO:** objeto imutável de transferência (`@dataclass(frozen=True, slots=True)`) contendo apenas os campos garantidos pelo contrato.

## Obrigações de toda app

1. **Publicar uma API de serviços.** Toda app possui `service_api.py` com pelo menos uma classe de serviço (mesmo que ofereça apenas consultas) e a declara em `runtime.service_api` no manifesto (verificado por `skeleton.E018`; `package build` e `package doctor` recusam releases sem a declaração).
2. **Publicar somente por ela.** Outras apps só podem importar o módulo `service_api` do provedor. Modelos, managers, `services.py` internos, formulários e views não são contrato (verificação estática por `skeleton.E019` e `package doctor`).
3. **Declarar a dependência.** Consumir a API de outra app exige declará-la em `dependencies` no manifesto com faixa de versão, assim como ocorre com migrations e FKs (E014/E015).
4. **Autorizar dentro do serviço.** Cada método aplica `authorized_queryset`, `require_record_action`, capacidades e verificação de ativação da app. Ser chamado por código Python não concede permissão.
5. **Não vazar internos.** Métodos devolvem DTOs, páginas de DTOs ou tipos primitivos — nunca modelos, querysets nem campos de `sensitive_fields`.
6. **Documentar e testar.** Toda app mantém `docs/service-api.md` em seu `documentation_root` e testes de contrato (seções abaixo).
7. **Versionar.** Alterações incompatíveis em métodos públicos exigem incremento *major* da versão da app (veja [Versionamento e depreciação](#versionamento-e-depreciação)).

## Estrutura de arquivos

```text
applications/tasks/
├── service_api.py        # classes de serviço públicas e DTOs (contrato)
├── services.py           # regras internas, usadas pelo service_api e pelas views
├── models.py
├── views.py, api_urls.py # superfícies: chamam o service_api da própria app
├── manifest.json         # runtime.service_api declara módulo e classes
├── docs/
│   ├── spec.md, adr.md   # já exigidos pelo check E012
│   └── service-api.md    # referência da API de serviços
└── tests/test_service_api.py
```

Apps maiores podem transformar `service_api.py` em pacote (`service_api/__init__.py` reexportando as classes), mantendo o mesmo nome de módulo declarado como ponto de importação.

Declaração no manifesto (`manifest_version` 2):

```json
"runtime": {
  "service_api": {
    "module": "applications.tasks.service_api",
    "classes": ["TaskService"]
  }
}
```

- `module` deve permanecer dentro do pacote da app (`applications.<package_name>.…`); caminhos externos são recusados pelo validador.
- `classes` lista nomes simples e únicos (de 1 a 32), definidos no módulo declarado ou em submódulo dele.
- Declarar o campo exige `compatibility.skeleton` com piso mínimo `SERVICE_API_CORE_MINIMUM` (como todo núcleo publicado a partir de 0.1.0 entende o campo, o piso é `0.0.0`, mantendo o mecanismo pronto para releases futuras). `package build` grava esse piso no artefato.
- Manifestos históricos sem o campo continuam carregando com o aviso `skeleton.W018` até a versão do core que o torne obrigatório; `package build` e `package doctor` já exigem o campo, o módulo e `docs/service-api.md`.
- `skeleton.service_api.registry.service_registry()` devolve o mapa `app_id → classes` resolvidas para introspecção e documentação; `/platform/apps/<app_label>/documentation/` exibe o `service-api.md` da app.

## Forma de um serviço e de seus métodos

- A classe herda de `ServiceAPI` (`skeleton.service_api.base`, entregue em [#97](https://github.com/alaorneto/skeleton/issues/97)) e não mantém estado entre chamadas. Consumidores instanciam sem argumentos: `TaskService()`.
- Todo método público:
  - é decorado com `@service_method`;
  - aceita apenas parâmetros **keyword-only** (`def m(self, *, actor, ...)`);
  - recebe o ator explícito em `actor`;
  - traz anotações de tipo completas e docstring contendo as seções **Autorização**, **Erros** e **Efeitos**.
- Nomes refletem o vocabulário de negócio: verbos no imperativo para comandos (`create_task`, `close_task`) e substantivos para consultas (`task`, `tasks`, `open_count`).
- Comandos que alteram registros existentes exigem `expected_revision`; comandos de criação sujeitos a reexecução por jobs aceitam `idempotency_key`.
- Métodos iniciados por `_` são internos, mesmo dentro de `service_api.py`.

```python
@service_method(read_only=True, since="1.0")
def task(self, *, actor: User, task_id: UUID) -> TaskInfo:
    """Devolve uma tarefa legível pelo ator.

    Autorização: TablePermission read em tasks.task e acesso ao registro.
    Erros: NotFound se não existir, estiver em outra organização ou não for legível.
    Efeitos: nenhum.
    """
```

## Contexto e ator

Um serviço **nunca abre** contexto próprio: ele sempre executa dentro do contexto já aberto pelo chamador:

| Chamador | Contexto já aberto | `actor` |
|---|---|---|
| View ou endpoint tenant | `tenant_context` aberto pelo middleware a partir da URL | `request.user` |
| Job `run_as="actor"` ([#77](https://github.com/alaorneto/skeleton/issues/77)) | `tenant_context` aberto pelo worker | ator do job |
| Job `run_as="system"` | `system_context(reason=...)` da organização | `None`, só em métodos `@service_method(allow_system=True)` |
| View do portal | `external_context` da pessoa externa | `request.user`, só em métodos `@service_method(allow_external=True)` |

Em contexto externo, qualquer método sem `allow_external=True` responde `Forbidden(code="external_not_allowed")`. Essa marca aparece em `ServiceMethodSpec.allow_external`, e métodos marcados devem autorizar pela ligação da própria app com a pessoa externa — nunca via `authorized_queryset` ([acesso externo](access-control.md#acesso-externo-portal)).

Dentro de um método `allow_external`, a app pode numerar com sua própria sequência, publicar seu próprio tipo de evento e escrever seu próprio evento gerenciado na agenda: essas superfícies identificam a app chamadora pela cadeia de execução de `service_method` (`skeleton.service_api.base.portal_caller_app`), nunca por argumento, e recusam chamadas diretas vindas de views com `external_not_allowed` ([portal](portal.md#superfícies-do-núcleo-para-os-recursos-da-própria-app-741)).

O decorador valida que `actor` coincide com `current_context().actor` (`skeleton/tenancy/context.py`) e que existe organização quando o método exige tenant. Uma app não consegue atuar em nome de outra pessoa passando outro `actor`, nunca deve chamar `system_context` para elevar privilégios e nunca troca de organização durante a chamada.

## Autorização

- Consultas partem de `authorized_queryset(model, user=actor, action=...)`, filtrando antes de contar ou paginar.
- Comandos sobre um registro executam `require_record_action(record, user=actor, action=...)` após localizar o alvo em um queryset autorizado.
- Operações administrativas verificam capacidades (`require_capability`, de [#17](https://github.com/alaorneto/skeleton/issues/17)) ou delegam a `skeleton.access.services`, nunca inspecionando papéis manualmente.
- Campos de acesso (`owner`, `visibility`, `owning_group` e concessões) só mudam pela gestão de acesso autorizada, igualmente via serviço.
- Registro inexistente, pertencente a outra organização ou ilegível responde sempre `NotFound`, evitando revelar a existência de dados.
- Antes de executar qualquer lógica, o serviço do provedor confirma que sua app está disponível e ativa na organização, respondendo `Unavailable` quando a integração está desligada.
- Com a [cobrança](billing.md#ganchos-de-direito) ativada, a situação de pagamento da organização também é aplicada: em `restricted`, comandos respondem `Unavailable` com código `organization_restricted` (leituras seguem liberadas); em `suspended`, todos os métodos de serviços de apps e os comandos do núcleo respondem `Unavailable` com `organization_suspended`. Chamadas em `system_context` e serviços com `access_exempt = True` (como cobrança) não sofrem esse bloqueio.

## Retorno: DTOs e páginas

- DTOs são `@dataclass(frozen=True, slots=True)` declarados em `service_api.py` (ou `service_api/dto.py`) e documentados como parte do contrato.
- Tipos permitidos em campos: `UUID`, `str`, `int`, `bool`, `Decimal`, `date`, `datetime` com fuso horário, enums de texto, tuplas e outros DTOs. Datas e horas saem em UTC com `tzinfo`; a conversão para o fuso do usuário compete à superfície.
- Apenas campos listados em `read_fields` do `TableSpec` e ausentes de `sensitive_fields` podem integrar um DTO (salvo exceção documentada na seção **Autorização** do método).
- Um serviço de app pode retornar DTOs de uma [tabela derivada](access-control.md#tabelas-derivadas): aplicam-se as mesmas regras de campos, autorizando o filho por `authorized_queryset`/`require_record_action` (e `require_parent_action` na criação), que decidem pela âncora. Valide com [`assert_derived_access_contract`](testing.md#kit-de-tabelas-derivadas).
- Listagens retornam `Page[T]` (`items`, `next_cursor`, `page_size`, máximo de 100 itens), nunca listas ilimitadas. Cursores ou páginas com deslocamento superior a `MAX_OFFSET` (1 000 000 de linhas) respondem `InvalidInput` — consultas profundas devem usar filtros. Exceção: coleções naturalmente pequenas e documentadas no método podem retornar tupla (organizações do ator, grupos do ator, apps instaladas).
- `paginate(rows, convert=..., page_size=..., cursor=..., key=(...))` utiliza paginação por chave (*keyset*) ao receber `key` — tupla com os campos de ordenação (`-` para decrescente), último campo único e nenhum anulável, como `("code", "sysid")` ou `("-created_at", "-record_revision", "sysid")`. Cada página parte após a última linha lida, evitando pulos ou duplicações em mudanças concorrentes e mantendo custo constante em qualquer profundidade. Utilize apenas sobre colunas imutáveis (colunas mutáveis, como `updated_at` da caixa de entrada, seguem no modo por deslocamento). O cursor continua opaco, não concede acesso e mantém compatibilidade com cursores antigos por deslocamento. `OrganizationService.members`/`groups`, `AuditService.history` e `FileService.list` usam paginação por chave.
- Retornar instância de modelo, `QuerySet` ou `dict` mutável viola o contrato: `@service_method` rejeita em tempo de execução e `assert_service_contract` reprova a anotação. A validação em runtime usa a lista fechada do contrato (`None`, `bool`, `int`, `str`, `Decimal`, `date`, `datetime`, `UUID`, enum de texto, tupla e dataclass congelada, incluindo `Page`), recursivamente. Geradores, iteradores, `float`, `bytes`, `frozenset`, objetos comuns, dataclasses mutáveis e enums não textuais levantam `TypeError`.

## Erros

Os erros públicos residem em `skeleton.service_api.errors` ([#97](https://github.com/alaorneto/skeleton/issues/97)); exceções internas nunca atravessam a fronteira:

| Erro | Quando | HTTP correspondente |
|---|---|---|
| `NotFound` | Alvo inexistente, de outra organização ou não legível | 404 |
| `Forbidden` | Ator legível, mas sem permissão para a ação; ator divergente do contexto ou ausente, com código `actor_mismatch` | 403 |
| `InvalidInput(errors)` | Falha de validação de campos; `errors` mapeia campo → mensagens | 400 |
| `Conflict(code)` | Revisão desatualizada (`code="stale_revision"`), unicidade (`integrity_conflict`), vínculos ativos (`live_references`), último admin (`last_administrator`, `last_platform_administrator`) ou cota | 409 |
| `Unavailable` | App provedora desativada, indisponível ou não instalada na organização; ou acesso limitado pela cobrança (`organization_restricted`, `organization_suspended`) | 404 |
| `ContextRequired` | Chamada sem o contexto exigido | 500 (erro de programação) |

Consumidores devem tratar `Unavailable` como "integração desligada", ocultando o recurso opcional sem interromper o fluxo principal.

A classificação de exceções internas (revisão, vínculos, último administrador, validação, integridade e contexto) é centralizada em `skeleton.service_api.internal_errors.classify`, compartilhada por API de serviços, REST e formulários HTML, preservando a apresentação própria de cada superfície (status, `code` e mensagem).

## Transações, efeitos e idempotência

- Todo comando (`read_only=False`) roda dentro de um savepoint da transação do chamador: se levantar erro público, todas as alterações do comando são revertidas e a transação do chamador permanece utilizável.
- O serviço participa da transação do chamador e nunca executa `commit`. Pode usar `transaction.atomic()` para isolar etapas internas em savepoints.
- Efeitos externos (e-mail, HTTP, push) nunca ocorrem dentro do método: são delegados a jobs ou eventos ([#77](https://github.com/alaorneto/skeleton/issues/77), [#78](https://github.com/alaorneto/skeleton/issues/78)) ou agendados em `on_commit` com dados já materializados, garantindo que nenhum efeito saia em caso de rollback.
- Reações a mudanças em outra app devem usar eventos de domínio, nunca chamadas de serviço acionadas por signals.
- Uma `idempotency_key` repetida devolve o resultado original sem duplicar registros.

## Concorrência

Comandos de alteração recebem `expected_revision` e utilizam o mecanismo CAS das bases (`revision`). Revisões desatualizadas levantam `Conflict(code="stale_revision")`. O serviço nunca tenta novamente em silêncio (o que sobrescreveria edições de terceiros): cabe ao chamador reler e decidir como proceder.

## Versionamento e depreciação

- O contrato da API de serviços acompanha a versão da app (`version` no manifesto).
- Remover método, remover ou alterar o tipo de campo de um DTO, ou alterar a semântica de parâmetros ou erros é mudança incompatível (*major*).
- Adicionar método, parâmetro keyword opcional ou campo novo em DTO é mudança compatível (*minor*).
- Antes de remover qualquer item, marque-o com `@deprecated(since=..., removal=...)` e mantenha-o por pelo menos uma versão *minor* antes da *major* de remoção.
- Consumidores declaram a faixa compatível em `dependencies`, por exemplo `{"com.example.tasks": ">=1.2,<2.0"}`.

## Consumir a API de outra app

```python
# applications/billing/services.py
from applications.tasks.service_api import TaskService  # único import permitido da app tasks
from skeleton.service_api.errors import Unavailable


def open_followup_task(invoice, *, actor):
    try:
        return TaskService().create_task(
            actor=actor,
            title=f"Cobrar fatura {invoice.code}",
            idempotency_key=f"invoice-followup:{invoice.pk}",
        )
    except Unavailable:
        return None  # app de tarefas desligada nesta organização
```

O manifesto de `billing` declara `com.example.tasks` em `dependencies`. Não existem dependências opcionais: toda integração que exige o código de outra app precisa declará-la no manifesto e tratar a desativação por organização capturando `Unavailable`.

Apps first-party, como a agenda, são declaradas da mesma maneira — pelo `app_id` com a faixa correspondente à versão do core: `"dependencies": {"org.skeleton.calendar": ">=0.10.0,<0.11.0"}`. Como a agenda e o livro contábil são bibliotecas first-party, a dependência é sempre satisfeita (a biblioteca é instalada junto com a app) e fica automaticamente efetiva nas organizações em que a app está ativa, sem exigir ativação própria — tanto em apps do projeto quanto em pacotes `.skapp` ([extensão](extending.md#apps-first-party-do-núcleo-skeletoncontrib)).

## Superfícies públicas do núcleo para apps

Uma app só pode importar do núcleo os nomes listados abaixo. A coluna "Uso" representa a lista fechada verificada pelo check (`PUBLIC_CORE_NAMES` em `skeleton/system/import_boundary.py`); onde ela menciona "construtores" (OpenAPI e fluxos), qualquer nome público do módulo é aceito. Nomes iniciados por `_` nunca fazem parte do contrato, nem mesmo dentro de `skeleton.service_api`.

| Módulo | Uso |
|---|---|
| `skeleton.service_api` e submódulos | Base, erros, DTOs e serviços do núcleo |
| `skeleton.service_api.insights` | `register_instance_metric`: métricas da app somadas aos números de gestão da instância ([API de gestão](platform-api.md#resumo-e-métricas-skeletoninsights)) |
| `skeleton.common.models` | Bases `SystemRecord`, `OrganizationRecord`, `SecuredRecord` e o `SealableMixin` dos registros selados |
| `skeleton.common.money` | Dinheiro em inteiros da menor unidade: `parse`, `format_amount`, `scale`, `percent`, `exponent`, `validate_currency` e a tabela `CURRENCY_EXPONENTS` |
| `skeleton.common.markdown` | Texto formatado em Markdown ([texto formatado](ui-design.md#texto-formatado-markdown)): `render_html` (HTML já escapado), `to_plain_text` (texto sem marcadores), `validate_features`, a lista `FEATURES`, o limite `MAX_SOURCE_LENGTH` e os erros `MarkdownError` e `MarkdownTooLong`; nos templates, os filtros `markdown` e `markdown_plain` de `{% load skeleton_markdown %}` |
| `skeleton.common.sealing` | `canonical_bytes` e `digest`: o resumo SHA-256 das partes da app que entram no selo (`extra_digest`) |
| `skeleton.records.sealing` | `seal_record`, `seal_payload` e `verify_seal`: selar e verificar os modelos da própria app em processo ([modelo de dados](data-model.md#registros-selados-526)) |
| `skeleton.common.migration_operations` | `InstallRecordInfrastructure` e `InstallSearchIndex`, só em migrations |
| `skeleton.system.app_registry` | `AppManifest`, `register_app` em `ready()` |
| `skeleton.system.registry` | `TableSpec`, `register` em `ready()`, e as políticas `AttachmentPolicy`, `CommentPolicy`, `GlobalSearchPolicy` e `SealPolicy` |
| `skeleton.system.record_operations` | `RecordOperations`, `register_record_operations` |
| `skeleton.system.dashboard_widgets` | `DashboardWidget`, `register_dashboard_widget` ([quadros do painel da organização](extending.md#quadros-do-painel-da-organização)) |
| `skeleton.access.policies` | `authorized_queryset`, `require_record_action`, `require_parent_action` (criar filho de tabela derivada) e o predicado de leitura `has_table_permission`, sobre modelos da própria app |
| `skeleton.access.derivation` | `is_authorizable` e `is_derived`: se uma tabela é autorizada direto ou pelo `access_parent` ([tabelas derivadas](access-control.md#tabelas-derivadas)) |
| `skeleton.tenancy.context` | `current_context` (leitura) e `command_context`, só em `management/commands/` da app: abre o contexto de sistema sem organização de um comando do operador, o único em que uma [tabela global da app](table-catalog.md#tabelas-globais-de-apps) aceita escrita, e é recusado dentro de qualquer contexto já aberto (requisição, tenant, sistema, job, consumidor). `system_context`, `tenant_context` e `request_context` não são permitidos: o contexto é aberto pelo middleware, pelo worker ou pelo kit de testes, nunca pela app |
| `skeleton.web.views` | `tenant_page` |
| `skeleton.web.markdown_widget` | `MarkdownTextarea`: o widget de formulário do texto formatado, com o editor Markdown ([editor](ui-design.md#editor-markdown)) |
| `skeleton.web.vocabulary` | `register_status` e `register_marker` em `ready()`: os estados e marcadores da app, com rótulo e tom, lidos por `status_badge` e `marker` ([vocabulário visual](ui-design.md#estados-e-marcadores-de-apps)) |
| `skeleton.web.confirm` | `Confirmation`, `confirmation_step`: a página de confirmação de uma ação irreversível, com a mesma autorização do POST ([interface](ui-design.md#ações-pela-intenção)) |
| `skeleton.api.views` | `TenantAPI` e `request_fields` (o objeto de campos do corpo) |
| `skeleton.api.openapi` | `Operation` e construtores de schema para o atributo `openapi` das views de API da app ([OpenAPI](api.md#schema-openapi)) |
| `skeleton.jobs.registry`, `skeleton.jobs.periodic` | `JobType`, `register_job_type`, `register_periodic` em `ready()`; enfileirar é pelo `JobService` |
| `skeleton.events.registry` | `EventType`, `register_event_type`, `register_consumer` em `ready()`; publicar é pelo `EventService` |
| `skeleton.notifications.registry` | `NotificationType`, `NotificationField`, `register_notification_type` em `ready()`; notificar é pelo `NotificationService` ([texto do aviso](notifications.md#texto-do-aviso)) |
| `skeleton.sequences.registry` | `SequenceSpec`, `register_sequence` em `ready()`; numerar é pelo `SequenceService` |
| `skeleton.flows.registry` | `FlowSpec` e os construtores de etapas, transições, formulários e condições; `register_flow`, `register_flow_handler`, `register_flow_condition`, `register_flow_assignment` em `ready()`; iniciar e operar é pelo `FlowService` ([fluxos](flows.md)) |
| `skeleton.integrations.tokens.scopes` | `register_token_scope` em `ready()` (escopos nomeados, como o feed da agenda) |
| `skeleton.tenancy.aggregate` | `aggregate_route`, `for_each_organization` (leitura de várias organizações) e `user_context` (contexto global da pessoa na parte sem organização de uma rota agregada; recusa aninhar) |
| `skeleton.system.applications` | `application_state` e `require_application_enabled`, transitórios até o `ApplicationService` substituí-los |
| `skeleton.testing` | Kit de contrato e cenários, só em testes |

Para ler a própria configuração ou verificar se outra app está ativa, use o `ApplicationService` ([#105](https://github.com/alaorneto/skeleton/issues/105)); `skeleton.system.applications.application_state` segue permitido temporariamente nesta versão. Importar outros módulos do núcleo gera o aviso `skeleton.W019` nesta versão e passará a erro numa próxima. A restrição abrange também `from skeleton.common import models` e imports sob `TYPE_CHECKING`; para tipar o parâmetro `actor` sem importar o modelo do núcleo, use `django.contrib.auth.base_user.AbstractBaseUser`.

### Fronteira verificada

O check `skeleton.E019` inspeciona via `ast` — sem importar código — todos os arquivos `.py` de cada app registrada:

- importar de outra app registrada qualquer módulo diferente do `service_api` declarado (ou submódulo dele) é erro, incluindo imports relativos (`from ..tasks.models import Task`) e `from applications.tasks import models`;
- importar o `service_api` de outra app sem declará-la em `dependencies` é erro (análogo a E014/E015 para migrations e FKs);
- `applications.<pacote>` não registrada só permite importar `applications.<pacote>.service_api`;
- módulos fora das superfícies públicas do núcleo geram `skeleton.W019`;
- nomes fora da lista fechada de um módulo público (`from skeleton.tenancy.context import system_context`, `from skeleton.common.models import OperationalRecord`) e nomes privados (`_x`) de qualquer módulo `skeleton.*` geram `skeleton.W019`;
- importar `command_context` fora de `management/commands/` da app (em serviço, view, job, consumidor ou qualquer outro módulo) gera erro `command_only_import` (E019), pois somente comandos de operador podem abrir o contexto de sistema da app; `system_context` permanece fora da lista fechada (W019);
- chamadas a `apps.get_model("sk_...", ...)` com label literal do núcleo fora de `migrations/` geram `skeleton.W019` (em migrations, o modelo histórico via `apps.get_model` é obrigatório e permitido);
- todos os diretórios importáveis são analisados, incluindo `build/`, `dist/` e `node_modules/`; apenas `__pycache__` e diretórios iniciados por `.` são ignorados;
- apps first-party (`skeleton.contrib.<pacote>`) obedecem às mesmas regras: seu `service_api` exige dependência declarada (`org.skeleton.calendar`) e seus módulos internos geram `internal_import` (E019). Quando desconhecidas pelo inventário do `package doctor`, aceitam apenas `service_api`.

`package doctor` repete essa análise antes do build usando o inventário alvo (`--inventory`): violações aparecem como `internal_import`, `undeclared_dependency`, `command_only_import` e `unreadable_source`, enquanto módulos internos do núcleo aparecem como `core_import` com `"severity": "warning"` (sem reprovar o doctor).

## Catálogo do núcleo

O núcleo expõe sua própria API de serviços em submódulos de `skeleton.service_api` (epic [#113](https://github.com/alaorneto/skeleton/issues/113)). Serviços do núcleo não têm `app_label` (não dependem de ativação de app) e obedecem integralmente às regras deste contrato (parâmetros keyword-only, `actor` coerente com o contexto, DTOs imutáveis e erros públicos).

| Serviço | Módulo | Métodos | Issue |
|---|---|---|---|
| `IdentityService` | `skeleton.service_api.identity` | `me`, `get_person`, `people`, `update_my_preferences`, `update_my_profile` | [#102](https://github.com/alaorneto/skeleton/issues/102), [#475](https://github.com/alaorneto/skeleton/issues/475) |
| `OrganizationService` | `skeleton.service_api.organizations` | `current`, `my_organizations`, `members`, `groups`, `is_member`, `member_refs`, `settings`, `update_settings`, `limits` | [#103](https://github.com/alaorneto/skeleton/issues/103), [#167](https://github.com/alaorneto/skeleton/issues/167), [#698](https://github.com/alaorneto/skeleton/issues/698) |
| `ExternalAccessService` | `skeleton.service_api.external` | `invite`, `revoke_invitation`, `resend_invitation`, `accesses`, `invitations`, `access_for`, `revoke`, `me` | [#532](https://github.com/alaorneto/skeleton/issues/532) |
| `AccessService` | `skeleton.service_api.access` | `can`, `require`, `capabilities`, `granted_record_ids`, `my_groups`, `grant_member_table_access`, `grant_record_access`, `revoke_record_access`, `record_grants` | [#104](https://github.com/alaorneto/skeleton/issues/104) |
| `ApplicationService`, `ApplicationAdministrationService` | `skeleton.service_api.applications` | `is_enabled`, `require_enabled`, `my_configuration`; administração: `applications`, `application`, `configure` | [#105](https://github.com/alaorneto/skeleton/issues/105) |
| `AuditService`, `AuditLogService` | `skeleton.service_api.audit` | `history`; log das superfícies do núcleo: `events`, `event`, `related_domain_events` | [#106](https://github.com/alaorneto/skeleton/issues/106), [#110](https://github.com/alaorneto/skeleton/issues/110) |
| `NotificationService` | `skeleton.service_api.notifications` | `notify`, `settings`, `unread_count`, `inbox`, `mark_read`, `archive` | [#107](https://github.com/alaorneto/skeleton/issues/107) |
| `JobService`, `EventService` | `skeleton.service_api.jobs`, `skeleton.service_api.events` | `enqueue`, `publish` | [#108](https://github.com/alaorneto/skeleton/issues/108) |
| `FileService` | `skeleton.service_api.files` | `attach`, `list`, `download_url`, `external_download_url`, `remove` | [#109](https://github.com/alaorneto/skeleton/issues/109) |
| `CommentService` | `skeleton.service_api.comments` | `comments`, `add_comment`, `edit_comment`, `delete_comment`, `restore_comment` | [#187](https://github.com/alaorneto/skeleton/issues/187) |
| `SearchService` | `skeleton.service_api.search` | `search` | [#583](https://github.com/alaorneto/skeleton/issues/583) |
| `SequenceService` | `skeleton.service_api.sequences` | `next_number` | [#155](https://github.com/alaorneto/skeleton/issues/155) |
| `RecordSealService` | `skeleton.service_api.records` | `seal`, `status`, `verify` | [#527](https://github.com/alaorneto/skeleton/issues/527) |
| `FeedTokenService` | `skeleton.service_api.tokens` | `issue`, `issue_for_record` | [#76](https://github.com/alaorneto/skeleton/issues/76) |
| `RemoteInstanceService` | `skeleton.service_api.remote_instances` | `connections`, `instance`, `organizations`, `organization`, `invoices`, `payments`, `metrics` (só em jobs da organização liberada; [API de gestão](platform-api.md#instância-central-conexões-remotas)) | [#712](https://github.com/alaorneto/skeleton/issues/712) |
| `BillingService` | `skeleton.service_api.billing` | `access_state`, `entitlement`, `storage_allowance`, `check_quota`, `record_usage`, `void_usage`, `check_usage`, `usage`, `subscription`, `list_invoices`, `offers`, `contract`, `remove_item`, `cancel`, `resume`, `redeem_voucher`, `update_account`, `checkout` | [#404](https://github.com/alaorneto/skeleton/issues/404) |
| `FlowService` | `skeleton.service_api.flows` | `start`, `signal`, `instance`, `instances`, `history`, `tasks`, `open_task_count`, `task`, `claim_task`, `release_task`, `reassign_task`, `complete_task`, `decide_task`, `cancel`, `suspend`, `resume`, `retry` | [#205](https://github.com/alaorneto/skeleton/issues/205) |

### IdentityService

```python
from skeleton.service_api.identity import IdentityService

me = IdentityService().me(actor=request.user)  # UserProfileInfo
person = IdentityService().get_person(actor=request.user, user_id=assignee_id)  # PersonSummary
names = IdentityService().people(
    actor=request.user, user_ids=(author_id, assignee_id)
)  # tuple[PersonSummary, ...]: só os membros ativos da organização atual, até 200 ids, uma consulta (desde 0.1.0)
IdentityService().update_my_preferences(actor=request.user, expected_revision=me.revision, timezone="America/Sao_Paulo")
```

| Método | Autorização | Erros |
|---|---|---|
| `me(*, actor) -> UserProfileInfo` | o próprio ator; funciona com ou sem organização no contexto | `Forbidden` (ator divergente) |
| `get_person(*, actor, user_id) -> PersonSummary` | vínculo ativo do ator; `email` só com a capacidade `members.view` | `NotFound` se a pessoa não tiver vínculo ativo na organização atual, mesmo que exista |
| `people(*, actor, user_ids) -> tuple[PersonSummary, ...]` | vínculo ativo do ator; `email` só com `members.view`; quem não tem vínculo ativo na organização atual é omitido | `InvalidInput` (mais de 200 IDs ou ID inválido) |
| `update_my_preferences(*, actor, expected_revision, language=None, timezone=None, color_scheme=None) -> UserProfileInfo` | somente o próprio ator; `color_scheme` (desde 0.4.0) aceita `""` (seguir o sistema), `light` ou `dark` | `InvalidInput` (idioma, fuso ou tema inválido; nada a alterar); `Conflict("stale_revision")` |
| `update_my_profile(*, actor, expected_user_revision, expected_revision, changes) -> UserProfileInfo` | somente o próprio ator; `changes` aceita `first_name`, `last_name`, `display_name`, `timezone`, `language` e `color_scheme` (desde 0.4.0) | `InvalidInput` (campo não permitido ou valor inválido); `Conflict("stale_revision")` em qualquer das duas revisões |

`UserProfileInfo` expõe `sysid`, `email`, `display_name`, `language`, `timezone`, `kind`, `revision` (do perfil, para CAS), `first_name`, `last_name`, `user_revision`, `profile_sysid`, `profile_display_name` (valor gravado, sem fallback), `effective_language`, `effective_timezone` (valor efetivo no contexto: perfil → organização → instância) e `color_scheme` (desde 0.4.0: `""`, `light` ou `dark`). `PersonSummary` expõe `sysid`, `display_name`, `email` (ou `None`) e `kind` (`person` ou `service` para contas de serviço). Nenhum DTO expõe senha, MFA, flags de plataforma ou `auth_version`.

### OrganizationService

| Método | Autorização | Erros |
|---|---|---|
| `current(*, actor) -> OrganizationInfo` | vínculo ativo | `ContextRequired` sem organização |
| `my_organizations(*, actor) -> tuple[OrganizationInfo, ...]` | próprio ator, por autodescoberta; apenas organizações ativas com vínculo ativo; funciona sem organização; no endereço próprio de uma organização (`<slug>.<domínio base>`) devolve apenas a organização do host | `Forbidden` (ator divergente) |
| `members(*, actor, query=None, group_id=None, cursor=None, page_size=25) -> Page[PersonSummary]` | vínculo ativo; `email` só com `members.view` | `NotFound` (grupo de outra organização); `InvalidInput` (busca acima de 100 caracteres, cursor inválido ou `page_size` acima de 100) |
| `groups(*, actor, cursor=None, page_size=25) -> Page[GroupSummary]` | vínculo ativo | `InvalidInput` |
| `is_member(*, actor, user_id) -> bool` | vínculo ativo | `InvalidInput` |
| `member_refs(*, actor, user_ids, include_service_accounts=False) -> tuple[MemberRef, ...]` | vínculos ativos da organização entre `user_ids` (`user_id`, `membership_id`, ordenados por `user_id`), apenas pessoas por padrão e sem nome nem e-mail; vínculo ativo do ator, ou `actor=None` em `system_context` (jobs) | `InvalidInput` (identificador inválido ou mais de 500) |
| `settings(*, actor) -> OrganizationSettingsInfo` | vínculo ativo | `ContextRequired` sem organização |
| `update_settings(*, actor, display_name, default_language, default_timezone, brand_color, expected_revision) -> OrganizationSettingsInfo` | administrador da organização | `Forbidden`; `InvalidInput` (idioma fora de `LANGUAGES`, fuso inválido, cor fora de `#RRGGBB`, nome acima de 200 caracteres); `Conflict("stale_revision")` |
| `limits(*, actor) -> tuple[LimitInfo, ...]` (desde 0.6.0) | vínculo ativo com `members.view` ou `members.manage` | `Forbidden` sem a capacidade |

`LimitInfo` traz `metric`, `used`, `limit`, `reserved` (convites pendentes, somente em `members`) e `over_limit`; apenas métricas com limite aparecem ([limites da organização](organization-limits.md)).

`OrganizationSettingsInfo.locked_fields` (desde 0.6.0) lista os campos travados pela plataforma, e `update_settings` responde `Conflict("setting_locked")` a qualquer tentativa de alterá-los. Da mesma forma, `ApplicationState.locked_fields` lista a ativação e os campos de app travados ou geridos pela plataforma (respondendo `Conflict("setting_locked")` em `ApplicationAdministrationService.configure`), enquanto `ApplicationService.my_configuration` já devolve inclusive os campos geridos pela plataforma ([configurações geridas pela plataforma](managed-settings.md)).

As configurações começam em branco com `revision=0` e passam a existir na primeira gravação; valores vazios herdam o padrão da instância, e `has_logo` indica a presença de logo sem revelar a chave de armazenamento. O diretório de membros retorna apenas vínculos ativos de identidades ativas da organização atual, busca por nome de exibição ou nome e omite contas de serviço. A administração de vínculos continua nos serviços de acesso.

### AccessService

| Método | Comportamento | Erros |
|---|---|---|
| `can(*, actor, action, table_key, record_id=None) -> bool` | Equivale a `authorized_queryset(model, user=actor, action=action)` (filtrado pelo registro quando informado); em [tabela derivada](access-control.md#tabelas-derivadas), decide pela âncora com a ação mapeada, e `manage_access` é sempre `False`; tabela desconhecida ou não autorizável, registro inexistente ou de outra organização retornam `False` | `InvalidInput` (ação ou UUID inválidos) |
| `require(*, actor, action, table_key, record_id=None) -> None` | Mesmo comportamento de `can`, mas levantando erro | `NotFound` se ilegível (inclusive de outra organização); `Forbidden` se legível mas sem a ação |
| `capabilities(*, actor) -> tuple[str, ...]` | Capacidades dos papéis internos do ator, ordenadas em tupla (`frozenset` não é admitido pelo contrato) | — |
| `granted_record_ids(*, actor, table_key, record_ids, action="read") -> tuple[UUID, ...]` | Quais de `record_ids` têm concessão de registro viva para o vínculo do ator, um grupo dele ou papel efetivo na organização atual; não dispensa `TablePermission` nem confirma existência (inexistentes e de outra organização são omitidos). Em tabela derivada, retorna as linhas cuja âncora tem a concessão, entre as acessíveis ao ator | `InvalidInput` (tabela não exposta, ação fora de `read`/`update`/`delete`, ID inválido ou mais de 500) |
| `my_groups(*, actor) -> tuple[GroupSummary, ...]` | Grupos vivos do ator na organização | `Forbidden` sem vínculo |
| `grant_member_table_access(*, actor, table_key, actions) -> tuple[str, ...]` | Concede ao papel interno `member` as ações informadas em uma tabela exposta, pelo serviço de concessão da gestão de acesso (lock e auditoria); retorna apenas as ações efetivamente acrescentadas (idempotente). Usado pela ação "acesso básico" da agenda | `Forbidden` sem `access.manage`; `InvalidInput` (tabela não exposta, ação inválida, catálogo ou papel indisponível) |
| `grant_record_access(*, actor, table_key, record_id, action, group_id=None, role_id=None, membership_user_id=None) -> RecordGrantInfo` | Concede `read`, `update` ou `delete` em um registro a exatamente um grupo, papel ou pessoa (pelo `user_id` do vínculo ativo), via gestão de acesso (lock e auditoria); uma concessão viva idêntica é devolvida sem nova gravação | `NotFound` (registro inexistente, ilegível ou de outra organização); `Forbidden` sem `access.manage`; `InvalidInput` (tabela não exposta ou derivada — cujo acesso é gerido na âncora; ação inválida; nenhum ou mais de um destinatário; destinatário inativo ou de outra organização) |
| `revoke_record_access(*, actor, table_key, record_id, action, group_id=None, role_id=None, membership_user_id=None) -> None` | Revoga a concessão daquele destinatário e ação; sem concessão ativa, nada muda | Os mesmos de `grant_record_access` |
| `record_grants(*, actor, table_key, record_id) -> tuple[RecordGrantInfo, ...]` | Concessões vivas de um registro (até 500, da mais antiga à mais nova), para telas de compartilhamento; exige leitura do registro e `access.manage` | `NotFound` (registro inexistente, ilegível ou de outra organização); `Forbidden` sem `access.manage`; `InvalidInput` (tabela não exposta ou derivada) |

`RecordGrantInfo` contém `sysid`, `table_key`, `record_id`, `action`, `group_id`, `role_id` e `membership_user_id`. Não existe cache entre chamadas: qualquer revogação surte efeito imediato. A administração completa de ACL permanece nos serviços de gestão de acesso.

### ApplicationService

| Método | Comportamento | Erros |
|---|---|---|
| `is_enabled(*, actor, app_id) -> bool` | Aceita `app_id` ou `app_label`; retorna `True` se a app estiver instalada, disponibilizada pela plataforma e ativa na organização com suas dependências; app desconhecida retorna `False` | `InvalidInput` (vazio) |
| `require_enabled(*, actor, app_id) -> None` | Mesmo critério de `is_enabled`, mas levanta o erro padronizado | `Unavailable` |
| `my_configuration(*, actor, app_label) -> AppConfiguration` | Configuração da organização para a **própria** app, com todos os campos declarados resolvidos (`AppConfiguration.get(nome)`) | `Forbidden` se o módulo chamador não pertencer a `app_label`; `Unavailable` se a app estiver desativada ou indisponível |

A app chamadora é identificada pelo primeiro frame da pilha fora da API de serviços, do kit de testes e do Django (`apps.get_containing_app_config`), impedindo que uma app leia a configuração de outra. Esse mecanismo assume código de apps confiável no mesmo processo e previne enganos acidentais, não ataques de falsificação de frames. Ativar e configurar apps permanece restrito ao administrador.

### AuditService

`history(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[AuditEntry]` devolve o histórico de um registro (do mais recente ao mais antigo), seguindo `authorized_audit_queryset`: administrador da organização ou `TablePermission audit` combinada à leitura do registro (ou restauração autorizada, se excluído). Em [tabelas derivadas](access-control.md#tabelas-derivadas), exige `audit` na tabela âncora e leitura do registro âncora. A filtragem ocorre antes da paginação; falta de permissão, tabela desconhecida ou registro de outra organização retornam `NotFound`.

`AuditEntry` contém `sysid`, `occurred_at` (UTC), `operation`, `record_revision`, `actor_id`, `request_id`, `source` e `changes` — tupla de `FieldChange(name, old, new, redacted)` restrita aos `read_fields` da tabela, com campos sensíveis substituídos por `[REDACTED]` e valores não escalares serializados como texto JSON. A auditoria global da plataforma não passa por este serviço.

`AuditLogService` atende as telas e endpoints de auditoria do núcleo (`/o/<org>/audit/`, `/api/v1/o/<org>/audit/` e `/platform/audit/`):

- `events(*, actor, model_label=None, record_id=None, since=None, until=None, actor_id=None, operation=None, request_id=None, page=1, page_size=25) -> AuditLogPage` devolve página numerada (`items`, `count`, `page`, `page_size`) filtrada via `authorized_audit_queryset` antes da contagem. Filtros de período (`since` inclusivo, `until` exclusivo), ator, operação e requisição apenas restringem o que já é autorizado ([#184](https://github.com/alaorneto/skeleton/issues/184)).
- `event(*, actor, event_id) -> AuditLogEvent` devolve um único evento ou `NotFound`. `AuditLogEvent` inclui os campos publicados na REST (`table_name`, `db_principal` e `changes_json` com as colunas visíveis); `table_name` e `db_principal` (metadados de infraestrutura) vêm vazios para quem não administra a organização nem a plataforma. Sem organização no contexto, `events` e `event` leem a auditoria global e exigem administrador da plataforma (`Forbidden` para demais).
- `related_domain_events(*, actor, request_id) -> tuple[DomainEventSummary, ...]` devolve (somente ao administrador da organização) os eventos de domínio ainda retidos da mesma requisição (tipo, tabela, registro, revisão e momento, nunca `data`).

Em apps, prefira sempre `AuditService.history`, que já valida o acesso ao registro específico.

### NotificationService

```python
from skeleton.service_api.notifications import NotificationService, Recipients

NotificationService().notify(
    actor=request.user,
    type_code="tasks.assigned",
    recipients=Recipients(users=(assignee_id,)),
    payload=(("message", "Nova tarefa"),),
    target_table_key="tasks.task",
    target_id=task_id,
)
```

| Método | Autorização | Erros |
|---|---|---|
| `notify(*, actor, type_code, recipients, payload=(), target_table_key=None, target_id=None, dedupe_key=None) -> NotificationReceipt` | a app chamadora precisa ser dona do tipo (`owner_app`); o ator precisa ler o alvo (em tabela derivada, pela âncora); cada destinatário é reautorizado no fan-out; `actor=None` só em `system_context` (consumidores de eventos) | `Forbidden` (tipo de outra app), `NotFound` (alvo ilegível ou de outra organização), `InvalidInput` |
| `settings(*, actor) -> tuple[NotificationSetting, ...]` (desde 0.7.0) | vínculo ativo; retorna apenas os tipos da app chamadora com `enabled`, `reason` (`mandatory`, `instance_disabled`, `organization_disabled` ou vazio) e `text_source` (`organization`, `instance`, `type`), [#785](https://github.com/alaorneto/skeleton/issues/785) | — |
| `unread_count(*, actor) -> int` | linhas do ator em organizações com vínculo ativo | — |
| `inbox(*, actor, cursor=None, include_archived=False, page_size=25) -> Page[NotificationItem]` | linhas do ator na organização atual (ou em todas as organizações ativas sem tenant); cada item traz `title` e `body` renderizados no idioma do ator no momento do envio (linhas antigas exibem o rótulo) | `InvalidInput` |
| `mark_read(*, actor, ids, read=True) -> int`, `archive(*, actor, ids) -> int` | linhas do ator no tenant atual; idempotentes | `NotFound` (linha de outra pessoa), `InvalidInput` |

`Recipients(users, groups, roles, organization)` é o DTO público de destinatários. Cada app registra seus tipos em `ready()` ([notificações](notifications.md)). Um rollback do chamador não deixa notificações nem jobs pendentes; canais e preferências permanecem internos ao subsistema.

### JobService e EventService

| Método | Autorização | Erros |
|---|---|---|
| `JobService.enqueue(*, actor, job_type, payload=(), idempotency_key=None, run_after=None) -> JobReceipt` | o tipo pertence à app chamadora (`JobType.owner_app`); roda na organização do contexto; `actor=None` permitido em `system_context` | `Forbidden` (tipo de outra app), `InvalidInput` (tipo desconhecido, payload inválido ou `run_after` sem fuso) |
| `EventService.publish(*, actor, event_type, record_id, data=()) -> EventReceipt` | o tipo pertence à app chamadora (`EventType.owner_app`); o ator lê o registro (em tabela derivada, pela âncora); no portal (desde 0.6.0), permitido apenas a partir de um método `allow_external` da app dona, bastando que o registro exista na organização | `Forbidden` (tipo alheio; no portal fora de método `allow_external` de app, `external_not_allowed`), `NotFound` (registro ilegível ou de outra organização), `InvalidInput` |

Tipos registrados sem `owner_app` não podem ser acionados via serviço e respondem `Forbidden` a qualquer chamador — regra que também vale para `NotificationService.notify` e `SequenceService.next_number`.

`JobReceipt` traz `sysid`, `job_type`, `state`, `run_after` e `created` (`False` quando a chave de idempotência já existia); `EventReceipt` traz `sysid`, `event_type`, `record_id` e `occurred_at`. Nenhum recibo devolve o payload. Ambos gravam na transação do chamador (rollback cancela job e evento). Tipos e consumidores são registrados em `ready()` ([jobs](jobs.md), [eventos](events.md)). A `idempotency_key` tem escopo por tipo de job e organização: repetida na mesma organização devolve o job existente; usada por outra organização é recusada sem expor o job alheio ([jobs](jobs.md#enfileirar)).

### FeedTokenService

| Método | Autorização | Erros |
|---|---|---|
| `FeedTokenService.issue(*, actor, name, scope, expires_at) -> IssuedFeedToken` | a própria pessoa (exclui contas de serviço) respeitando a política de MFA; `scope` deve ser escopo de feed registrado (`register_token_scope(..., feed=True)`) | `InvalidInput` (escopo não de feed, nome ou validade inválidos), `Forbidden` |
| `FeedTokenService.issue_for_record(*, actor, name, scope, expires_at, table_key, record_id) -> IssuedFeedToken` | mesmas regras de `issue`, dentro de uma organização, para um registro legível pelo ator (`AccessService.can`); vincula o token ao registro e à sua organização | `NotFound` (registro inexistente, ilegível ou de outra organização), `InvalidInput`, `Forbidden` |

`IssuedFeedToken` traz `sysid`, `prefix`, `scope`, `expires_at` e `text` (exibido apenas nesta resposta). Na view de feed, `skeleton.service_api.tokens.authenticate_feed(request, text, scope=...)` retorna o usuário de um token de feed vivo com aquele escopo ou `None` (incluindo excesso de taxa), cabendo à view responder 404. Tokens de feed nunca autenticam `/api/v1/` ([#76](https://github.com/alaorneto/skeleton/issues/76)). Note que `authenticate_feed(request, text, *, scope)` é função auxiliar para views de feed, não um método `@service_method`.

Tokens emitidos por `issue_for_record` abrem exclusivamente o feed daquele registro ([#669](https://github.com/alaorneto/skeleton/issues/669)): `authenticate_feed` os recusa, e `authenticate_record_feed(request, text, *, scope, table_key)` devolve `RecordFeed(user, organization_id, table_key, record_id)` ou `None`, sob as mesmas recusas e limites. Como o token não carrega permissão própria, a view abre somente a organização do registro (`for_each_organization(..., organization_id=...)`), revalida a autorização a cada pedido e responde 404 caso o ator perca leitura, sem revogar o token.

### FileService

| Método | Autorização | Erros |
|---|---|---|
| `attach(*, actor, table_key, record_id, filename, stream, content_type=None) -> AttachmentInfo` | `update` no registro alvo; o tipo real é validado pelo conteúdo (`content_type` é apenas indicativo) | `NotFound` (tabela sem opt-in, alvo inexistente, de outra organização ou ilegível); `Forbidden` (lê sem `update`); `InvalidInput` (tipo, nome ou tamanho); `Conflict("quota_exceeded")` |
| `list(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[AttachmentInfo]` | leitura do alvo | `NotFound`, `InvalidInput` |
| `download_url(*, actor, attachment_id) -> str` | leitura do alvo e anexo `available`; devolve URL interna cuja view reautoriza a cada requisição | `NotFound` (inclusive pendente ou em quarentena) |
| `external_download_url(*, actor, attachment_id) -> str` | apenas no portal (`allow_external=True`): o `external_reader` de `AttachmentPolicy` decide pelo registro alvo vivo | `NotFound`; `Forbidden` fora do portal |
| `remove(*, actor, attachment_id, expected_revision) -> None` | `update` no alvo | `NotFound`, `Forbidden`, `Conflict("stale_revision")` |

Apps nunca acessam o storage diretamente. O alvo pode ser uma tabela exposta ou [derivada](access-control.md#tabelas-derivadas): em filhos, listar e baixar exigem leitura na âncora, enquanto anexar e remover exigem `update` na âncora. A URL gerada por `download_url` também funciona para filhos (reautorizando a cada pedido), mas a tela genérica de registro e as rotas genéricas de upload continuam restritas a tabelas expostas.

### CommentService

Gerencia [comentários](comments.md) em registros de tabelas que declaram `CommentPolicy`, autorizados pelo registro alvo assim como no `FileService` — incluindo [tabelas derivadas](access-control.md#tabelas-derivadas). Todos os métodos têm `since="0.1.0"`:

- `comments(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[CommentInfo]` lista comentários vivos do mais antigo ao mais novo;
- `add_comment(*, actor, table_key, record_id, body)` exige leitura do alvo;
- `edit_comment(..., comment_id, body, expected_revision)` é restrito ao autor;
- `delete_comment` e `restore_comment(..., comment_id, expected_revision)` aceitam autor ou administrador da organização.

`CommentInfo` devolve o corpo em texto simples, que deve ser renderizado escapado (`skeleton.comments.text.linkify` aplica escape e gera links). Erros possíveis: `NotFound` (tabela sem política ou alvo inexistente, de outra organização ou ilegível), `Forbidden` (sem direito de editar ou excluir), `Conflict("stale_revision")` e `InvalidInput` (corpo vazio, acima de 5 000 caracteres, com caracteres de controle ou menção a quem não é membro ou não lê o registro; veja [menções](comments.md#menções)).

### SearchService

Expõe a [busca global](extending.md#busca-global) sobre tabelas marcadas com `GlobalSearchPolicy` para apps que queiram integrá-la às próprias telas (a API REST e a página web chamam o mesmo gateway). Método com `since="0.2.0"`.

```python
from skeleton.service_api.search import SearchService

results = SearchService().search(actor=request.user, query="contr", limit=5)  # SearchResults
for group in results.groups:  # Um SearchGroup por tabela com resultado.
    print(group.label, [hit.label for hit in group.hits], group.has_more)
```

| Método | Autorização | Erros |
|---|---|---|
| `search(*, actor, query, limit=5) -> SearchResults` | leitura de cada tabela (`TablePermission`, escopos do token e app ativa) e de cada registro (`authorized_queryset`); tabelas ilegíveis não geram grupo, assim como tabelas sem resultados | `InvalidInput` (`query` fora de 2 a 200 caracteres ou não textual, `limit` fora de 1 a 20 ou não inteiro); `Forbidden` (ator divergente); `ContextRequired` (sem organização no contexto) |

`SearchResults` traz `query` (termo normalizado com espaços colapsados) e `groups`; cada `SearchGroup` traz `table_key`, `label` (rótulo da tabela), `hits` e `has_more` (indica haver mais resultados além de `limit`); cada `SearchHit` traz `sysid` e `label` (o campo de título do registro, nunca `__str__`). A operação é puramente de leitura, sem efeitos, commit ou contexto próprio.

### SequenceService

| Método | Autorização | Erros |
|---|---|---|
| `next_number(*, actor, key, at=None) -> SequenceNumber` | a app chamadora é dona da sequência (`owner_app`); numera na organização do contexto; `actor=None` só em `system_context` com organização (consumidores e jobs, desde 0.2.0); no portal (desde 0.6.0), só a partir de método `allow_external` da app dona, com a pessoa externa como ator | `Forbidden` (sequência de outra app, `actor=None` fora de `system_context` ou, no portal fora de método `allow_external` de app, `external_not_allowed`); `ContextRequired` (sem organização); `InvalidInput` (sequência não declarada ou data sem fuso) |

`SequenceNumber` contém `key`, `period`, `number` e `formatted`. O incremento ocorre dentro da transação do chamador: em caso de rollback o número é devolvido, evitando lacunas ([sequências](sequences.md)).

### RecordSealService

| Método | Autorização | Erros |
|---|---|---|
| `seal(*, actor, table_key, record_id, expected_revision, extra_digest="") -> SealInfo` | `update` no registro (tabela exposta ou derivada, pela âncora) | `NotFound` (inexistente, de outra organização ou ilegível); `Forbidden` (lê sem `update`); `Conflict("record_sealed")`; `Conflict("stale_revision")`; `InvalidInput` (tabela sem `TableSpec.seal`, revisão ou `extra_digest` inválidos) |
| `status(*, actor, table_key, record_id) -> SealInfo` | leitura do registro | `NotFound`; `InvalidInput` |
| `verify(*, actor, table_key, record_id, extra_digest="") -> SealVerification` | leitura do registro | `NotFound`; `InvalidInput` |

`SealInfo` contém `sysid`, `sealed`, `sealed_at`, `sealed_by`, `digest` e `revision`; `SealVerification` contém `sysid`, `valid`, `digest` (gravado) e `computed` (recalculado). Selar é sempre ato explícito de negócio da app, sem endpoint genérico. O resumo cobre tabela, registro, organização, colunas de `seal_fields`, anexos vivos (quando congelados pela política) e `extra_digest` (SHA-256 calculado pela app sobre suas partes complementares com `skeleton.common.sealing.digest`, que deve ser repassado em `verify`). O núcleo não assina documentos: entrega o resumo para apps de assinatura digital. Para selar os próprios modelos em processo, a app usa `skeleton.records.sealing.seal_record` ([modelo de dados](data-model.md#registros-selados-526)).

### BillingService

Gerencia a [cobrança](billing.md) da organização. Apps consultam situação de acesso, direitos e cotas, enquanto as telas e a API do núcleo usam os métodos de contratação e faturamento. O serviço possui `access_exempt = True`, permanecendo acessível sob restrição de pagamento para permitir a regularização da conta. Métodos com `since="0.1.0"`.

| Método | Autorização | Erros |
|---|---|---|
| `access_state(*, actor) -> AccessState` | qualquer membro, ou `system_context` | — |
| `entitlement(*, actor, app_label) -> bool` | qualquer membro, ou `system_context`; apps sem preço têm direito garantido | — |
| `storage_allowance(*, actor) -> StorageAllowance` | qualquer membro, ou `system_context` | — |
| `check_quota(*, actor, metric, increment=1) -> None` | qualquer membro, ou `system_context` | `Conflict("quota_exceeded")` acima do limite; `InvalidInput` para métrica não registrada |
| `record_usage(*, actor, meter, idempotency_key, quantity=1, occurred_at=None) -> UsageReceipt` | qualquer membro, ou `system_context`; a app do prefixo do medidor precisa estar ativa | `Conflict("quota_exceeded")`; `Conflict("usage_voided")`; `Unavailable`; `InvalidInput` |
| `void_usage(*, actor, meter, idempotency_key) -> UsageReceipt` | idem | `NotFound` (chave desconhecida); `Conflict("usage_period_closed")`; `Unavailable`; `InvalidInput` |
| `check_usage(*, actor, meter, quantity=1) -> UsageStatus` | idem; não grava | `Conflict("quota_exceeded")`; `Unavailable`; `InvalidInput` |
| `usage(*, actor) -> tuple[UsageStatus, ...]` | qualquer membro, ou `system_context`; medidores das apps ativas | — |
| `subscription(*, actor) -> SubscriptionSummary \| None` | `billing.view` | `NotFound` sem a capacidade |
| `list_invoices(*, actor, page_size=20, cursor=None) -> Page[InvoiceSummary]` | `billing.view` | `NotFound` sem a capacidade |
| `offers(*, actor) -> tuple[PriceOffer, ...]` | `billing.view`; preços públicos ativos na moeda e no ciclo da organização | `NotFound` sem a capacidade |
| `contract(*, actor, price_id, terms_version="") -> SubscriptionSummary` | `billing.manage`; planos iniciam ou trocam a assinatura, apps ou pacotes entram como item | `NotFound` (preço inexistente ou não público); `InvalidInput` (ciclo incompatível, app já contratada); `Conflict("organization_restricted")` sob restrição |
| `remove_item(*, actor, item_id)`, `cancel(*, actor, at_period_end=True)`, `resume(*, actor)` | `billing.manage` | `NotFound`; `InvalidInput` |
| `redeem_voucher(*, actor, code)` | `billing.manage` | `InvalidInput` com o motivo (`invalid_voucher`, `already_redeemed`, `not_trialing`, `too_many_attempts`) |
| `update_account(*, actor, legal_name, billing_email, tax_id, country) -> None` | `billing.manage` | `InvalidInput` |
| `checkout(*, actor, invoice_id, return_url) -> CheckoutLink` | `billing.manage` | `NotFound` (fatura de outra organização); `InvalidInput` (fatura não aberta ou sem provedor) |

**Medidores de uso.** O módulo exporta `register_usage_meter(code, *, label, unit_label)` para apps declararem consumo por período em `ready()` (`<app_label>.<nome>`), além de `registered_usage_meters()`. `UsageReceipt` traz `meter`, `metered` (`False` sem cobrança ou assinatura ativa), `created` (`False` em chave repetida ou anulação), `used`, `included`, `limit` (`None` para ilimitado) e `overage` (`block`, `bill` ou `unlimited`). `UsageStatus` traz `meter`, `label`, `unit_label`, `used`, `included`, `limit`, `overage`, `period_start` e `period_end`. Consulte os termos de preço em [cobrança](billing.md#medidores-de-uso).

`AccessState` traz `level` e `since`; `StorageAllowance` traz `quota_bytes` (`None` quando ilimitada), `used_bytes` e `overage` (`block` ou `bill`). Para declarar métricas de estado limitáveis por preço sem importar `skeleton.billing`, a app chama `skeleton.service_api.billing.register_quota_metric("<app_label>.<nome>", contador)` em `ready()` (o livro contábil registra `skeleton_ledger.books`). Os DTOs de assinatura, item, fatura e oferta trazem apenas identificadores, valores na menor unidade monetária e datas — nunca dados de contato da conta.

### FlowService

Opera os [fluxos de processo](flows.md) registrados por apps via `skeleton.flows.registry`. Embora seja serviço do núcleo (sem `app_label`), toda operação exige que a **app dona do fluxo** esteja ativa na organização (respondendo `Unavailable` caso contrário), e listagens omitem fluxos de apps desativadas. Em `instance`, `history` e `task`, a visibilidade é verificada antes da disponibilidade da app, garantindo que atores sem acesso recebam `NotFound` mesmo quando a app está desativada (de modo semelhante, em `ExternalAccessService`, `revoke_invitation`, `resend_invitation` e `revoke` autorizam o chamador antes de buscar o alvo, respondendo igualmente para alvos inexistentes ou de outra app). Métodos com `since="0.1.0"`.

```python
from skeleton.service_api.flows import FlowService

receipt = FlowService().start(
    actor=request.user,
    flow_key="project_purchases.approval",
    subject_id=purchase.pk,
    data=(("amount", "1200.00"),),
    idempotency_key=f"purchase:{purchase.pk}",
)
inbox = FlowService().tasks(actor=request.user, scope="mine")
FlowService().decide_task(actor=request.user, task_id=task.sysid, expected_revision=task.revision, decision="approved")
```

| Método | Autorização | Erros |
|---|---|---|
| `start(*, actor, flow_key, subject_id, data=(), idempotency_key=None) -> StartReceipt` | a app chamadora é dona do fluxo; o ator lê o registro de negócio e possui a ação de início (`update` por padrão); `actor=None` em `system_context` | `Forbidden` (fluxo de outra app ou sem a ação), `NotFound` (registro inexistente, de outra organização ou ilegível), `InvalidInput`, `Conflict("already_running" \| "definition_changed" \| "idempotency_mismatch" \| "flow_failed")` |
| `signal(*, actor, instance_id, name, data=()) -> SignalReceipt` | a app chamadora é dona; o ator lê a instância; `actor=None` em `system_context` | `Forbidden`, `NotFound`, `InvalidInput`, `Conflict("instance_suspended")` |
| `instance(*, actor, instance_id) -> InstanceInfo`, `history(*, actor, instance_id, cursor=None, page_size=25) -> Page[ActivityEntry]` | quem lê o registro de negócio, quem iniciou, participantes das tarefas ou administrador da organização | `NotFound` |
| `instances(*, actor, flow_key=None, subject_id=None, status=None, cursor=None, page_size=25) -> Page[InstanceInfo]` | mesma regra, filtrada antes de paginar | `InvalidInput` |
| `tasks(*, actor, scope="mine", status=None, cursor=None, page_size=25) -> Page[TaskSummary]`, `open_task_count(*, actor) -> int` | `mine` e `available` (filas): apenas o que o ator pode executar agora; `all`: tudo o que ele pode ler | `InvalidInput` |
| `task(*, actor, task_id) -> TaskDetail` | responsável, candidatos da fila, participantes anteriores ou administrador; campos sensíveis do formulário saem apenas para quem pode agir (`can_act`) | `NotFound` |
| `claim_task`, `release_task`, `reassign_task(…, to_user_id, comment="")`, `complete_task(…, action=None, data=(), comment="")`, `decide_task(…, decision, data=(), comment="")`, todos com `task_id` e `expected_revision`, devolvem `TaskSummary` | responsável ou candidato da fila; reatribuir exige responsável de etapa delegável ou administrador | `NotFound`, `Forbidden`, `InvalidInput`, `Conflict("stale_revision")`, `Conflict` de estado (`instance_suspended`, `instance_closed`, `task_closed`, `not_a_queue_task`, `not_claimed`, `not_a_task`, `not_an_approval`, `flow_failed`) |
| `cancel(…, reason="")`, `suspend(…, reason="")`, `resume`, `retry`, todos com `instance_id` e `expected_revision`, devolvem `InstanceInfo` | administrador da organização; `cancel` também por quem iniciou, se o fluxo permitir | `NotFound`, `Forbidden`, `Conflict("stale_revision" \| "instance_closed" \| "not_running" \| "not_suspended" \| "not_failed")` |

- A ordem de verificação é fixa: visibilidade (`NotFound`) → permissão para agir (`Forbidden`) → revisão (`Conflict("stale_revision")`) → estado da tarefa e da instância. Quem perde a disputa em uma tarefa de fila recebe conflito (não `NotFound`), pois a tarefa encerrada permanece legível à fila.
- Dados de formulário entram como tupla de pares `(campo, valor)`, aceitando somente campos declarados na etapa e na transição selecionada.
- `InstanceInfo.data` e `ActivityEntry` nunca incluem campos sensíveis. Comentários de etapas aparecem no histórico para quem lê a instância e são redigidos apenas na auditoria.
- Todas as operações rodam na transação do chamador: em rollback, nenhuma instância, tarefa, job, evento ou notificação persiste.

### Superfícies do núcleo sobre os serviços

A REST e as páginas HTML do núcleo atuam como camadas finas sobre estes serviços ([#110](https://github.com/alaorneto/skeleton/issues/110)), preservando exatamente o comportamento observável:

- `GET/PATCH /api/v1/me/` e a página de perfil usam `IdentityService`;
- `GET /api/v1/organizations/`, a página inicial e o painel da organização usam `OrganizationService`;
- `GET/PUT /api/v1/o/<org>/apps/...` e `/o/<org>/apps/...` usam `ApplicationAdministrationService`, exibindo no formulário a mensagem específica do serviço (dependência, disponibilidade ou valor) e respeitando `ApplicationState.configuration_valid` (padrão `True`, indicando se a configuração gravada ainda cabe no manifesto);
- `/o/<org>/audit/`, `/api/v1/o/<org>/audit/` (incluindo filtro por registro) e `/platform/audit/` usam `AuditLogService`, mantendo a paginação numerada e os campos publicados.

Todos os erros de serviço passam por um único mapeamento HTTP (`skeleton.api.errors.service_error_response`), e recursos com `If-Match` usam `if_match_precondition()` para manter 412 `revision_conflict` em revisão desatualizada. Os recursos administrativos de acesso (papéis, grupos, concessões, convites e vínculos) e o gateway genérico de tabelas seguem diretamente sobre `skeleton.access.services` e as policies; o núcleo não expõe página própria de diretório de membros (atendendo apps via `OrganizationService.members`). A cota de armazenamento por organização vem de `StorageQuota` definida pela plataforma ou de `SKELETON_FILES_ORG_QUOTA_BYTES` (desligada por padrão), com reserva sob lock ([#61](https://github.com/alaorneto/skeleton/issues/61)); veja [arquivos e anexos](files.md).

A app de agenda é a implementação de referência de app first-party com `CalendarService` ([#111](https://github.com/alaorneto/skeleton/issues/111)).

## Testes obrigatórios

Para cada método público, comprove nos testes:

- **Caminho autorizado:** retorno válido em DTO, sem campos sensíveis.
- **Default deny:** ator sem `TablePermission` ou sem acesso ao registro recebe `NotFound` (leitura) ou `Forbidden` (ação).
- **Organizações A/B:** nenhum dado de B aparece em retorno, contagem ou mensagem de erro quando o contexto é A.
- **App desativada ou indisponível:** levanta `Unavailable`.
- **Ator divergente do contexto:** levanta `Forbidden`.
- **Comandos:**
  - CAS com duas chamadas na mesma revisão: uma vence e a outra recebe `Conflict`;
  - `idempotency_key` repetida não duplica;
  - rollback do chamador não deixa efeitos colaterais nem eventos.
- **Contrato:** `assert_service_contract(ServiceClass)` do kit.

Execute os testes em PostgreSQL com a role runtime, conforme o [AGENTS.md](../AGENTS.md), e registre na [evidência de testes](testing-evidence.md) somente o que foi realmente executado.

### Kit de testes de contrato

O módulo `skeleton.testing.service_api` é distribuído no wheel do core, levanta `AssertionError` e funciona com `pytest` ou `unittest`:

| Helper | Prova |
|---|---|
| `assert_service_contract(Service)` | Estrutura do E018 somada a anotações de retorno restritas a DTO congelado, `Page[T]`, tupla, enum de texto ou primitivo (nunca modelo, `QuerySet`, `dict`, `list`, `Any` ou `float`), parâmetros anotados (exceto `actor`) e docstring com `Autorização:`, `Erros:` e `Efeitos:` |
| `build_service_scenario(Model)` | Cria organizações A/B, `member` com `TablePermission` completa em ambas, `denied` em A sem permissão, `outsider` sem vínculo e app ativa nas duas. Identidades, organizações, papéis e vínculos usam os serviços públicos via `skeleton.testing.people` ([testes](testing.md#pessoas-organizações-e-grupos-em-fixtures)); permissões, ativação e registros são preparados em `system_context`. `scenario.password` fornece a senha aleatória por processo para testes com login |
| `scenario.caller(fn, user=..., organization=...)` | Executa `fn(actor)` em contexto próprio (contextos não aninham nem trocam de ator) |
| `assert_tenant_isolated(call, foreign=[...], allowed_errors=(NotFound,), expected_count=None)` | Garante que nenhum UUID, código ou título de B apareça em retorno, página ou erro (`foreign` vazio levanta `TypeError`; erro fora de `allowed_errors` reprova; contagem só é verificada com `expected_count`) |
| `assert_denied_as_not_found(call, missing=...)` | Prova que acesso negado devolve `NotFound` idêntico ao de um alvo inexistente |
| `assert_unavailable_when_disabled(call, disabled=scenario.application_disabled)` | Comprova `Unavailable` com a app desativada após validar uma chamada de controle com a app ativa (outro erro de serviço é aceito no controle por provar passagem pela checagem de disponibilidade) |
| `assert_rejects_other_actor(call)` | Comprova `Forbidden(code="actor_mismatch")` para ator diferente do contexto (ou `None` fora de `system_context`); passe como "outro ator" uma pessoa plenamente autorizada |
| `assert_cas_conflict(first, second, reread=None)` | Comprova que, em duas escritas sobre a mesma revisão, uma vence e a outra recebe `Conflict("stale_revision")`; com `reread`, confirma que o estado final não foi alterado após o conflito |

Como o cenário grava linhas confirmadas, execute-o sempre na fixture de banco isolado — nunca em um banco compartilhado. Projetos derivados importam `from skeleton.testing.database import skeleton_db` em `conftest.py`: a fixture recria o schema `skeleton` a cada teste como `skeleton_owner` (migra uma vez por processo e restaura um template com `pg_dump`, isolando cada worker do `pytest-xdist`; veja [testes](testing.md#fixture-distribuída-em-paralelo-e-com-template)), executa as asserções como `skeleton_runtime` e exige banco descartável (`TEST_DATABASE_URL`, `TEST_MIGRATION_DATABASE_URL`, nome terminado em `_skeleton_test`, diferente de `DATABASE_URL` e `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`). Como apps nascem indisponíveis, use `skeleton.testing.applications.open_app(label)` antes do cenário para simular a liberação pela plataforma. Veja `tests/integration/test_notes_service_api.py` como referência completa; o gerador já cria `tests/conftest.py` e `tests/test_service_api.py` prontos com esses testes.

### Limites da verificação

- Imports dinâmicos (`importlib.import_module`, `__import__`, caminhos resolvidos em runtime) escapam da análise estática.
- Padrões indiretos como `import skeleton.tenancy.context as ctx` seguido de `ctx.system_context(...)` ou `from skeleton.tenancy.context import *` não são rastreados por análise de fluxo. `get_user_model()` e FKs declaradas por string (`"sk_tenancy.Membership"`) são cobertas por checks próprios.
- Os checks validam estrutura, não semântica: garantir que o método chama `authorized_queryset` e não vaza dados requer os testes do kit.
- Enquanto `W019` permanecer como aviso, evitar imports de módulos internos do núcleo depende também da revisão de código.

## Documentação por app

O arquivo `docs/service-api.md` de cada app deve conter:

1. **Visão geral:** por que e quando outras apps usariam esta API.
2. **Dependência:** `app_id` e faixa de versão recomendada para o manifesto consumidor.
3. **Serviços:** para cada classe e método, assinatura, descrição, seções Autorização, Erros e Efeitos e um exemplo de chamada.
4. **DTOs:** campos, tipos e significado.
5. **Eventos publicados** (se houver): tipos e payload.
6. **Histórico de versões:** métodos adicionados, depreciados e removidos.

O modelo pronto está no skill `service-api` (`references/templates.md`).

## O que a API de serviços não é

- Não é RPC remoto nem substitui a API REST para clientes externos.
- Não é atalho de privilégio: ser código Python não concede autorização, e `system_context` não é oferecido a consumidores.
- Não substitui eventos de domínio para reações assíncronas.
- Não compartilha transações entre organizações: toda chamada pertence exclusivamente à organização do contexto atual.
