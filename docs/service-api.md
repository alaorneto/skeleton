# API de serviços entre apps

**Estado: contrato entregue (epic [#112](https://github.com/alaorneto/skeleton/issues/112), core 0.4.0): base em `skeleton.service_api`, `runtime.service_api` no manifesto, checks E018/E019, kit de testes `skeleton.testing.service_api` e adoção pelo exemplo Notes e pelo gerador. Serviços do núcleo entregues (epic [#113](https://github.com/alaorneto/skeleton/issues/113), métodos `since="0.5.0"`): veja o [catálogo do núcleo](#catálogo-do-núcleo).** Este documento é o contrato normativo para apps que oferecem operações a outras apps ou consomem as do núcleo. O skill `service-api` (`.claude/skills/service-api/`) é o procedimento. O que as verificações automáticas não alcançam (seção "Limites da verificação") continua valendo por revisão de código.

## Por que existe

Uma app de agenda quer notificar participantes. Uma app de chamados quer saber o nome de exibição de quem abriu o chamado. Uma app de faturamento quer criar uma tarefa na app de tarefas. Sem contrato, cada uma importaria modelos, managers e funções internas das outras. Esses módulos mudam sem aviso, devolvem instâncias de modelo com campos sensíveis e permitem pular a autorização.

A **API de serviços** é o contrato em processo entre apps: classes de serviço em Python, com métodos tipados, que outras apps chamam diretamente. Ela é diferente da [API REST](api.md):

| | API de serviços | API REST (`/api/v1/`) |
|---|---|---|
| Quem chama | Código de outra app, no mesmo processo | Navegador ou cliente HTTP |
| Transporte | Chamada de método Python | HTTP + JSON |
| Transação | A do chamador (mesmo commit ou rollback) | Uma por requisição |
| Contexto | O `request_context`/`tenant_context` já aberto | Aberto pelo middleware a partir da URL |
| Autorização | Revalidada dentro do serviço | Revalidada pela view, que deve chamar o serviço |

A REST e as páginas HTML devem ser camadas finas sobre a API de serviços da própria app, para que exista um único caminho de regra de negócio.

## Termos

- **Provedor**: a app (ou subsistema do núcleo) que publica uma API de serviços.
- **Consumidor**: a app que chama a API de outra.
- **Serviço**: classe derivada de `ServiceAPI` publicada no módulo `service_api` do provedor.
- **Método público**: método de serviço decorado com `@service_method`; é o único ponto de entrada para outras apps.
- **DTO**: objeto imutável de transferência (`@dataclass(frozen=True, slots=True)`) com os campos que o contrato promete.

## Obrigações de toda app

1. **Publicar uma API de serviços.** Toda app tem `service_api.py` no pacote, com pelo menos uma classe de serviço, ainda que só com consultas. Ela é declarada no manifesto em `runtime.service_api` (verificação pelo check `skeleton.E018`; `package build` e `package doctor` recusam release sem a declaração).
2. **Publicar só por ela.** Outra app só pode importar o `service_api` do provedor. Modelos, managers, `services.py` internos, forms e views não são contrato (verificação estática pelo check `skeleton.E019` e pelo `package doctor`).
3. **Declarar a dependência.** Consumir a API de outra app exige declará-la em `dependencies` do manifesto, com faixa de versão, como já acontece com migrations e FKs (E014/E015).
4. **Autorizar dentro do serviço.** O método aplica `authorized_queryset`, `require_record_action`, capacidades e ativação da app. Ser chamado por código não concede permissão.
5. **Não vazar internos.** Retorna DTOs, páginas de DTOs ou tipos primitivos, nunca modelos, querysets ou campos de `sensitive_fields`.
6. **Documentar e testar.** Mantém `docs/service-api.md` no `documentation_root` da app e testes de contrato (seções abaixo).
7. **Versionar.** Mudança incompatível em método público é mudança *major* da app (seção "Versionamento").

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

Uma app grande pode transformar `service_api.py` em pacote (`service_api/__init__.py` reexportando as classes). O nome do módulo declarado continua sendo o ponto de importação.

Declaração no manifesto (`manifest_version` 2, core `>=0.4.0`):

```json
"runtime": {
  "service_api": {
    "module": "applications.tasks.service_api",
    "classes": ["TaskService"]
  }
}
```

- `module` fica dentro do pacote da app (`applications.<package_name>.…`); qualquer outro caminho é recusado pelo validador do manifesto.
- `classes` são nomes simples e únicos (1 a 32), definidos no módulo declarado (ou num submódulo dele).
- Declarar o campo exige `compatibility.skeleton` com piso `>=0.4.0`: um core mais antigo não conhece o campo. `package build` acrescenta o piso ao artefato publicado.
- Manifestos históricos sem o campo continuam carregando, com o aviso `skeleton.W018`, até a versão do core que tornar o campo obrigatório. `package build` e `package doctor` já exigem o campo, o módulo e `docs/service-api.md`.
- `skeleton.service_api.registry.service_registry()` devolve `app_id → classes` resolvidas, para introspecção e documentação; a página `/platform/apps/<app_label>/documentation/` mostra o `service-api.md` da app.

## Forma de um serviço e de seus métodos

- A classe deriva de `ServiceAPI` (`skeleton.service_api.base`, entregue em [#97](https://github.com/alaorneto/skeleton/issues/97)) e não guarda estado entre chamadas. O consumidor instancia sem argumentos: `TaskService()`.
- Todo método público:
  - é decorado com `@service_method`;
  - aceita somente argumentos **keyword-only** (`def m(self, *, actor, ...)`);
  - recebe o ator explícito em `actor`;
  - tem anotações de tipo completas e docstring com as seções **Autorização**, **Erros** e **Efeitos**.
- Nomes seguem o vocabulário do negócio, no imperativo para comandos (`create_task`, `close_task`) e no substantivo para consultas (`task`, `tasks`, `open_count`).
- Comandos que alteram um registro existente recebem `expected_revision`. Comandos que criam algo e podem ser repetidos por jobs aceitam `idempotency_key`.
- Métodos com nome iniciado por `_` são internos, mesmo dentro de `service_api.py`.

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

O serviço **não abre** contexto. Ele roda dentro do contexto do chamador:

| Chamador | Contexto já aberto | `actor` |
|---|---|---|
| View ou endpoint tenant | `tenant_context` aberto pelo middleware a partir da URL | `request.user` |
| Job `run_as="actor"` ([#77](https://github.com/alaorneto/skeleton/issues/77)) | `tenant_context` aberto pelo worker | ator do job |
| Job `run_as="system"` | `system_context(reason=...)` da organização | `None`, só em métodos `@service_method(allow_system=True)` |

O decorador confere que `actor` é o mesmo de `current_context().actor` (`skeleton/tenancy/context.py`) e que existe organização quando o método exige tenant. Uma app não consegue agir em nome de outra pessoa passando outro `actor`. Um serviço nunca chama `system_context` para ampliar o que o ator pode fazer, e nunca troca de organização no meio da chamada.

## Autorização

- Consultas partem de `authorized_queryset(model, user=actor, action=...)` e filtram antes de contar ou paginar.
- Comandos sobre um registro usam `require_record_action(record, user=actor, action=...)` depois de localizar o registro por um queryset autorizado.
- Operações administrativas usam capacidades (`require_capability`, de [#17](https://github.com/alaorneto/skeleton/issues/17)) ou os serviços de `skeleton.access.services`, nunca checagem manual de papel.
- Campos de acesso (`owner`, `visibility`, `owning_group`, grants) só mudam pela gestão de acesso autorizada, também via serviço.
- Registro inexistente, de outra organização ou não legível responde igual (`NotFound`). Diferenciar os casos revelaria a existência de dados.
- Antes de qualquer coisa, o serviço do provedor exige que a app dele esteja ativa e disponível na organização. `Unavailable` diz ao consumidor que a integração está desligada.
- Com a [cobrança](billing.md#ganchos-de-direito) ligada, a situação de pagamento da organização também vale: em `restricted`, os comandos respondem `Unavailable` com o código `organization_restricted` (leituras continuam); em `suspended`, todo método de um serviço de app responde `Unavailable` com `organization_suspended`, e os comandos do núcleo também. Chamadas em `system_context` não são limitadas, e um serviço com `access_exempt = True` (o de cobrança) também não.

## Retorno: DTOs e páginas

- DTOs são `@dataclass(frozen=True, slots=True)` declarados em `service_api.py` (ou num módulo `service_api/dto.py`) e documentados como parte do contrato.
- Campos permitidos: UUID, `str`, `int`, `bool`, `Decimal`, `date`, `datetime` com fuso, enums de texto, tuplas e outros DTOs. Datas e horas saem em UTC com `tzinfo`. Converter para o fuso da pessoa é trabalho da superfície.
- Só entram campos que estão em `read_fields` do `TableSpec` e fora de `sensitive_fields`, salvo campo sensível documentado na seção **Autorização** do método.
- Listas usam `Page[T]` (`items`, `next_cursor`, `page_size`, máximo de 100). Nunca devolva lista ilimitada. Um cursor ou uma página que endereça um deslocamento além de `MAX_OFFSET` (1 000 000 de linhas) é `InvalidInput`: leituras mais fundas usam filtros (#271). Exceção: coleção limitada por natureza e documentada no método pode sair como tupla (as organizações do próprio ator, os grupos do ator, as apps instaladas).
- `paginate(rows, convert=..., page_size=..., cursor=..., key=(...))` pagina por chave (keyset, #305) quando recebe `key`: os campos da ordenação, `-` para decrescente, o último único e nenhum nulo, como `("code", "sysid")` ou `("-created_at", "-record_revision", "sysid")`. Cada página começa depois da última linha vista: uma linha que entra ou sai entre chamadas não faz o percurso pular nem repetir outra, e a página custa o mesmo em qualquer profundidade. Use só sobre colunas imutáveis; uma coluna que muda (como `updated_at` da caixa de entrada) fica no modo por deslocamento. O cursor continua opaco, não concede acesso (só posiciona dentro do queryset já autorizado) e cursores antigos por deslocamento continuam aceitos. `OrganizationService.members`/`groups`, `AuditService.history` e `FileService.list` usam a chave.
- Retornar instância de modelo, `QuerySet` ou `dict` mutável é violação: `@service_method` recusa o valor na execução, e `assert_service_contract` reprova a anotação de retorno. A checagem na execução é uma lista de permitidos, a mesma do contrato: `None`, `bool`, `int`, `str`, `Decimal`, `date`/`datetime`, `UUID`, enum de texto, tupla e dataclass congelada (inclusive `Page`), em profundidade. Gerador, iterador, `float`, `bytes`, `frozenset`, objeto comum, dataclass não congelada e enum que não seja de texto são `TypeError` (#272).

## Erros

Os erros públicos ficam em `skeleton.service_api.errors` ([#97](https://github.com/alaorneto/skeleton/issues/97)). Exceções internas não atravessam a fronteira:

| Erro | Quando | HTTP correspondente |
|---|---|---|
| `NotFound` | Alvo inexistente, de outra organização ou não legível | 404 |
| `Forbidden` | Ator legível, mas sem permissão para a ação; ator divergente do contexto ou ausente, com o código próprio `actor_mismatch` (#321) | 403 |
| `InvalidInput(errors)` | Validação de campos; `errors` mapeia campo → mensagens | 400 |
| `Conflict(code)` | Revisão desatualizada (`code="stale_revision"`), unicidade (`integrity_conflict`), vínculos ativos (`live_references`), último admin (`last_administrator`, `last_platform_administrator`), cota | 409 |
| `Unavailable` | App provedora desativada, indisponível ou não instalada na organização; ou organização com acesso limitado pela cobrança (`organization_restricted`, `organization_suspended`) | 404 |
| `ContextRequired` | Chamada sem o contexto exigido | 500 (erro de programação) |

O consumidor trata `Unavailable` como "integração desligada": esconde a funcionalidade, sem falhar a operação principal.

A classificação das exceções internas (revisão, vínculos, último administrador, validação, integridade, contexto) fica num lugar só, `skeleton.service_api.internal_errors.classify`, compartilhado pela API de serviços, pela REST e pelos formulários HTML; cada superfície mantém a própria apresentação (status, `code`, mensagem) (#325).

## Transações, efeitos e idempotência

- Cada comando (`read_only=False`) roda num savepoint da transação do chamador: se levantar um erro público, nada do que ele gravou permanece, e a transação do chamador continua utilizável (#232).
- O serviço participa da transação do chamador e não faz commit. Pode usar `transaction.atomic()` como savepoint para isolar uma etapa.
- Efeitos externos (email, HTTP, push) não acontecem dentro do método. Eles vão para jobs ou eventos ([#77](https://github.com/alaorneto/skeleton/issues/77), [#78](https://github.com/alaorneto/skeleton/issues/78)) ou para `on_commit` com valores já materializados. Se o chamador desfizer a transação, nada sai.
- Reações a mudanças de outra app usam eventos de domínio, não chamadas de serviço disparadas por signals.
- `idempotency_key` repetida devolve o resultado da primeira execução, sem criar duplicata.

## Concorrência

Comandos de alteração recebem `expected_revision` e usam o CAS das bases (`revision`). Revisão desatualizada gera `Conflict(code="stale_revision")`. O serviço não tenta de novo em silêncio, porque sobrescreveria a alteração de outra pessoa. Quem decide reler e repetir é o chamador.

## Versionamento e depreciação

- O contrato da API de serviços segue a versão da app (`version` do manifesto).
- Remover método, remover ou mudar o tipo de campo de DTO, mudar o significado de um parâmetro ou de um erro é incompatível: versão *major*.
- Acrescentar método, parâmetro keyword opcional ou campo novo em DTO é compatível: versão *minor*.
- Para retirar algo, marque com `@deprecated(since=..., removal=...)` e mantenha por pelo menos uma versão *minor* antes da *major* que remove.
- O consumidor declara a faixa em `dependencies`, por exemplo `{"com.example.tasks": ">=1.2,<2.0"}`.

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

O manifesto de `billing` declara `com.example.tasks` em `dependencies`. Não há dependência opcional: se a integração só funciona com a outra app presente, ela é declarada, e a ausência por organização é tratada com `Unavailable`.

Uma app first-party, como a agenda, é declarada do mesmo modo, pelo `app_id` e com a faixa da versão do core: `"dependencies": {"org.skeleton.calendar": ">=0.5.0,<0.6.0"}`. A dependência só é satisfeita quando a instância instala a agenda (`SKELETON_BUNDLED_APPS=calendar`); vale para apps do projeto e para apps empacotadas ([extensão](extending.md#apps-first-party-do-núcleo-skeletoncontrib), #254).

## Superfícies públicas do núcleo para apps

Uma app pode importar do núcleo apenas os nomes abaixo. A coluna "Uso" é a lista fechada de nomes que o check verifica (`PUBLIC_CORE_NAMES` em `skeleton/system/import_boundary.py`, #300); onde ela fala em "construtores" (OpenAPI, fluxos), vale qualquer nome público do módulo. Nomes iniciados por `_` nunca são contrato, nem em `skeleton.service_api`.

| Módulo | Uso |
|---|---|
| `skeleton.service_api` e submódulos | Base, erros, DTOs e serviços do núcleo |
| `skeleton.common.models` | Bases `SystemRecord`, `OrganizationRecord`, `SecuredRecord` |
| `skeleton.common.migration_operations` | `InstallRecordInfrastructure` e `InstallSearchIndex`, só em migrations |
| `skeleton.system.app_registry` | `AppManifest`, `register_app` em `ready()` |
| `skeleton.system.registry` | `TableSpec`, `register` em `ready()` |
| `skeleton.system.record_operations` | `RecordOperations`, `register_record_operations` |
| `skeleton.access.policies` | `authorized_queryset`, `require_record_action` e o predicado de leitura `has_table_permission`, sobre modelos da própria app |
| `skeleton.tenancy.context` | `current_context` (leitura). `system_context`, `tenant_context` e `request_context` não: o contexto é aberto pelo middleware, pelo worker ou pelo kit de testes, nunca pela app |
| `skeleton.web.views` | `tenant_page` |
| `skeleton.api.views` | `TenantAPI` e `request_fields` (o objeto de campos do corpo) |
| `skeleton.api.openapi` | `Operation` e construtores de schema para o atributo `openapi` das views de API da app ([OpenAPI](api.md#schema-openapi)) |
| `skeleton.jobs.registry`, `skeleton.jobs.periodic` | `JobType`, `register_job_type`, `register_periodic` em `ready()`; enfileirar é pelo `JobService` |
| `skeleton.events.registry` | `EventType`, `register_event_type`, `register_consumer` em `ready()`; publicar é pelo `EventService` |
| `skeleton.notifications.registry` | `NotificationType`, `register_notification_type` em `ready()`; notificar é pelo `NotificationService` |
| `skeleton.sequences.registry` | `SequenceSpec`, `register_sequence` em `ready()`; numerar é pelo `SequenceService` |
| `skeleton.flows.registry` | `FlowSpec` e os construtores de etapas, transições, formulários e condições; `register_flow`, `register_flow_handler`, `register_flow_condition`, `register_flow_assignment` em `ready()`; iniciar e operar é pelo `FlowService` ([fluxos](flows.md)) |
| `skeleton.integrations.tokens.scopes` | `register_token_scope` em `ready()` (escopos nomeados, como o feed da agenda) |
| `skeleton.tenancy.aggregate` | `aggregate_route`, `for_each_organization` (leitura de várias organizações, #74) e `user_context` (contexto global da pessoa na parte sem organização de uma rota agregada; recusa aninhar, #343) |
| `skeleton.system.applications` | `application_state` e `require_application_enabled`, transitórios até o `ApplicationService` substituí-los |
| `skeleton.testing` | Kit de contrato e cenários, só em testes |

Para ler a própria configuração ou a ativação de outra app, use o `ApplicationService` ([#105](https://github.com/alaorneto/skeleton/issues/105)); `skeleton.system.applications.application_state` continua permitido nesta versão e sai da lista numa próxima. Importar outros módulos do núcleo é o aviso `skeleton.W019` nesta versão e vira erro numa próxima. A lista vale também para `from skeleton.common import models` e para imports sob `TYPE_CHECKING`; para anotar o ator sem importar o modelo do núcleo, use `django.contrib.auth.base_user.AbstractBaseUser`.

### Fronteira verificada

O check `skeleton.E019` analisa com `ast`, sem importar código, todos os `.py` de cada app registrada:

- importar de outra app registrada qualquer módulo que não seja o `service_api` declarado dela (ou submódulo dele) é erro, inclusive por import relativo (`from ..tasks.models import Task`) e por `from applications.tasks import models`;
- importar o `service_api` de outra app sem declará-la em `dependencies` é erro, como E014/E015 fazem para migrations e FKs;
- `applications.<pacote>` desconhecido (app não registrada) só admite `applications.<pacote>.service_api`;
- módulos fora das superfícies públicas do núcleo geram `skeleton.W019`;
- nomes fora da lista fechada de um módulo público (`from skeleton.tenancy.context import system_context`, `from skeleton.common.models import OperationalRecord`) e nomes privados (`_x`) de qualquer módulo `skeleton.*` geram `skeleton.W019` (#300);
- `apps.get_model("sk_...", ...)` com label literal do núcleo fora de `migrations/` gera `skeleton.W019`: chega a um modelo interno sem `import`. Em migrations, `apps.get_model` (modelo histórico) é obrigatório e não é acusado;
- todos os diretórios importáveis são analisados, inclusive `build/`, `dist/` e `node_modules/`; só `__pycache__` e diretórios iniciados por `.` ficam de fora;
- uma app first-party (`skeleton.contrib.<pacote>`) segue as regras de qualquer app: o `service_api` dela exige a dependência declarada (`org.skeleton.calendar`), e os demais módulos são `internal_import` (E019). Um `skeleton.contrib.<pacote>` que a análise não conhece (`package doctor` sem a first-party no inventário) só admite o `service_api`.

`package doctor` repete a análise antes do build: os provedores vêm do inventário alvo (`--inventory`); violações aparecem como `internal_import`, `undeclared_dependency` e `unreadable_source`, e o uso de módulo não público do núcleo como `core_import` com `"severity": "warning"`, que não reprova o doctor.

## Catálogo do núcleo

O núcleo publica a própria API de serviços em submódulos de `skeleton.service_api` (epic [#113](https://github.com/alaorneto/skeleton/issues/113)). Os serviços do núcleo não têm `app_label`: não dependem de ativação de app. Todos seguem as regras deste documento (keyword-only, `actor` igual ao do contexto, DTOs imutáveis, erros públicos).

| Serviço | Módulo | Métodos | Issue |
|---|---|---|---|
| `IdentityService` | `skeleton.service_api.identity` | `me`, `get_person`, `update_my_preferences`, `update_my_profile` | [#102](https://github.com/alaorneto/skeleton/issues/102) |
| `OrganizationService` | `skeleton.service_api.organizations` | `current`, `my_organizations`, `members`, `groups`, `is_member`, `member_refs`, `settings`, `update_settings` | [#103](https://github.com/alaorneto/skeleton/issues/103), [#167](https://github.com/alaorneto/skeleton/issues/167) |
| `AccessService` | `skeleton.service_api.access` | `can`, `require`, `capabilities`, `granted_record_ids`, `my_groups`, `grant_member_table_access` | [#104](https://github.com/alaorneto/skeleton/issues/104) |
| `ApplicationService`, `ApplicationAdministrationService` | `skeleton.service_api.applications` | `is_enabled`, `require_enabled`, `my_configuration`; administração: `applications`, `application`, `configure` | [#105](https://github.com/alaorneto/skeleton/issues/105) |
| `AuditService`, `AuditLogService` | `skeleton.service_api.audit` | `history`; log das superfícies do núcleo: `events`, `event`, `related_domain_events` | [#106](https://github.com/alaorneto/skeleton/issues/106), [#110](https://github.com/alaorneto/skeleton/issues/110) |
| `NotificationService` | `skeleton.service_api.notifications` | `notify`, `unread_count`, `inbox`, `mark_read`, `archive` | [#107](https://github.com/alaorneto/skeleton/issues/107) |
| `JobService`, `EventService` | `skeleton.service_api.jobs`, `skeleton.service_api.events` | `enqueue`, `publish` | [#108](https://github.com/alaorneto/skeleton/issues/108) |
| `FileService` | `skeleton.service_api.files` | `attach`, `list`, `download_url`, `remove` | [#109](https://github.com/alaorneto/skeleton/issues/109) |
| `CommentService` | `skeleton.service_api.comments` | `comments`, `add_comment`, `edit_comment`, `delete_comment`, `restore_comment` | [#187](https://github.com/alaorneto/skeleton/issues/187) |
| `SequenceService` | `skeleton.service_api.sequences` | `next_number` | [#155](https://github.com/alaorneto/skeleton/issues/155) |
| `FeedTokenService` | `skeleton.service_api.tokens` | `issue` | [#76](https://github.com/alaorneto/skeleton/issues/76) |
| `BillingService` | `skeleton.service_api.billing` | `access_state`, `entitlement`, `storage_allowance`, `check_quota`, `subscription`, `list_invoices`, `offers`, `contract`, `remove_item`, `cancel`, `resume`, `redeem_voucher`, `update_account`, `checkout` | [#404](https://github.com/alaorneto/skeleton/issues/404) |
| `FlowService` | `skeleton.service_api.flows` | `start`, `signal`, `instance`, `instances`, `history`, `tasks`, `open_task_count`, `task`, `claim_task`, `release_task`, `reassign_task`, `complete_task`, `decide_task`, `cancel`, `suspend`, `resume`, `retry` | [#205](https://github.com/alaorneto/skeleton/issues/205) |

### IdentityService

```python
from skeleton.service_api.identity import IdentityService

me = IdentityService().me(actor=request.user)  # UserProfileInfo
person = IdentityService().get_person(actor=request.user, user_id=assignee_id)  # PersonSummary
IdentityService().update_my_preferences(actor=request.user, expected_revision=me.revision, timezone="America/Sao_Paulo")
```

| Método | Autorização | Erros |
|---|---|---|
| `me(*, actor) -> UserProfileInfo` | o próprio ator; com ou sem organização no contexto | `Forbidden` (ator divergente) |
| `get_person(*, actor, user_id) -> PersonSummary` | vínculo ativo do ator; `email` só com a capacidade `members.view` | `NotFound` se a pessoa não tiver vínculo ativo na organização atual, mesmo que exista |
| `update_my_preferences(*, actor, expected_revision, language=None, timezone=None) -> UserProfileInfo` | só o próprio ator | `InvalidInput` (idioma/fuso inválido, nada a alterar); `Conflict("stale_revision")` |
| `update_my_profile(*, actor, expected_user_revision, expected_revision, changes) -> UserProfileInfo` | só o próprio ator; `changes` com `first_name`, `last_name`, `display_name`, `timezone`, `language` | `InvalidInput` (campo não permitido ou valor inválido); `Conflict("stale_revision")` em qualquer das duas revisões |

`UserProfileInfo` tem `sysid`, `email`, `display_name`, `language`, `timezone`, `kind`, `revision` (do perfil, para CAS), `first_name`, `last_name`, `user_revision`, `profile_sysid` e `profile_display_name` (o valor gravado, sem a regra de fallback de `display_name`); `PersonSummary` tem `sysid`, `display_name`, `email` (ou `None`) e `kind`. `kind` é `person`, ou `service` para contas de serviço (#44). Nenhum DTO traz senha, MFA, flags de plataforma ou `auth_version`.

### OrganizationService

| Método | Autorização | Erros |
|---|---|---|
| `current(*, actor) -> OrganizationInfo` | vínculo ativo | `ContextRequired` sem organização |
| `my_organizations(*, actor) -> tuple[OrganizationInfo, ...]` | o próprio ator, por autodescoberta; só organizações ativas com vínculo ativo; funciona sem organização | `Forbidden` (ator divergente) |
| `members(*, actor, query=None, group_id=None, cursor=None, page_size=25) -> Page[PersonSummary]` | vínculo ativo; `email` só com `members.view` | `NotFound` (grupo de outra organização); `InvalidInput` (busca acima de 100 caracteres, cursor, página acima de 100) |
| `groups(*, actor, cursor=None, page_size=25) -> Page[GroupSummary]` | vínculo ativo | `InvalidInput` |
| `is_member(*, actor, user_id) -> bool` | vínculo ativo | `InvalidInput` |
| `member_refs(*, actor, user_ids, include_service_accounts=False) -> tuple[MemberRef, ...]` | Vínculos ativos da organização entre `user_ids` (`user_id`, `membership_id`, ordenados por `user_id`), só pessoas por padrão; sem nome nem email. Vínculo ativo do ator, ou `actor=None` em `system_context` (jobs) (#343) | `InvalidInput` (identificador inválido, mais de 500) |
| `settings(*, actor) -> OrganizationSettingsInfo` | vínculo ativo | `ContextRequired` sem organização |
| `update_settings(*, actor, display_name, default_language, default_timezone, brand_color, expected_revision) -> OrganizationSettingsInfo` | administrador da organização | `Forbidden`; `InvalidInput` (idioma fora de `LANGUAGES`, fuso inválido, cor fora de `#RRGGBB`, nome acima de 200 caracteres); `Conflict("stale_revision")` |

As configurações começam em branco com `revision=0` e passam a existir na primeira gravação; valores em branco significam "use o padrão da instância", e `has_logo` diz se há logo sem revelar a chave do storage.

O diretório traz só vínculos ativos de identidades ativas da organização atual, busca por nome de exibição ou nome, e deixa de fora as contas de serviço (#44). Administrar vínculos continua nos serviços de acesso.

### AccessService

| Método | Comportamento | Erros |
|---|---|---|
| `can(*, actor, action, table_key, record_id=None) -> bool` | Exatamente `authorized_queryset(model, user=actor, action=action)` (filtrado pelo registro, quando informado); tabela desconhecida ou não exposta, registro inexistente ou de outra organização: `False` | `InvalidInput` (ação ou UUID inválidos) |
| `require(*, actor, action, table_key, record_id=None) -> None` | Igual a `can`, mas levanta | `NotFound` se não for legível (inclusive de outra organização); `Forbidden` se for legível e a ação for negada |
| `capabilities(*, actor) -> tuple[str, ...]` | Capacidades dos papéis internos do ator, ordenadas (tupla: o contrato não admite `frozenset`) | — |
| `granted_record_ids(*, actor, table_key, record_ids, action="read") -> tuple[UUID, ...]` | Quais de `record_ids` têm concessão de registro viva ao vínculo do ator, a um grupo dele ou a um papel efetivo, na organização do contexto; não dispensa a `TablePermission` nem diz se o registro existe (inexistente e de outra organização simplesmente não aparecem) (#343) | `InvalidInput` (tabela não exposta, ação fora de `read`/`update`/`delete`, identificador inválido, mais de 500) |
| `my_groups(*, actor) -> tuple[GroupSummary, ...]` | Grupos vivos do ator na organização | `Forbidden` sem vínculo |
| `grant_member_table_access(*, actor, table_key, actions) -> tuple[str, ...]` | Dá ao papel interno `member` as ações numa tabela exposta, pelo serviço de concessão da gestão de acesso (lock e auditoria); devolve só as ações acrescentadas, então repetir não muda nada. Usado pela ação "acesso básico" da agenda (#68) | `Forbidden` sem `access.manage`; `InvalidInput` (tabela não exposta, ação inválida, catálogo ou papel indisponível) |

Não há cache entre chamadas: uma revogação vale na chamada seguinte. Alterar ACL continua nos serviços de gestão de acesso.

### ApplicationService

| Método | Comportamento | Erros |
|---|---|---|
| `is_enabled(*, actor, app_id) -> bool` | `app_id` ou `app_label`; verdadeiro se a app está instalada, disponível pela plataforma para a organização e ativa, com as dependências; app desconhecida: `False` | `InvalidInput` (vazio) |
| `require_enabled(*, actor, app_id) -> None` | Igual, mas levanta o `Unavailable` padronizado | `Unavailable` |
| `my_configuration(*, actor, app_label) -> AppConfiguration` | Configuração da organização para a **própria** app, com todos os campos declarados resolvidos; `AppConfiguration.get(nome)` | `Forbidden` se o módulo que chama não pertencer a `app_label`; `Unavailable` se a app estiver desativada ou indisponível |

A app chamadora é identificada pelo módulo do primeiro frame fora da API de serviços, do kit de testes e do Django, resolvido para a app registrada que o contém (`apps.get_containing_app_config`). Uma app não lê a configuração de outra. Essa identificação assume que o código das apps é confiável (o mesmo processo); ela impede engano, não um código malicioso que forje frames. Ativar ou configurar apps continua com o administrador.

### AuditService

`history(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[AuditEntry]` devolve o histórico de um registro, do mais recente ao mais antigo, com as mesmas regras de `authorized_audit_queryset`: administrador da organização, ou `TablePermission audit` mais leitura do registro atual (se excluído, restauração autorizada). Filtra antes de paginar. Sem essas permissões, para tabela desconhecida e para registro de outra organização, responde `NotFound`; eventos de outra organização nunca aparecem.

`AuditEntry` tem `sysid`, `occurred_at` (UTC), `operation`, `record_revision`, `actor_id`, `request_id`, `source` e `changes`, uma tupla de `FieldChange(name, old, new, redacted)` restrita aos `read_fields` da tabela. Valores sensíveis chegam como `[REDACTED]`; valores não escalares chegam como JSON em texto. A auditoria global da plataforma não passa por este serviço.

`AuditLogService` é o log que as páginas e a REST do núcleo mostram (`/o/<org>/audit/`, `/api/v1/o/<org>/audit/` e `/platform/audit/`). `events(*, actor, model_label=None, record_id=None, since=None, until=None, actor_id=None, operation=None, request_id=None, page=1, page_size=25) -> AuditLogPage` devolve uma página numerada (`items`, `count`, `page`, `page_size`) com as regras de `authorized_audit_queryset`, filtrada antes de contar; `event(*, actor, event_id) -> AuditLogEvent` devolve um evento ou `NotFound`. `AuditLogEvent` traz os campos do evento como a REST os publica (inclusive `table_name` e `db_principal`) e `changes_json`, o objeto JSON das colunas visíveis. `table_name` e `db_principal` são identificadores de infraestrutura (nome físico da tabela e role do banco): chegam como texto vazio para quem não administra a organização nem a plataforma (#324). Sem organização no contexto, os dois métodos leem os eventos globais e exigem o administrador da plataforma (`Forbidden` para os demais). Os filtros de período (`since` inclusivo, `until` exclusivo), ator, operação e requisição só estreitam o que a autorização já permite ([#184](https://github.com/alaorneto/skeleton/issues/184)). `related_domain_events(*, actor, request_id) -> tuple[DomainEventSummary, ...]` devolve, só para o administrador da organização, os eventos de domínio ainda retidos da mesma requisição: tipo, tabela, registro, revisão e momento, nunca `data`. Para o histórico de um registro numa app, prefira `AuditService.history`, que já confere a leitura do registro.

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
| `notify(*, actor, type_code, recipients, payload=(), target_table_key=None, target_id=None, dedupe_key=None) -> NotificationReceipt` | a app chamadora é a dona do tipo (`owner_app`); o ator lê o alvo; cada destinatário é reautorizado no fan-out; `actor=None` só em `system_context` (consumidores de eventos) | `Forbidden` (tipo de outra app), `NotFound` (alvo ilegível ou de outra organização), `InvalidInput` |
| `unread_count(*, actor) -> int` | linhas do ator em organizações com vínculo ativo | — |
| `inbox(*, actor, cursor=None, include_archived=False, page_size=25) -> Page[NotificationItem]` | linhas do ator: o tenant atual, ou todas as organizações ativas sem tenant | `InvalidInput` |
| `mark_read(*, actor, ids, read=True) -> int`, `archive(*, actor, ids) -> int` | linhas do ator no tenant atual; idempotentes | `NotFound` (linha de outra pessoa), `InvalidInput` |

`Recipients(users, groups, roles, organization)` é o DTO público dos destinatários. Os tipos continuam registrados por cada app ([notificações](notifications.md)). O rollback do chamador não deixa notificação nem job. Canais e preferências ficam internos ao epic de notificações.

### JobService e EventService

| Método | Autorização | Erros |
|---|---|---|
| `JobService.enqueue(*, actor, job_type, payload=(), idempotency_key=None, run_after=None) -> JobReceipt` | o tipo pertence à app chamadora (`JobType.owner_app`); a organização é a do contexto; `actor=None` em `system_context` | `Forbidden` (tipo alheio), `InvalidInput` (tipo desconhecido, payload inválido, `run_after` sem fuso) |
| `EventService.publish(*, actor, event_type, record_id, data=()) -> EventReceipt` | o tipo pertence à app chamadora (`EventType.owner_app`); o ator lê o registro | `Forbidden`, `NotFound` (registro ilegível ou de outra organização), `InvalidInput` |

Tipo registrado sem `owner_app` não pode ser usado pelo serviço: é `Forbidden` para qualquer chamador, app ou não (#272). Isso vale também para `NotificationService.notify` e `SequenceService.next_number`.

`JobReceipt` traz `sysid`, `job_type`, `state`, `run_after` e `created` (falso quando a chave de idempotência já existia); `EventReceipt` traz `sysid`, `event_type`, `record_id` e `occurred_at`. Nenhum recibo devolve o payload. Os dois gravam na transação do chamador: um rollback não deixa job nem evento. Tipos e consumidores continuam registrados em `ready()` ([jobs](jobs.md), [eventos](events.md)). A `idempotency_key` vale por tipo de job e organização: repetida no mesmo tipo e organização, devolve o job existente; usada por outra organização, é recusada sem revelar o job alheio ([jobs](jobs.md#enfileirar)).

### FeedTokenService

| Método | Autorização | Erros |
|---|---|---|
| `FeedTokenService.issue(*, actor, name, scope, expires_at) -> IssuedFeedToken` | a própria pessoa (sem contas de serviço), política de MFA; `scope` é um escopo de feed registrado (`register_token_scope(..., feed=True)`) | `InvalidInput` (escopo que não é de feed, nome ou validade), `Forbidden` |

`IssuedFeedToken` traz `sysid`, `prefix`, `scope`, `expires_at` e o `text`, só nesta resposta. Para a view do feed, `skeleton.service_api.tokens.authenticate_feed(request, text, scope=...)` devolve o usuário de um token de feed vivo com o escopo, ou `None` (inclusive acima dos limites de taxa), e a view responde 404. Um token de feed nunca autentica `/api/v1/` ([#76](https://github.com/alaorneto/skeleton/issues/76)). `authenticate_feed(request, text, *, scope)` é função de apoio para a view de feed da própria app; não é método de serviço e não segue as regras de `@service_method`.

### FileService

| Método | Autorização | Erros |
|---|---|---|
| `attach(*, actor, table_key, record_id, filename, stream, content_type=None) -> AttachmentInfo` | update no registro alvo; o tipo é provado pelo conteúdo (`content_type` é só indicativo) | `NotFound` (tabela sem opt-in, alvo inexistente, de outra organização ou ilegível); `Forbidden` (lê sem update); `InvalidInput` (tipo, nome ou tamanho); `Conflict("quota_exceeded")` |
| `list(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[AttachmentInfo]` | leitura do alvo | `NotFound`, `InvalidInput` |
| `download_url(*, actor, attachment_id) -> str` | leitura do alvo e anexo `available`; a URL é interna e a view reautoriza a cada pedido | `NotFound` (inclusive pendente ou em quarentena) |
| `remove(*, actor, attachment_id, expected_revision) -> None` | update no alvo | `NotFound`, `Forbidden`, `Conflict("stale_revision")` |

Apps não acessam o armazenamento diretamente.

### CommentService

[Comentários](comments.md) em registros de tabelas com `CommentPolicy`, autorizados pelo registro alvo como no `FileService`. `comments(*, actor, table_key, record_id, cursor=None, page_size=25) -> Page[CommentInfo]` lista os vivos, do mais antigo ao mais novo; `add_comment(*, actor, table_key, record_id, body)` exige leitura do alvo; `edit_comment(..., comment_id, body, expected_revision)` só para o autor; `delete_comment` e `restore_comment(..., comment_id, expected_revision)` para o autor ou o administrador da organização. `CommentInfo` traz o corpo como texto simples: renderize-o escapado (`skeleton.comments.text.linkify` faz o escape e os links). Erros: `NotFound` para tabela sem a política e alvo inexistente, de outra organização ou ilegível; `Forbidden` para quem não pode editar ou excluir; `Conflict("stale_revision")`; `InvalidInput` para corpo vazio, acima de 5.000 caracteres, com caracteres de controle ou com menção a quem não é membro ou não lê o registro ([menções](comments.md#menções)). Métodos com `since="0.6.0"`.

### SequenceService

| Método | Autorização | Erros |
|---|---|---|
| `next_number(*, actor, key, at=None) -> SequenceNumber` | a app chamadora é a dona da sequência (`owner_app`); numera na organização do contexto | `Forbidden` (sequência de outra app); `InvalidInput` (sequência não declarada, data sem fuso) |

`SequenceNumber` tem `key`, `period`, `number` e `formatted`. O incremento acontece na transação do chamador: um rollback devolve o número e a numeração não tem lacunas. Veja [sequências](sequences.md).

### BillingService

A [cobrança](billing.md) da organização. As apps leem a situação dela e conferem os próprios limites; as páginas e a API de cobrança do núcleo usam o resto. O serviço continua usável com o acesso limitado (`access_exempt = True`): é por ele que a conta é regularizada. Métodos com `since="0.6.0"`.

| Método | Autorização | Erros |
|---|---|---|
| `access_state(*, actor) -> AccessState` | qualquer membro, ou `system_context` | — |
| `entitlement(*, actor, app_label) -> bool` | qualquer membro, ou `system_context`; uma app sem preço sempre tem direito | — |
| `storage_allowance(*, actor) -> StorageAllowance` | qualquer membro, ou `system_context` | — |
| `check_quota(*, actor, metric, increment=1) -> None` | qualquer membro, ou `system_context` | `Conflict("quota_exceeded")` acima do limite; `InvalidInput` para métrica não registrada |
| `subscription(*, actor) -> SubscriptionSummary \| None` | `billing.view` | `NotFound` sem a capacidade |
| `list_invoices(*, actor, page_size=20, cursor=None) -> Page[InvoiceSummary]` | `billing.view` | `NotFound` sem a capacidade |
| `offers(*, actor) -> tuple[PriceOffer, ...]` | `billing.view`; preços públicos e ativos na moeda e no ciclo da organização | `NotFound` sem a capacidade |
| `contract(*, actor, price_id, terms_version="") -> SubscriptionSummary` | `billing.manage`; um plano inicia ou troca a assinatura, uma app ou pacote vira item | `NotFound` (preço inexistente ou não público); `InvalidInput` (ciclo, app já contratada); `Conflict("organization_restricted")` com o acesso limitado |
| `remove_item(*, actor, item_id)`, `cancel(*, actor, at_period_end=True)`, `resume(*, actor)` | `billing.manage` | `NotFound`; `InvalidInput` |
| `redeem_voucher(*, actor, code)` | `billing.manage` | `InvalidInput` com o código do motivo (`invalid_voucher`, `already_redeemed`, `not_trialing`, `too_many_attempts`) |
| `update_account(*, actor, legal_name, billing_email, tax_id, country) -> None` | `billing.manage` | `InvalidInput` |
| `checkout(*, actor, invoice_id, return_url) -> CheckoutLink` | `billing.manage` | `NotFound` (fatura de outra organização); `InvalidInput` (fatura não aberta, sem provedor) |

`AccessState` tem `level` e `since`; `StorageAllowance`, `quota_bytes` (`None`: ilimitada), `used_bytes` e `overage` (`block` ou `bill`). Os DTOs de assinatura, item, fatura e oferta trazem só identificadores, valores na menor unidade da moeda e datas; dados de contato da conta nunca saem por eles.

### FlowService

Opera os [fluxos de processo](flows.md) que as apps declaram com `skeleton.flows.registry`. É um serviço do núcleo (sem `app_label`), mas toda chamada exige a **app dona do fluxo** ativa na organização e responde `Unavailable` sem ela; as listas omitem o que é de apps desativadas. Métodos com `since="0.6.0"`.

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
| `start(*, actor, flow_key, subject_id, data=(), idempotency_key=None) -> StartReceipt` | a app chamadora é a dona do fluxo; o ator lê o registro de negócio e tem a ação de início (`update` por padrão); `actor=None` em `system_context` | `Forbidden` (fluxo de outra app, sem a ação), `NotFound` (registro inexistente, de outra organização ou ilegível), `InvalidInput`, `Conflict("already_running" \| "definition_changed" \| "idempotency_mismatch" \| "flow_failed")` |
| `signal(*, actor, instance_id, name, data=()) -> SignalReceipt` | a app chamadora é a dona; o ator lê a instância; `actor=None` em `system_context` | `Forbidden`, `NotFound`, `InvalidInput`, `Conflict("instance_suspended")` |
| `instance(*, actor, instance_id) -> InstanceInfo`, `history(*, actor, instance_id, cursor=None, page_size=25) -> Page[ActivityEntry]` | quem lê o registro de negócio, quem iniciou, quem participa das tarefas, o administrador da organização | `NotFound` |
| `instances(*, actor, flow_key=None, subject_id=None, status=None, cursor=None, page_size=25) -> Page[InstanceInfo]` | a mesma, filtrada antes de paginar | `InvalidInput` |
| `tasks(*, actor, scope="mine", status=None, cursor=None, page_size=25) -> Page[TaskSummary]`, `open_task_count(*, actor) -> int` | `mine` e `available` (filas): só o que o ator pode fazer agora; `all`: o que ele pode ler | `InvalidInput` |
| `task(*, actor, task_id) -> TaskDetail` | responsável, candidatos da fila, quem participou, administrador. Valores sensíveis do formulário só para quem pode agir (`can_act`) | `NotFound` |
| `claim_task`, `release_task`, `reassign_task(…, to_user_id, comment="")`, `complete_task(…, action=None, data=(), comment="")`, `decide_task(…, decision, data=(), comment="")`, todos com `task_id` e `expected_revision`, devolvem `TaskSummary` | responsável ou candidato da fila; reatribuir: responsável de etapa delegável ou administrador | `NotFound`, `Forbidden`, `InvalidInput`, `Conflict("stale_revision")`, `Conflict` de estado (`instance_suspended`, `instance_closed`, `task_closed`, `not_a_queue_task`, `not_claimed`, `not_a_task`, `not_an_approval`, `flow_failed`) |
| `cancel(…, reason="")`, `suspend(…, reason="")`, `resume`, `retry`, todos com `instance_id` e `expected_revision`, devolvem `InstanceInfo` | administrador da organização; `cancel` também por quem iniciou, se o fluxo permitir | `NotFound`, `Forbidden`, `Conflict("stale_revision" \| "instance_closed" \| "not_running" \| "not_suspended" \| "not_failed")` |

- A ordem das verificações é sempre: visível (senão `NotFound`), autorizado a agir (senão `Forbidden`), revisão (`Conflict("stale_revision")`), estado da tarefa e da instância. Quem perde a corrida numa tarefa de fila recebe conflito, não `NotFound`: a tarefa encerrada continua legível para quem está na fila.
- Dados de formulário entram como tupla de pares `(campo, valor)`; só os campos declarados na etapa e na transição escolhida são aceitos.
- `InstanceInfo.data` e `ActivityEntry` nunca trazem campos sensíveis. O comentário de um passo aparece no histórico para quem lê a instância; só a auditoria o redige.
- Tudo é gravado na transação do chamador: um rollback não deixa instância, tarefa, job, evento nem notificação.

### Superfícies do núcleo sobre os serviços

A REST e as páginas do núcleo são camadas finas sobre estes serviços ([#110](https://github.com/alaorneto/skeleton/issues/110)), sem mudança observável: `GET/PATCH /api/v1/me/` e a página de perfil usam o `IdentityService`; a auditoria usa o `AuditLogService`; `GET /api/v1/organizations/`, a página inicial e o painel da organização usam o `OrganizationService`; `GET/PUT /api/v1/o/<org>/apps/...` e as páginas `/o/<org>/apps/...` usam o `ApplicationAdministrationService`, e o formulário de configuração mostra a mensagem do serviço (dependência, disponibilidade, valor) em vez de uma genérica (#338). `ApplicationState.configuration_valid` (padrão verdadeiro, acrescentado sem quebrar o contrato) diz se a configuração gravada ainda cabe no manifesto (#251). Os erros de serviço passam por um único mapeamento para HTTP (`skeleton.api.errors.service_error_response`); os recursos do núcleo que recebem `If-Match` usam `if_match_precondition()` para manter o 412 `revision_conflict` do gateway numa revisão desatualizada. A auditoria (`/o/<org>/audit/`, a REST `/api/v1/o/<org>/audit/`, com o filtro por registro, e `/platform/audit/`) usa o `AuditLogService`, que mantém a paginação numerada e os campos publicados. Ficam fora os recursos administrativos de acesso (papéis, grupos, concessões, convites e vínculos) e o gateway genérico de tabelas, que continuam sobre `skeleton.access.services` e as policies; não há superfície de diretório de membros no núcleo, e o `OrganizationService.members` atende as apps. A cota por organização vem da `StorageQuota` definida pela plataforma ou de `SKELETON_FILES_ORG_QUOTA_BYTES` (desligada por padrão), com reserva sob lock ([#61](https://github.com/alaorneto/skeleton/issues/61)). Veja [arquivos e anexos](files.md).

A app de agenda é a referência de app first-party com `CalendarService` ([#111](https://github.com/alaorneto/skeleton/issues/111)).

## Testes obrigatórios

Para cada método público, prove:

- **Caminho autorizado:** retorno correto em DTO, sem campos sensíveis.
- **Default deny:** ator sem `TablePermission` ou sem acesso ao registro recebe `NotFound` (leitura) ou `Forbidden` (ação).
- **Organizações A/B:** nada de B aparece em retorno, contagem ou erro quando o contexto é A.
- **App desativada ou indisponível:** `Unavailable`.
- **Ator divergente do contexto:** `Forbidden`.
- **Comandos:**
  - CAS com duas chamadas na mesma revisão: uma vence, a outra recebe `Conflict`;
  - `idempotency_key` repetida não duplica;
  - rollback do chamador não deixa efeitos nem eventos.
- **Contrato:** `assert_service_contract(ServiceClass)` do kit.

Rode em PostgreSQL com a role runtime, como exige o [AGENTS.md](../AGENTS.md). Registre na [evidência de testes](testing-evidence.md) somente o que rodou de fato.

### Kit de testes de contrato

`skeleton.testing.service_api` é distribuído no wheel do core. As funções levantam `AssertionError` e funcionam com pytest ou unittest:

| Helper | Prova |
|---|---|
| `assert_service_contract(Service)` | Estrutura do E018 mais anotações de retorno só com DTO congelado, `Page[T]`, tupla, enum de texto ou primitivo (nunca modelo, `QuerySet`, `dict`, `list`, `Any` ou `float`), parâmetros anotados (exceto `actor`) e docstring com `Autorização:`, `Erros:` e `Efeitos:` |
| `build_service_scenario(Model)` | Organizações A/B, `member` com `TablePermission` em todas as ações nas duas, `denied` em A sem permissão, `outsider` sem vínculo e a app ativa nas duas. Identidades, organizações e vínculos vêm dos serviços públicos de identidade e provisionamento; papéis, permissões, ativação e registros são fixture em `system_context`. `scenario.password` é a senha das identidades (sorteada por processo) para os testes que fazem login (#321) |
| `scenario.caller(fn, user=..., organization=...)` | Uma chamada `fn(actor)` dentro do próprio contexto (contextos não aninham nem trocam de ator) |
| `assert_tenant_isolated(call, foreign=[...], allowed_errors=(NotFound,), expected_count=None)` | Nenhum UUID, código ou título de B aparece em retorno, página ou mensagem de erro. `foreign` vazio é `TypeError`; um erro fora de `allowed_errors` reprova; uma contagem só é conferida com `expected_count` (#321) |
| `assert_denied_as_not_found(call, missing=...)` | Negado responde `NotFound` idêntico ao de um alvo inexistente |
| `assert_unavailable_when_disabled(call, disabled=scenario.application_disabled)` | Com a app desativada na organização, `Unavailable`. Antes, uma chamada de controle com a app ativa não pode ser `Unavailable` (outro erro de serviço é aceito: ele prova que a chamada passou da verificação de disponibilidade) |
| `assert_rejects_other_actor(call)` | `actor` diferente do contexto (ou `None` fora de `system_context`), `Forbidden(code="actor_mismatch")`. Outro `Forbidden` reprova: use como "outro ator" uma pessoa plenamente autorizada |
| `assert_cas_conflict(first, second, reread=None)` | Duas escritas na mesma revisão: uma vence, a outra recebe `Conflict("stale_revision")`; com `reread`, o registro lido depois da primeira escrita é igual ao lido depois do conflito |

O cenário grava linhas confirmadas: use-o dentro da fixture de banco isolado, nunca num banco compartilhado. Um projeto derivado a importa do wheel: `from skeleton.testing.database import skeleton_db` no `conftest.py` (#334). Ela recria o schema `skeleton` e o migra como `skeleton_owner` a cada teste, roda as asserções como `skeleton_runtime` e só aceita um banco descartável: `TEST_DATABASE_URL` (runtime), `TEST_MIGRATION_DATABASE_URL` (owner), banco terminado em `_skeleton_test`, diferente de `DATABASE_URL`, e `SKELETON_TEST_ALLOW_SCHEMA_RESET=1`; sem isso, para com erro de uso antes de tocar no banco. As apps nascem indisponíveis (#22): antes do cenário, `skeleton.testing.applications.open_app(label)` faz o papel da plataforma. `tests/integration/test_notes_service_api.py` é o exemplo completo. O gerador cria `tests/conftest.py` com essa fixture e `tests/test_service_api.py` com o teste de contrato e os cenários dos três métodos gerados.

### Limites da verificação

- Imports dinâmicos (`importlib.import_module`, `__import__`, strings resolvidas em runtime) escapam da análise estática.
- `import skeleton.tenancy.context as ctx` seguido de `ctx.system_context(...)` e `from skeleton.tenancy.context import *` não são vistos: rastrear atributos exigiria análise de fluxo. `get_user_model()` e FKs por string (`"sk_tenancy.Membership"`) também não são acusados; a integridade delas tem checks próprios.
- Os checks verificam forma, não semântica: que o método chama `authorized_queryset` e não vaza dados só se prova com os testes do kit.
- W019 ainda é aviso; até virar erro, um import de módulo interno do núcleo depende da revisão.

## Documentação por app

`docs/service-api.md` de cada app contém:

1. **Visão geral:** para que outras apps usariam esta API.
2. **Dependência:** `app_id` e faixa de versão recomendada para o manifesto do consumidor.
3. **Serviços:** para cada classe e método, a assinatura, a descrição, as seções Autorização, Erros e Efeitos e um exemplo de chamada.
4. **DTOs:** campos, tipos e significado.
5. **Eventos publicados**, se houver: tipos e payload.
6. **Histórico de versões:** métodos acrescentados, depreciados e removidos.

O modelo está no skill `service-api` (`references/templates.md`).

## O que a API de serviços não é

- Não é RPC remoto nem substitui a API REST para clientes externos.
- Não é atalho de privilégio: ser código não autoriza, e `system_context` não é oferecido a consumidores.
- Não substitui eventos de domínio para reações assíncronas.
- Não permite compartilhar transação entre organizações: uma chamada pertence à organização do contexto.
