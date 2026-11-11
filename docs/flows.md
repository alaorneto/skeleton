# Fluxos de processo de negócio

Um pedido de compra passa por quem pede, por um gestor e, acima de certo valor, pela diretoria e pelo financeiro ao mesmo tempo. Uma anotação só é publicada depois de revisada. Esses são **processos de negócio**: sequências de etapas, com bifurcações, formulários, pessoas responsáveis e aprovações. O núcleo oferece o motor desses processos em `skeleton/flows` (label `sk_flows`, epic [#209](https://github.com/alaorneto/skeleton/issues/209)); as apps declaram os fluxos e os usam pela [API de serviços](service-api.md).

**Sobre o nome.** O pedido original fala em "workflow". Neste repositório, "workflow" já é o fluxo de desenvolvimento (`docs/workflow.md`, `tools/workflow`, skill `issue-workflow`). Por isso o subsistema se chama **fluxos** (`flows`): "fluxo" é o processo declarado, "instância" é uma execução dele.

**O que o núcleo faz e o que a app faz.** O núcleo é só o motor: guarda definições, executa instâncias, distribui tarefas, valida formulários e registra o histórico. Regras de negócio (que fluxo existe, quem aprova, o que acontece ao aprovar) ficam na app, declaradas em código.

## Vocabulário

| Termo | Significado |
|---|---|
| **Fluxo** (`FlowSpec`) | A declaração de um processo: etapas, transições, formulários e regras. Tem chave (`app_label.nome`) e versão |
| **Definição** (`FlowDefinition`) | A cópia congelada de uma versão do fluxo numa organização |
| **Instância** (`FlowInstance`) | Uma execução do fluxo, ligada a um registro de negócio |
| **Registro de negócio** | A linha da app sobre a qual o processo corre (o pedido, a anotação). É dele que vem a autorização de leitura |
| **Etapa** | Um nó do fluxo: início, fim, tarefa, aprovação, serviço, temporizador, espera ou gateway |
| **Transição** | Uma seta entre duas etapas, com condição, resultado que a dispara e formulário opcionais |
| **Ativação** (`FlowStepRun`) | Uma passagem da instância por uma etapa. É a "ficha" que percorre o fluxo |
| **Tarefa** (`FlowTask`) | O trabalho de uma pessoa numa etapa de tarefa ou de aprovação |
| **Envio** (`FlowSubmission`) | Um formulário preenchido, guardado como foi enviado |
| **Histórico** (`FlowActivity`) | A lista ordenada do que aconteceu na instância: quem, quando, o quê |

## Declarar um fluxo

A app declara o fluxo no `ready()` do seu `AppConfig`, como faz com tipos de job, de evento e de notificação. Apps importam só `skeleton.flows.registry`.

```python
# applications/purchases/flows.py
from skeleton.flows import registry as flows

REQUEST = flows.Form(
    flows.Field("amount", "decimal", label="Valor", required=True, minimum=0),
    flows.Field("justification", "text", label="Justificativa", required=True, max_length=2000),
    flows.Field("bank_account", "str", label="Conta para reembolso", sensitive=True),
)

PURCHASE = flows.FlowSpec(
    key="project_purchases.approval",
    version=1,
    label="Aprovação de compra",
    owner_app="project_purchases",
    subject_table="project_purchases.purchase",
    steps=(
        flows.start("start"),
        flows.task("fill", label="Preencher o pedido", assignees=(flows.Assignment.starter(),), form=REQUEST),
        flows.exclusive("by_amount", label="Valor alto?"),
        flows.approval("manager", label="Aprovação do gestor", assignees=(flows.Assignment.role("purchase_manager"),)),
        flows.parallel("fork", join="joined"),
        flows.approval(
            "board",
            label="Diretoria",
            assignees=(flows.Assignment.group("board", expand=True),),
            policy="quorum",
            quorum=2,
        ),
        flows.approval(
            "finance",
            label="Financeiro",
            assignees=(flows.Assignment.role("finance"),),
            sla=flows.Sla(due_seconds=172800, remind_before_seconds=(86400,)),
        ),
        flows.join("joined"),
        flows.exclusive("verdict"),
        flows.service("register", label="Registrar a compra", handler="project_purchases.register"),
        flows.end("approved", outcome="approved"),
        flows.end("rejected", outcome="rejected"),
    ),
    transitions=(
        flows.Transition("start", "fill"),
        flows.Transition("fill", "by_amount", key="submit", label="Enviar"),
        flows.Transition("by_amount", "fork", condition=flows.gt("amount", 10000)),
        flows.Transition("by_amount", "manager", default=True),
        flows.Transition("manager", "register", on="approved"),
        flows.Transition("manager", "fill", on="rejected"),  # devolução a quem pediu
        flows.Transition("fork", "board"),
        flows.Transition("fork", "finance"),
        flows.Transition("board", "joined", on="approved"),
        flows.Transition("board", "joined", on="rejected"),
        flows.Transition("finance", "joined", on="approved"),
        flows.Transition("finance", "joined", on="rejected"),
        flows.Transition("joined", "verdict"),
        flows.Transition(
            "verdict",
            "register",
            condition=flows.all_of(
                flows.eq(flows.outcome("board"), "approved"), flows.eq(flows.outcome("finance"), "approved")
            ),
        ),
        flows.Transition("verdict", "rejected", default=True),
        flows.Transition("register", "approved"),
    ),
)


def ready(self):
    flows.register_flow(PURCHASE)
    flows.register_flow_handler("project_purchases.register", register_purchase, owner_app="project_purchases")
```

### Etapas

| Tipo | Construtor | O que faz | Como termina |
|---|---|---|---|
| Início | `start(key)` | Ponto de entrada; exatamente um por fluxo | Segue a única transição de saída |
| Fim | `end(key, outcome=...)` | Encerra a instância com o resultado `outcome` | — |
| Tarefa | `task(key, assignees=..., form=..., sla=..., delegable=True)` | Cria tarefa para uma pessoa ou fila | A pessoa conclui escolhendo uma transição de saída (a "ação") |
| Aprovação | `approval(key, assignees=..., policy=..., quorum=..., form=..., sla=...)` | Cria uma tarefa por vaga de aprovador | A política resolve para `approved` ou `rejected` |
| Serviço | `service(key, handler=..., outputs=..., max_attempts=3)` | Roda um handler registrado pela app, num job | O handler devolve um resultado opcional |
| Temporizador | `timer(key, delay_seconds=...)` | Espera um tempo | O job do temporizador dispara |
| Espera | `wait(key, signal=..., timeout_seconds=..., form=...)` | Espera um sinal enviado pela app | `received` ou `timeout` |
| Gateway exclusivo | `exclusive(key)` | Escolhe **uma** saída: a primeira condição verdadeira, na ordem declarada, ou a padrão | Imediato |
| Bifurcação paralela | `parallel(key, join=...)` | Ativa **todas** as saídas | Imediato |
| Bifurcação inclusiva | `inclusive(key, join=...)` | Ativa **todas as saídas cuja condição é verdadeira**; se nenhuma, a padrão | Imediato |
| Junção | `join(key)` | Espera todos os ramos que a bifurcação correspondente ativou | Quando o último ramo chega |

Subfluxos (uma etapa que inicia outro fluxo) ficam para o [ciclo 2](#o-que-fica-para-depois).

### Transições

`Transition(source, target, key="", label="", on="", condition=None, default=False, form=None)`:

- **`on`** é o resultado da etapa de origem que dispara a transição: `approved`, `rejected` e `expired` numa aprovação; `expired` numa tarefa; o resultado devolvido pelo handler num serviço; `received` e `timeout` numa espera.
- Numa **tarefa**, as transições sem `on` são as **ações** que a pessoa pode escolher, identificadas por `key` (obrigatória quando há mais de uma). Uma ação com `condition` só é oferecida quando a condição vale.
- **`form`** é o formulário da transição: os dados exigidos para seguir por ela (o motivo de uma devolução, por exemplo), além do formulário da etapa.
- Uma transição pode apontar para uma etapa anterior: é assim que uma rejeição **devolve** o processo.

### Condições

Uma condição é uma **árvore de dados**, não um texto. Os construtores são `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `is_in`, `truthy`, `all_of`, `any_of` e `negate`, sobre um campo de formulário da instância ou sobre o resultado de uma etapa (`outcome("manager")`). Os operadores são uma lista fechada, e os valores são comparados com o tipo declarado do campo (um `decimal` é comparado como `Decimal`, uma data como data).

Quando a regra não cabe numa expressão, a app registra uma função e a refere por nome: `call("project_purchases.needs_board")` com `register_flow_condition(nome, função, owner_app=...)`. A função recebe um contexto somente leitura e devolve um booleano.

Nada é avaliado com `eval`, e nenhum nome de classe ou de módulo vem de dados: a definição congelada só guarda nomes que o motor resolve nos registros em código.

### Formulários

`Form(Field(...), ...)` declara os campos aceitos. Tipos: `str`, `text`, `int`, `decimal`, `bool`, `date`, `datetime`, `choice` (com `choices`) e `user` (o UUID de uma pessoa com vínculo ativo na organização).

- **Allowlist:** um campo não declarado no formulário da etapa ou da transição é recusado, não ignorado.
- **Validação no servidor:** obrigatório, tipo, tamanho, mínimo e máximo, escolha válida, pessoa da mesma organização.
- **Sensíveis:** um campo `sensitive=True` é guardado numa coluna à parte (`sensitive_data` na instância, `sensitive_values` no envio), que a auditoria redige por inteiro. Ele não entra em eventos nem em notificações, e só é devolvido a quem pode agir numa tarefa da instância.
- **Histórico:** cada envio é uma linha `FlowSubmission` imutável. A instância guarda os valores mais recentes de cada campo (`data`), que as condições leem.
- Um mesmo nome de campo tem o mesmo tipo e a mesma sensibilidade em todos os formulários do fluxo; o check reprova a divergência.

O fluxo pode declarar `start_form`: os dados que a app informa ao iniciar a instância.

### Atribuição

A definição vale para todas as organizações, então não pode citar o UUID de um papel ou de uma pessoa. `Assignment` descreve **quem** de forma resolvível em cada organização:

| Construtor | Resolve para |
|---|---|
| `Assignment.starter()` | Quem iniciou a instância |
| `Assignment.subject_owner()` | O dono (`owner`) do registro de negócio |
| `Assignment.field("approver")` | A pessoa escolhida num campo `user` de um formulário anterior |
| `Assignment.role("finance")` | Uma fila: quem tem o papel de código `finance` na organização, direto ou por grupo |
| `Assignment.group("board")` | Uma fila: os membros do grupo de código `board` |
| `Assignment.role(..., expand=True)`, `Assignment.group(..., expand=True)` | Uma vaga para **cada** pessoa do papel ou do grupo, no momento da ativação |
| `Assignment.rule("app.nome")` | O que a regra registrada pela app devolver (pessoas, grupos, papéis) |

Uma tarefa de **fila** não tem responsável: qualquer candidato pode **assumir** (`claim`) e depois **liberar** (`release`). Se nada resolver (o papel não existe na organização, por exemplo), a tarefa nasce sem responsável e sem candidatos, o histórico registra `assignment_unresolved` e o administrador da organização a reatribui.

### Aprovações

Cada vaga de aprovador vira uma `FlowTask`, e a política decide sobre o conjunto:

| `policy` | Aprova quando | Rejeita quando |
|---|---|---|
| `any` (padrão) | A primeira decisão é de aprovar | A primeira decisão é de rejeitar |
| `all` | Todos aprovaram | Alguém rejeitou |
| `quorum` (com `quorum=n`) | `n` aprovaram | As rejeições tornam `n` impossível |
| `sequential` | Todos aprovaram, um de cada vez, na ordem declarada | Alguém rejeitou |

Resolvida a etapa, as tarefas ainda abertas são canceladas. A decisão aceita comentário e os campos do formulário da etapa. A transição `on="rejected"` decide o que a rejeição significa: ir a um fim, ou **voltar** a uma etapa anterior.

**Delegação e reatribuição.** O responsável passa a tarefa a outra pessoa com vínculo ativo (`reassign`) quando a etapa é `delegable`; o administrador da organização reatribui qualquer tarefa. A tarefa guarda quem delegou.

### Prazos (SLA)

`Sla(due_seconds, remind_before_seconds=(), on_overdue="notify", escalate_to=())`:

- a tarefa nasce com `due_at`;
- cada valor de `remind_before_seconds` agenda um lembrete (`flows.task_reminder`) ao responsável ou à fila;
- vencido o prazo, `on_overdue` decide: `notify` avisa o responsável e o escalonamento (`escalate_to`); `reassign` passa a tarefa ao escalonamento; `expire` encerra a etapa com o resultado `expired` e segue a transição `on="expired"`.

Os prazos correm em tempo corrido. Horas úteis e substituição por ausência ficam para o ciclo 2.

### Validação estrutural

O check `skeleton.E029` (em `manage.py check`, sem consultar o banco) reprova:

- chave mal formada ou registrada duas vezes com specs diferentes; versão menor que 1;
- zero ou mais de um início; nenhum fim; etapa ou transição com chave repetida; transição para etapa inexistente;
- **etapa inalcançável** a partir do início e **beco sem saída** (etapa de onde não se chega a nenhum fim);
- gateway exclusivo sem exatamente uma transição padrão ou com saída sem condição; bifurcação com menos de duas saídas;
- **bifurcação sem junção**: `join` inexistente, ramo que não chega à junção, ramo que alcança um fim ou volta à bifurcação sem passar pela junção, entrada no bloco por fora da bifurcação, junção usada por duas bifurcações ou por nenhuma;
- aprovação sem as transições `approved` e `rejected`; serviço, temporizador e espera com saídas que não cobrem seus resultados; `on_overdue="expire"` sem a transição `expired`;
- condição com operador ou campo desconhecido; condição, handler ou regra de atribuição não registrados, ou registrados por outra app;
- campo de formulário com tipo, sensibilidade ou escolhas divergentes entre formulários;
- `subject_table` sem `TableSpec`, que não seja `SecuredRecord` exposto ou que pertença a outra app; sequência de numeração não declarada.

Os blocos paralelo e inclusivo são **estruturados**: toda bifurcação nomeia a sua junção. Isso deixa a validação decidível e dá à junção inclusiva uma regra simples (esperar exatamente os ramos ativados).

## Definições versionadas

A declaração em código é a versão **publicada**. Para que uma instância em andamento não mude de regra quando a app é atualizada:

1. Ao iniciar a primeira instância de um fluxo numa organização, o motor grava a forma canônica (JSON) da spec em `FlowDefinition`, com o digest SHA-256, chave e versão.
2. A instância aponta para essa linha e é **sempre** executada a partir dela, não do código.
3. `FlowDefinition` é imutável (guard no banco). Mudar o fluxo exige incrementar `version`: as instâncias novas usam a nova definição, e as antigas terminam na que tinham.
4. Mudar a spec **sem** mudar a versão é erro de publicação: o digest do código difere do gravado e `start` falha fechado (`Conflict("definition_changed")`).

Handlers, condições e regras de atribuição são resolvidos por nome no código atual. Uma versão antiga que cite um nome removido falha naquela etapa (a instância fica `failed`, com o motivo no histórico) em vez de executar outra coisa. Mantenha os nomes registrados enquanto houver instâncias antigas.

## Modelo de dados

Seis tabelas, todas `OrganizationRecord` (UUID, organização imutável, revisão, exclusão lógica, auditoria por trigger, RLS tenant), nenhuma no gateway genérico:

| Modelo | Guarda | Campos imutáveis |
|---|---|---|
| `FlowDefinition` | `key`, `version`, `app_label`, `label`, `digest`, `specification` (JSON) | todos |
| `FlowInstance` | `definition`, `flow_key`, `flow_version`, `number`, `subject_table`, `subject_sysid`, `status`, `outcome`, `starter`, `data`, `sensitive_data`, `idempotency_key`, `activity_count`, `finished_at` | definição, chave, versão, registro de negócio, quem iniciou, chave de idempotência |
| `FlowStepRun` | `instance`, `step_key`, `kind`, `status`, `sequence`, `scope`, `branch`, `via`, `outcome`, `state` (JSON), `due_at`, `finished_at` | instância, etapa, tipo, ordem, escopo, ramo, origem |
| `FlowTask` | `instance`, `step_run`, `step_key`, `kind`, `status`, `slot`, `assignee`, `candidate_group`, `candidate_role`, `delegated_from`, `decision`, `action`, `comment`, `due_at`, `reminded_count`, `escalated_at`, `completed_by`, `completed_at` | instância, ativação, etapa, tipo, vaga |
| `FlowSubmission` | `instance`, `step_run`, `task`, `step_key`, `transition_key`, `origin`, `values`, `sensitive_values` | todos |
| `FlowActivity` | `instance`, `sequence`, `kind`, `step_key`, `step_run`, `task`, `detail` (JSON com identificadores e rótulos), `comment` | todos |

- `sensitive_data`, `sensitive_values` e `comment` são `sensitive_fields`: a auditoria guarda `[REDACTED]`.
- Há no máximo **uma instância ativa** (`running` ou `suspended`) por fluxo e registro de negócio (índice único parcial).
- Nada é apagado: o runtime não tem DELETE, e não há serviço de exclusão. Retenção e anonimização ficam para o ciclo 2.

**Estados.** Instância: `running`, `suspended`, `completed`, `cancelled`, `failed`. Ativação: `active`, `waiting` (uma junção esperando ramos), `completed`, `cancelled`, `failed`. Tarefa: `open`, `completed`, `cancelled`.

## Execução

1. **Início.** `start` valida a app dona, o registro de negócio e o `start_form`, congela a definição, cria a instância e ativa a etapa de início.
2. **Avanço.** O motor segue as transições até parar numa etapa que espera algo (tarefa, aprovação, serviço, temporizador, espera, junção incompleta) ou num fim. Gateways são resolvidos na hora.
3. **Paralelismo.** Uma bifurcação cria uma ativação por ramo. Cada uma leva o **escopo** (a ativação da bifurcação) e o **ramo** (a transição que a originou). A junção anota os ramos que chegaram e só continua quando chegaram todos os que a bifurcação ativou.
4. **Fim.** Ao chegar num fim, a instância fica `completed` com o `outcome` do fim. A validação garante que não sobram ramos ativos.

**Etapas automáticas.** Um serviço enfileira o job `flows.service` na transação do avanço. O job roda em `system_context` da organização e chama o handler com um contexto somente leitura (instância, etapa, registro de negócio, dados). O handler devolve `None`, um resultado (texto) ou `StepResult(outcome, data)`; os dados precisam estar declarados em `outputs`. Os efeitos no banco e o avanço são confirmados juntos. Efeitos externos são **pelo menos uma vez**, como em qualquer [job](jobs.md).

**Falhas.** Se o handler levanta exceção, o motor desfaz o que ele escreveu, conta a tentativa e agenda outra com espera crescente. Esgotado `max_attempts`, a instância fica `failed` e o histórico guarda a classe do erro (nunca a mensagem, que pode ter dados). `retry` reativa a etapa.

**Temporizadores e esperas.** Um temporizador é um job `flows.timer` com `run_after`. Uma espera termina quando a app envia o sinal (`signal`), que pode trazer os dados do formulário da espera; com `timeout_seconds`, um job encerra a espera com `timeout`.

**Cancelar, suspender, retomar.** `cancel` encerra as ativações e as tarefas abertas. `suspend` congela a instância: as ações de tarefa são recusadas e os jobs que dispararem não fazem nada. `resume` reagenda o que ficou pendente.

## Concorrência

- **Um bloqueio por instância.** Todo comando que muda uma instância bloqueia primeiro a linha dela (`SELECT ... FOR UPDATE`) e só depois lê ativações e tarefas. Dois ramos paralelos concluídos ao mesmo tempo são processados um depois do outro: a junção dispara exatamente uma vez. A ordem de bloqueio é sempre instância → tarefa.
- **CAS.** Ações de tarefa recebem a revisão da tarefa; `cancel`, `suspend`, `resume` e `retry`, a da instância. A revisão é comparada depois do bloqueio. Revisão antiga é `Conflict("stale_revision")`; o serviço não tenta de novo.
- **Um vencedor.** Dois atores que agem na mesma tarefa leram a mesma revisão: o primeiro vence, o segundo recebe o conflito.
- **Idempotência.** `start` aceita `idempotency_key`: a repetição devolve a mesma instância (`created=False`). Os jobs do subsistema usam chaves de idempotência e conferem o estado antes de agir, então uma execução repetida não avança duas vezes.

## Autorização

Tudo é negado por padrão, e cada chamada revalida no servidor.

| Operação | Quem pode |
|---|---|
| Iniciar (`start`), enviar sinal (`signal`) | O código da **app dona** do fluxo, para um ator com a ação `start_action` (padrão `update`) sobre o registro de negócio; em `system_context`, sem ator (jobs e consumidores de eventos da app) |
| Ler a instância e o histórico | Quem lê o registro de negócio (`authorized_queryset`), quem iniciou, quem tem ou teve tarefa na instância, quem é candidato a uma tarefa aberta, e o administrador da organização |
| Ler e agir numa tarefa | O responsável; para tarefa de fila, quem é do grupo ou tem o papel candidato **no momento da chamada** |
| Reatribuir | O responsável (etapa `delegable`) ou o administrador da organização |
| Cancelar | O administrador da organização, ou quem iniciou quando o fluxo permite (`starter_can_cancel`) |
| Suspender, retomar, repetir | O administrador da organização |

- **App desativada bloqueia.** Com a app dona desativada ou indisponível na organização, toda operação sobre os fluxos dela responde `Unavailable` e as listas omitem as tarefas e instâncias dela.
- **Inexistente e não autorizado respondem igual** (`NotFound`), inclusive para outra organização.
- **Revogação imediata.** Quem perde o papel ou sai do grupo deixa de ver a fila na chamada seguinte; não há cache.
- **Ator explícito.** Todo método recebe `actor`, conferido contra o contexto.
- O motor nunca abre `system_context` para ampliar o que o ator pode; os jobs do subsistema rodam como sistema porque não agem em nome de ninguém.

**Decisão: autorização derivada, sem `TablePermission` própria.** *Contexto:* as tabelas de fluxos não são `SecuredRecord` expostos, então não recebem `TablePermission`. *Decisão:* a leitura de uma instância deriva do registro de negócio e da participação; a ação numa tarefa deriva da atribuição. *Consequência:* um aprovador que não lê o registro de negócio ainda vê a instância e os dados dos formulários dela, porque precisa deles para decidir; ele não ganha acesso ao registro em si.

## Integrações

| Subsistema | Uso |
|---|---|
| [Jobs](jobs.md) | `flows.service`, `flows.timer`, `flows.wait_timeout`, `flows.task_reminder` e `flows.task_due`, todos como sistema, por organização, com payload só de identificadores |
| [Eventos](events.md) | `flows.instance_started`, `flows.instance_completed`, `flows.instance_cancelled` e `flows.instance_failed` sobre `sk_flows.flowinstance`, com `flow_key`, `flow_version`, `status`, `outcome`, `subject_table` e `subject_sysid` |
| [Webhooks](webhooks.md) | O fluxo pode mapear momentos (`started`, `completed`, `cancelled`, `failed`) para tipos de evento **da app, sobre o registro de negócio** (`subject_events`). Como o alvo é um `SecuredRecord` exposto, a entrega de webhook reautoriza a leitura pelo principal do endpoint, como em qualquer evento |
| [Notificações](notifications.md) | `flows.task_assigned`, `flows.task_reminder` e `flows.task_overdue`, com o rótulo do fluxo e da etapa. Abrir a notificação reautoriza o acesso à tarefa |
| [Auditoria](audit-soft-delete.md) | Triggers em todas as tabelas; o histórico (`FlowActivity`) é a narrativa do processo, a auditoria continua a fonte de verdade de cada escrita |
| [Sequências](sequences.md) | `number_sequence` numera as instâncias (`PC-2026-0042`) sem lacunas, na transação do início |

Os eventos do núcleo sobre a instância não vão para webhooks: a tabela não tem autorização por registro que o principal de um endpoint possa satisfazer.

## Superfícies

- **API de serviços** (`skeleton.service_api.flows.FlowService`): o contrato principal. Veja o [catálogo do núcleo](service-api.md#flowservice).
- **REST** em `/api/v1/o/<org>/flows/`: tarefas, instâncias, histórico e as ações, com `If-Match`. Veja [API](api.md#fluxos-de-processo).
- **Páginas** em `/o/<org>/flows/`: caixa de tarefas, tarefa, instâncias e instância com histórico, sem JavaScript.

Iniciar uma instância não tem rota no núcleo: é uma operação de negócio da app dona, que chama `FlowService().start(...)` da sua própria view ou serviço.

## O que entra na primeira entrega

Tudo o que está descrito acima: declaração em código com validação estrutural; etapas de início, fim, tarefa, aprovação, serviço, temporizador e espera; gateways exclusivo, paralelo e inclusivo; formulários por etapa e por transição; aprovações `any`, `all`, `quorum` e `sequential`; delegação, devolução, comentários, prazos, lembretes e escalonamento; atribuição por pessoa, papel, grupo ou regra; caixa de tarefas; vínculo com o registro de negócio; histórico; cancelar, suspender, retomar e repetir; jobs, eventos, webhooks, notificações, auditoria e sequências; `FlowService`, REST e páginas; e o fluxo de exemplo no Notes.

## O que fica para depois

Avaliado e deixado para o epic [#208](https://github.com/alaorneto/skeleton/issues/208), com o motivo:

| Item | Por que não agora |
|---|---|
| Subfluxos | Exigem fixar a versão do fluxo filho e propagar cancelamento, suspensão e falha entre instâncias |
| Definições em dados por organização, com editor | A tabela já é por organização; falta decidir quem publica e como validar na publicação. Fluxos em código cobrem o pedido e seguem o padrão dos outros subsistemas |
| Caixa de tarefas entre organizações e contador no cabeçalho | Exige autodescoberta em `FlowTask` e leitura agregada |
| Capacidade `flows.manage` e painel de operação | Mudar a lista de capacidades afeta papéis internos de todas as organizações; por ora só o administrador administra fluxos |
| Espera por tipo de evento de domínio | O sinal explícito, enviado pelo consumidor de eventos da app, cobre o caso sem acoplar o motor ao outbox |
| Token de API nas rotas de fluxos | Exige escopos próprios; a primeira entrega só aceita sessão |
| SLA em horas úteis e substituição por ausência | Dependem de um calendário da organização que ainda não existe |
| Migrar instâncias para outra versão | Precisa de um mapa de etapas entre versões e de regras para tarefas abertas |
| Diagrama do fluxo e métricas | Apresentação e operação, sem efeito no contrato |
| Comentários livres na instância | Serão os comentários de registros (#187) |
| Retenção e anonimização dos dados de formulário | Faz parte do epic de LGPD (#88) |

## Decisões que pedem confirmação do responsável

1. **Nome** `skeleton/flows` / `sk_flows` / "fluxos", em vez de "workflow". Renomear depois de existir migration custa uma migração de dados.
2. **Fluxos só em código** na primeira entrega (sem editor nem definição em dados).
3. **Registro de negócio obrigatório**: todo fluxo corre sobre um `SecuredRecord` exposto da app dona, e a leitura da instância deriva dele.
4. **Uma instância ativa por fluxo e registro.**
5. **Quem administra fluxos é o administrador da organização**; não há capacidade delegável ainda.
6. **Um aprovador vê os dados de formulário da instância mesmo sem ler o registro de negócio.**
7. **Nada é apagado**: sem retenção na primeira entrega.
8. **A versão do core não muda neste epic**; os métodos novos declaram `since="0.6.0"`.
