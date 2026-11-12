# Sequências

Um código comercial como `AT-2026-0042` não é o `sysid` do registro ([conceitos](concepts.md#identificadores-uuid-chave-primária-e-código-comercial)): ele tem regra de negócio, é por organização e muitas vezes recomeça a cada ano. O núcleo oferece sequências **sem lacunas** em `skeleton/sequences` (label `sk_sequences`), parte do epic #89.

## Declarar

```python
# apps.py da app
from skeleton.sequences.registry import SequenceSpec, register_sequence


def ready(self):
    register_sequence(
        SequenceSpec("tickets.number", period="year", template="AT-{year}-{number:04d}", owner_app="tickets")
    )
```

`skeleton.sequences.registry` é superfície pública do núcleo para registrar em `ready()`; numerar é sempre pelo `SequenceService`.

| Campo | Valores |
|---|---|
| `key` | `app_label.nome`, até 100 caracteres |
| `period` | `year` (recomeça a cada ano, padrão), `month` (a cada mês) ou `none` (nunca); calendário em UTC |
| `template` | `str.format` com `{number}` obrigatório e, opcionalmente, `{year}`, `{month}` e `{period}` |
| `owner_app` | A app que pode numerar com a sequência pela API de serviços |

O check `skeleton.E028` reprova chave mal formada, período desconhecido, modelo sem `{number}` ou com campo desconhecido, e declaração duplicada diferente.

## Numerar

Apps usam `SequenceService` de `skeleton.service_api.sequences` ([API de serviços](service-api.md#sequenceservice)):

```python
from skeleton.service_api.sequences import SequenceService

number = SequenceService().next_number(actor=request.user, key="tickets.number")
ticket.code = number.formatted  # "AT-2026-0042"; number.number == 42, number.period == "2026"
```

A função interna do núcleo é `skeleton.sequences.services.next_number(key, *, at=None)`. As duas exigem contexto tenant; `at` (com fuso, padrão agora) escolhe o período.

## Garantias

- **Sem lacunas:** o contador de `(organização, chave, período)` é uma linha `SequenceCounter` bloqueada com `SELECT … FOR UPDATE` e incrementada **na transação do chamador**. Se a escrita do negócio é desfeita, o incremento também é, e a próxima alocação reutiliza o número.
- **Sem duplicatas:** alocações concorrentes da mesma sequência e período esperam o bloqueio e recebem números consecutivos. A primeira criação do contador é serializada no bloqueio da organização, de modo que duas transações nunca criam o mesmo contador.
- **Isolamento:** cada organização e período tem seu contador, protegido por RLS tenant; a organização B não vê nem avança o contador de A.
- **Auditoria:** `SequenceCounter` é um `OrganizationRecord` com a infraestrutura padrão: cada incremento gera um evento de auditoria na mesma transação.

**Custo:** as escritas que numeram com a mesma sequência e período na mesma organização são serializadas até o commit. Mantenha curtas as transações que numeram. Não há tela nem API para editar ou reiniciar um contador.

## Campos preenchidos pelo gateway (#156)

Uma tabela de negócio exposta pode declarar que um campo vem de uma sequência:

```python
TableSpec(
    model=Ticket,
    read_fields=("code", "title"),
    write_fields=("title",),
    sequence_fields=(("code", "tickets.number"),),
    expose_api=True,
)
```

- A criação genérica (`/o/<org>/tables/<tabela>/new/` e `POST /api/v1/o/<org>/tables/<tabela>/records/`) preenche o campo com `next_number(...).formatted`, depois da autorização e da validação dos campos enviados e na mesma transação do registro. Uma criação recusada, inclusive pela validação do modelo ao salvar, desfaz o incremento e não consome número.
- O campo não aparece no formulário de criação e não é gravável: enviá-lo na criação ou na alteração responde 400, como qualquer campo fora de `write_fields`. Ele aparece só para leitura no detalhe e na API.
- O check `skeleton.E028` reprova campo inexistente, campo que está em `write_fields` e sequência não declarada.
