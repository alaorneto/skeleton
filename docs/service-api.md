# API de serviços entre apps

**Estado: especificação aprovada, implementação em andamento.** Este documento é o contrato normativo para apps que oferecem operações a outras apps ou consomem as do núcleo. A base técnica, as verificações automáticas e o catálogo de serviços do núcleo estão nos epics [#112](https://github.com/alaorneto/skeleton/issues/112) (contrato) e [#113](https://github.com/alaorneto/skeleton/issues/113) (núcleo). Cada seção indica quando depende de algo ainda não entregue. Até lá, a obrigação vale por revisão de código, e o skill `service-api` (`.claude/skills/service-api/`) é o procedimento.

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

1. **Publicar uma API de serviços.** Toda app tem `service_api.py` no pacote, com pelo menos uma classe de serviço, ainda que só com consultas. Ela é declarada no manifesto em `runtime.service_api` (verificação pelo check `skeleton.E018`, a partir de [#98](https://github.com/alaorneto/skeleton/issues/98)).
2. **Publicar só por ela.** Outra app só pode importar o `service_api` do provedor. Modelos, managers, `services.py` internos, forms e views não são contrato (verificação estática pelo check `skeleton.E019`, a partir de [#99](https://github.com/alaorneto/skeleton/issues/99)).
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

Declaração no manifesto (campo introduzido em [#98](https://github.com/alaorneto/skeleton/issues/98)):

```json
"runtime": {
  "service_api": {
    "module": "applications.tasks.service_api",
    "classes": ["TaskService"]
  }
}
```

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

## Retorno: DTOs e páginas

- DTOs são `@dataclass(frozen=True, slots=True)` declarados em `service_api.py` (ou num módulo `service_api/dto.py`) e documentados como parte do contrato.
- Campos permitidos: UUID, `str`, `int`, `bool`, `Decimal`, `date`, `datetime` com fuso, enums de texto, tuplas e outros DTOs. Datas e horas saem em UTC com `tzinfo`. Converter para o fuso da pessoa é trabalho da superfície.
- Só entram campos que estão em `read_fields` do `TableSpec` e fora de `sensitive_fields`, salvo campo sensível documentado na seção **Autorização** do método.
- Listas usam `Page[T]` (`items`, `next_cursor`, `page_size`, máximo de 100). Nunca devolva lista ilimitada.
- Retornar instância de modelo, `QuerySet` ou `dict` mutável é violação: o kit de contrato ([#100](https://github.com/alaorneto/skeleton/issues/100)) reprova.

## Erros

Os erros públicos ficam em `skeleton.service_api.errors` ([#97](https://github.com/alaorneto/skeleton/issues/97)). Exceções internas não atravessam a fronteira:

| Erro | Quando | HTTP correspondente |
|---|---|---|
| `NotFound` | Alvo inexistente, de outra organização ou não legível | 404 |
| `Forbidden` | Ator legível, mas sem permissão para a ação; ator divergente do contexto | 403 |
| `InvalidInput(errors)` | Validação de campos; `errors` mapeia campo → mensagens | 400 |
| `Conflict(code)` | Revisão desatualizada (`code="stale_revision"`), unicidade, último admin, cota | 409 |
| `Unavailable` | App provedora desativada, indisponível ou não instalada na organização | 404 |
| `ContextRequired` | Chamada sem o contexto exigido | 500 (erro de programação) |

O consumidor trata `Unavailable` como "integração desligada": esconde a funcionalidade, sem falhar a operação principal.

## Transações, efeitos e idempotência

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

## Superfícies públicas do núcleo para apps

Uma app pode importar do núcleo apenas:

| Módulo | Uso |
|---|---|
| `skeleton.service_api` e submódulos | Base, erros, DTOs e serviços do núcleo |
| `skeleton.common.models` | Bases `SystemRecord`, `OrganizationRecord`, `SecuredRecord` |
| `skeleton.common.migration_operations` | `InstallRecordInfrastructure`, só em migrations |
| `skeleton.system.app_registry` | `AppManifest`, `register_app` em `ready()` |
| `skeleton.system.registry` | `TableSpec`, `register` em `ready()` |
| `skeleton.system.record_operations` | `RecordOperations`, `register_record_operations` |
| `skeleton.access.policies` | `authorized_queryset`, `require_record_action`, sobre modelos da própria app |
| `skeleton.tenancy.context` | `current_context` (leitura) |
| `skeleton.web.views` | `tenant_page` |
| `skeleton.api.views` | `TenantAPI` |
| `skeleton.testing` | Fixtures e kit de contrato, só em testes (a partir de [#100](https://github.com/alaorneto/skeleton/issues/100)) |

`skeleton.system.applications.application_state` continua permitido até existir o `ApplicationService` ([#105](https://github.com/alaorneto/skeleton/issues/105)). Importar outros módulos do núcleo vira aviso e depois erro do check E019.

## Catálogo planejado do núcleo

O núcleo publica a própria API de serviços em `skeleton.service_api` (epic [#113](https://github.com/alaorneto/skeleton/issues/113)). Nenhum destes serviços existe ainda. A tabela mostra o contrato planejado e a issue que o entrega.

| Serviço | Métodos principais | Issue |
|---|---|---|
| `IdentityService` | `me`, `get_person`, `update_my_preferences` | [#102](https://github.com/alaorneto/skeleton/issues/102) |
| `OrganizationService` | `current`, `my_organizations`, `members`, `groups`, `is_member` | [#103](https://github.com/alaorneto/skeleton/issues/103) |
| `AccessService` | `can`, `require`, `capabilities`, `my_groups` | [#104](https://github.com/alaorneto/skeleton/issues/104) |
| `ApplicationService` | `is_enabled`, `my_configuration` | [#105](https://github.com/alaorneto/skeleton/issues/105) |
| `AuditService` | `history` | [#106](https://github.com/alaorneto/skeleton/issues/106) |
| `NotificationService` | `notify`, `unread_count`, `inbox`, `mark_read`, `archive` | [#107](https://github.com/alaorneto/skeleton/issues/107) |
| `JobService`, `EventService` | `enqueue`, `publish` | [#108](https://github.com/alaorneto/skeleton/issues/108) |
| `FileService` | `attach`, `list`, `download_url`, `remove` | [#109](https://github.com/alaorneto/skeleton/issues/109) |

A app de agenda é a referência de app first-party com `CalendarService` ([#111](https://github.com/alaorneto/skeleton/issues/111)). Depois que esses serviços existirem, a REST e as views do núcleo passam a consumi-los ([#110](https://github.com/alaorneto/skeleton/issues/110)).

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
- **Contrato:** `assert_service_contract(ServiceClass)` do kit ([#100](https://github.com/alaorneto/skeleton/issues/100)).

Rode em SQLite e em PostgreSQL com a role runtime, como exige o [AGENTS.md](../AGENTS.md). Registre em [testing](testing.md) somente o que rodou de fato.

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
