# Sequências

Um código comercial como `AT-2026-0042` não deve ser confundido com o `sysid` do registro ([conceitos](concepts.md#identificadores-uuid-chave-primária-e-código-comercial)): ele segue regra de negócio, pertence à organização e frequentemente recomeça a cada período. O núcleo oferece sequências **sem lacunas** em `skeleton/sequences` (label `sk_sequences`).

## Declarar

Cada sequência é registrada em código durante a inicialização da app (`ready()`), usando `skeleton.sequences.registry`, que é superfície pública do núcleo; a geração de números ocorre sempre pelo `SequenceService`:

```python
# apps.py da app
from skeleton.sequences.registry import SequenceSpec, register_sequence


def ready(self):
    register_sequence(
        SequenceSpec("tickets.number", period="year", template="AT-{year}-{number:04d}", owner_app="tickets")
    )
```

| Campo | Valores |
|---|---|
| `key` | `app_label.nome`, com até 100 caracteres |
| `period` | `year` (reinicia a cada ano, padrão), `month` (a cada mês) ou `none` (nunca reinicia); calendário em UTC |
| `template` | Formato `str.format` com `{number}` obrigatório e, opcionalmente, `{year}`, `{month}` e `{period}` |
| `owner_app` | App autorizada a numerar com essa sequência pela API de serviços; sem `owner_app`, nenhum chamador consegue numerar pelo serviço |

O check `skeleton.E028` reprova chave mal formada, período desconhecido, template sem `{number}` ou com marcador desconhecido, além de declarações duplicadas com especificações diferentes.

## Numerar

Apps geram números por meio do `SequenceService` em `skeleton.service_api.sequences` ([API de serviços](service-api.md#sequenceservice)):

```python
from skeleton.service_api.sequences import SequenceService

number = SequenceService().next_number(actor=request.user, key="tickets.number")
ticket.code = number.formatted  # "AT-2026-0042"; number.number == 42, number.period == "2026"
```

No [portal](portal.md#superfícies-do-núcleo-para-os-recursos-da-própria-app-741), a app pode numerar com a própria sequência dentro de um método `allow_external`, passando a pessoa externa como `actor`: o contador é auditado com essa pessoa e `source="http:portal"`, e um rollback da requisição devolve o número. Fora de um método `allow_external`, a chamada responde `Forbidden("external_not_allowed")`.

A função interna do núcleo é `skeleton.sequences.services.next_number(key, *, at=None)`. Tanto o serviço quanto a função interna exigem contexto de tenant; o parâmetro opcional `at` (com fuso horário, padrão agora) determina o período usado.

Um consumidor de evento ou um job por organização pode numerar passando `actor=None` dentro do `system_context` da organização (desde a versão 0.2.0). A regra de propriedade não muda: o código chamador precisa pertencer à app dona da sequência, e o número sai da organização do contexto, na transação do consumidor e sem lacunas. Um `system_context` sem organização responde `ContextRequired`. Veja um exemplo em [eventos](events.md#reagir-numerando-e-abrindo-um-título).

## Garantias

- **Sem lacunas.** O contador de `(organização, chave, período)` é uma linha `SequenceCounter` bloqueada com `SELECT … FOR UPDATE` e incrementada **na transação do chamador**. Se a escrita de negócio sofrer rollback, o incremento é desfeito junto e a próxima alocação reutiliza o mesmo número.
- **Sem duplicatas.** Alocações concorrentes na mesma sequência e período aguardam o bloqueio e recebem números consecutivos. A criação inicial do contador de um período serializa na linha da organização com `FOR NO KEY UPDATE` (que não conflita com inserções já realizadas na transação), impedindo que duas transações criem o mesmo contador.
- **Isolamento.** Cada organização e período mantém seu próprio contador protegido por RLS de tenant: a organização B nunca visualiza nem avança o contador da organização A.
- **Auditoria.** `SequenceCounter` é um `OrganizationRecord` com a infraestrutura padrão, registrando um evento de auditoria a cada incremento na mesma transação.

**Custo operacional.** Como escritas que numeram na mesma sequência e período dentro de uma organização ficam serializadas até o commit, mantenha essas transações curtas. Não há tela nem API para editar ou reiniciar contadores.

## Campos preenchidos pelo gateway (#156)

Uma tabela de negócio exposta pode declarar que um de seus campos é preenchido automaticamente por uma sequência:

```python
TableSpec(
    model=Ticket,
    read_fields=("code", "title"),
    write_fields=("title",),
    sequence_fields=(("code", "tickets.number"),),
    expose_api=True,
)
```

- Na criação genérica (`/o/<org>/tables/<tabela>/new/` e `POST /api/v1/o/<org>/tables/<tabela>/records/`), o gateway preenche o campo com `next_number(...).formatted` após a autorização e validação dos campos enviados, na mesma transação do registro. Qualquer recusa — inclusive na validação do modelo ao salvar — desfaz o incremento sem consumir número.
- O campo não aparece no formulário de criação e não pode ser gravado manualmente: enviá-lo na criação ou na edição retorna 400, como qualquer campo fora de `write_fields`. Ele fica disponível apenas para leitura na página de detalhe e na API.
- O check `skeleton.E028` reprova campo inexistente, campo listado em `write_fields` ou referência a sequência não declarada.
