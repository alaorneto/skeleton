# Fluxos de processo de negócio

Um pedido de compra pode passar pelo solicitante, por um gestor e, acima de certo valor, pela diretoria e pelo financeiro em paralelo; uma anotação só é publicada após revisão. Esses são **processos de negócio**: sequências de etapas com bifurcações, formulários, responsáveis e aprovações. O núcleo oferece o motor desses processos em `skeleton/flows` (label `sk_flows`, epic [#209](https://github.com/alaorneto/skeleton/issues/209)); as apps declaram os fluxos e os operam pela [API de serviços](service-api.md).

**Sobre o nome.** Embora o conceito seja frequentemente chamado de "workflow", neste repositório esse termo já designa o fluxo de desenvolvimento (`docs/workflow.md`, `tools/workflow` e skill `dev-workflow`). Por isso, o subsistema adota **fluxos** (`flows`): "fluxo" é o processo declarado, e "instância" é uma execução específica dele.

**Divisão de responsabilidades.** O núcleo atua exclusivamente como motor: armazena definições, executa instâncias, distribui tarefas, valida formulários e registra o histórico. As regras de negócio — quais fluxos existem, quem aprova e o que ocorre após uma decisão — pertencem à app e são declaradas em código.

## Vocabulário

| Termo | Significado |
|---|---|
| **Fluxo** (`FlowSpec`) | Declaração de um processo: etapas, transições, formulários e regras, identificados por chave (`app_label.nome`) e versão |
| **Definição** (`FlowDefinition`) | Cópia congelada de uma versão do fluxo dentro de uma organização |
| **Instância** (`FlowInstance`) | Execução de um fluxo vinculada a um registro de negócio |
| **Registro de negócio** | Registro da app sobre o qual o processo corre (como o pedido ou a anotação); dele deriva a autorização de leitura |
| **Etapa** | Nó do fluxo: início, fim, tarefa, aprovação, serviço, temporizador, subfluxo, espera ou gateway |
| **Transição** | Seta entre duas etapas, com condição, resultado disparador (`on`) e formulário opcionais |
| **Ativação** (`FlowStepRun`) | Passagem da instância por uma etapa; funciona como a "ficha" que percorre o fluxo |
| **Tarefa** (`FlowTask`) | Trabalho atribuído a uma pessoa ou fila em uma etapa de tarefa ou aprovação |
| **Envio** (`FlowSubmission`) | Formulário preenchido, armazenado exatamente como foi enviado |
| **Histórico** (`FlowActivity`) | Registro ordenado do que aconteceu na instância: quem, quando e o quê |

## Declarar um fluxo

A app registra seus fluxos no `ready()` do seu `AppConfig`, seguindo o mesmo padrão de jobs, eventos e notificações. Apps importam apenas `skeleton.flows.registry`:

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
| Serviço | `service(key, handler=..., outputs=..., max_attempts=3)` | Executa um handler registrado pela app em um job | O handler devolve um resultado opcional |
| Temporizador | `timer(key, delay_seconds=...)` | Aguarda um intervalo de tempo | O job do temporizador dispara |
| Subfluxo | `subflow(key, flow="app.filho", version=N)` | Inicia o fluxo filho na versão fixada sobre o mesmo registro e aguarda sua conclusão ([#412](https://github.com/alaorneto/skeleton/issues/412)) | Resultado (`outcome`) do fluxo filho |
| Espera | `wait(key, signal=..., timeout_seconds=..., form=...)` ou `wait(key, event=..., correlate=...)` | Aguarda um sinal enviado pela app ou um evento de domínio da app ([#411](https://github.com/alaorneto/skeleton/issues/411)) | `received` ou `timeout` |
| Gateway exclusivo | `exclusive(key)` | Escolhe **uma** saída: a primeira condição verdadeira na ordem declarada, ou a saída padrão | Imediato |
| Bifurcação paralela | `parallel(key, join=...)` | Ativa **todas** as saídas simultaneamente | Imediato |
| Bifurcação inclusiva | `inclusive(key, join=...)` | Ativa **todas as saídas com condição verdadeira**; se nenhuma for verdadeira, ativa a padrão | Imediato |
| Junção | `join(key)` | Aguarda todos os ramos ativados pela bifurcação correspondente | Quando o último ramo chega |

**Espera por evento ([#411](https://github.com/alaorneto/skeleton/issues/411)).** `wait(key, event="app.tipo")` avança quando um evento de domínio desse tipo é publicado sobre o registro de negócio da instância. Com `correlate="campo"`, o fluxo avança quando o campo declarado no evento carrega o `sysid` do registro, permitindo correlacionar eventos de outra tabela. O tipo precisa pertencer à própria app e, sem `correlate`, estar vinculado à tabela do registro; esperas por evento não aceitam formulário e suportam `timeout_seconds` como as de sinal. A entrega é feita pelo consumidor do núcleo `flows.event_waits`, que recebe apenas eventos das apps no contexto da organização de origem: eventos de outra organização nunca avançam a instância, e entregas repetidas encontram a espera concluída e são ignoradas. Os campos novos ficam fora da forma canônica quando vazios, preservando o digest de fluxos já publicados.

**Subfluxos ([#412](https://github.com/alaorneto/skeleton/issues/412)).** A etapa `subflow` inicia o fluxo filho na versão fixada na spec (`version=N`), sobre o mesmo registro de negócio: publicar uma nova versão do filho não altera instâncias pai em andamento. O filho é criado dentro do comando do pai (ordem de bloqueio: pai, depois filho) e armazena `parent` e `parent_run` de forma imutável. A conclusão do filho é comunicada ao pai por um job (`flows.subflow_finished`): se concluído, a etapa segue a transição correspondente ao resultado (`on="approved"`); se falhar ou for cancelado externamente, a etapa do pai falha. Cancelar, suspender ou retomar o pai propaga a operação para o filho. O check estrutural exige que o filho esteja declarado em código pela mesma app, sobre a mesma tabela, com uma transição para cada resultado possível e sem ciclos entre fluxos (`subflow cycle`). Filhos definidos em dados ou pertencentes a outras apps não são suportados.

### Transições

`Transition(source, target, key="", label="", on="", condition=None, default=False, form=None)`:

- **`on`** indica o resultado da etapa de origem que dispara a transição: `approved`, `rejected` ou `expired` em aprovações; `expired` em tarefas; o resultado devolvido pelo handler em serviços; `received` ou `timeout` em esperas.
- Em uma **tarefa**, as transições sem `on` representam as **ações** disponíveis para a pessoa, identificadas por `key` (obrigatória quando há mais de uma ação). Uma ação com `condition` só é oferecida quando a condição é verdadeira.
- **`form`** define o formulário específico da transição — dados exigidos apenas para aquele caminho (como o motivo de uma desistência), somados ao formulário da etapa. O formulário da etapa aplica-se a todas as suas ações; o da transição, apenas àquela ação.
- Uma transição pode apontar para uma etapa anterior, permitindo **devolver** o processo após uma rejeição.

### Condições

Condições são expressas como **árvores de dados**, nunca como texto livre. Os construtores são `eq`, `ne`, `gt`, `ge`, `lt`, `le`, `is_in`, `truthy`, `all_of`, `any_of` e `negate`, operando sobre campos de formulário da instância ou sobre o resultado de uma etapa (`outcome("manager")`). A lista de operadores é fechada, e as comparações respeitam o tipo declarado do campo (`decimal` como `Decimal`, datas como data, e assim por diante).

Quando a lógica não cabe em uma árvore de expressões, a app registra uma função e a referencia pelo nome: `call("project_purchases.needs_board")`, registrada com `register_flow_condition(nome, função, owner_app=...)`. A função recebe um contexto somente leitura e devolve um booleano.

Expressões não podem comparar um campo sensível com literais (pois o literal ficaria gravado na definição congelada): apenas `truthy` aceita campos sensíveis, e comparações mais complexas devem usar uma condição registrada. A avaliação falha fechada na raiz: qualquer nó inesperado torna a árvore inteira falsa, mesmo sob `negate`.

Nada é avaliado com `eval`, e nenhum nome de classe ou módulo vem de dados: a definição congelada guarda apenas nomes que o motor resolve nos registros em código.

Handlers, condições e regras de atribuição recebem um `FlowContext` somente leitura contendo organização, instância, fluxo e versão, etapa, registro de negócio, quem iniciou, dados, dados sensíveis e resultados das etapas. Esse contexto não concede acesso: a função reautoriza o que precisar ler. Um handler retorna `None`, um texto (o resultado) ou `StepResult(outcome, data)`; uma regra de atribuição retorna `Assignees(users, groups, roles)`.

### Formulários

`Form(Field(...), ...)` declara os campos aceitos em uma etapa ou transição. Os tipos suportados são `str`, `text`, `int`, `decimal`, `bool`, `date`, `datetime`, `choice` (com `choices`) e `user` (UUID de um membro ativo na organização). O fluxo também pode declarar `start_form` para os dados fornecidos pela app ao iniciar a instância.

Na interface da tarefa, campos `datetime` usam um seletor `datetime-local` no fuso horário da pessoa (o serviço recebe ISO 8601 com deslocamento), campos `user` exibem a lista de membros ativos (`OrganizationService.members`, até 200 pessoas; acima disso, ou se a consulta for recusada, o campo volta a ser um input de texto para o identificador) e campos obrigatórios recebem o asterisco `*` renderizado pela página, não no rótulo. Essa lista serve apenas para apresentação: o serviço continua validando a pessoa no servidor.

- **Allowlist.** Qualquer campo não declarado no formulário da etapa ou da transição é recusado, nunca ignorado. O `help_text` dos campos é texto simples, sempre escapado no HTML.
- **Validação no servidor.** Verifica obrigatoriedade, tipo, tamanho, mínimo e máximo, opção válida e vínculo da pessoa na organização.
- **Campos sensíveis.** Campos marcados com `sensitive=True` são gravados em colunas separadas (`sensitive_data` na instância e `sensitive_values` no envio), integralmente redigidas na auditoria. Eles nunca entram em eventos nem em notificações e só são exibidos a quem pode agir em uma tarefa da instância.
- **Histórico de envios.** Cada preenchimento gera uma linha `FlowSubmission`, imutável exceto pelos valores, que só podem ser alterados pela retenção ou pela eliminação de titular. A instância mantém os valores mais recentes de cada campo (`data`), usados na avaliação de condições.
- **Campos pessoais.** Um campo `personal=True` identifica dado pessoal de quem o enviou. Na eliminação da pessoa, o valor é removido dos envios dela e dos dados da instância que ainda o conservem ([#422](https://github.com/alaorneto/skeleton/issues/422)).
- **Auditoria.** Os valores (`data` da instância e `values` do envio) são tratados como campos pessoais nas tabelas e aparecem como `[REDACTED]` nos eventos de auditoria, registrando que houve mudança sem expor o conteúdo.
- **Consistência.** Um mesmo nome de campo precisa manter o mesmo tipo, a mesma flag de sensibilidade e a mesma marca de dado pessoal em todos os formulários do fluxo; divergências falham no check estrutural.

### Retenção dos dados de formulário

`FlowSpec.retention_days` define por quantos dias os valores de formulário de uma instância encerrada (concluída ou cancelada) são mantidos; o valor `0` herda `SKELETON_FLOWS_FORM_RETENTION_DAYS` (cujo padrão `0` mantém os dados indefinidamente). O job diário `flows.purge_form_data` (por organização) limpa `data` e `sensitive_data` da instância, além de `values` e `sensitive_values` de cada envio, marcando `form_purged_at`.

Etapas, tarefas, decisões, datas, resultado e histórico permanecem intactos. Instâncias em andamento, suspensas ou com falha nunca são tocadas, e reexecutar o job é idempotente. O prazo aplicado é sempre o da versão congelada da instância, e esses campos ficam fora da forma canônica enquanto estão no padrão, preservando o digest de versões já publicadas.

### Atribuição

Como a definição de um fluxo vale para todas as organizações, ela não referencia UUIDs fixos de pessoas ou papéis. `Assignment` descreve **quem** deve agir de forma resolvida dinamicamente em cada organização:

| Construtor | Resolve para |
|---|---|
| `Assignment.starter()` | Quem iniciou a instância |
| `Assignment.subject_owner()` | O dono (`owner`) do registro de negócio |
| `Assignment.field("approver")` | A pessoa selecionada em um campo `user` de um formulário anterior |
| `Assignment.role("finance")` | Uma fila com quem possui o papel de código `finance` na organização (diretamente ou via grupo) |
| `Assignment.group("board")` | Uma fila com os membros do grupo de código `board` |
| `Assignment.role(..., expand=True)`, `Assignment.group(..., expand=True)` | Uma vaga individual para **cada** pessoa do papel ou grupo no momento da ativação |
| `Assignment.rule("app.nome")` | O conjunto devolvido pela regra registrada pela app (pessoas, grupos ou papéis) |

Tarefas de **fila** nascem sem responsável: qualquer candidato pode **assumir** (`claim`) e depois **liberar** (`release`). Se a atribuição não resolver ninguém (por exemplo, um papel inexistente na organização), a tarefa nasce sem responsável e sem candidatos, o histórico registra `assignment_unresolved`, e quem administra fluxos pode reatribuí-la.

### Aprovações

Cada vaga de aprovador gera uma `FlowTask`, e a política da etapa decide o desfecho do conjunto:

| `policy` | Aprova quando | Rejeita quando |
|---|---|---|
| `any` (padrão) | A primeira decisão é de aprovação | A primeira decisão é de rejeição |
| `all` | Todos aprovaram | Qualquer aprovador rejeitou |
| `quorum` (com `quorum=n`) | `n` aprovadores aprovaram | As rejeições tornam impossível atingir `n` aprovações |
| `sequential` | Todos aprovaram, um de cada vez, na ordem declarada | Qualquer aprovador rejeitou |

Assim que a etapa é resolvida, as demais tarefas abertas são canceladas. Cada decisão aceita comentário e campos do formulário da etapa. A transição `on="rejected"` define o efeito da rejeição: encerrar a instância ou **devolver** o processo para uma etapa anterior.

**Delegação e reatribuição.** O responsável atual pode transferir a tarefa para outro membro ativo (`reassign`) quando a etapa tem `delegable=True`; quem administra fluxos pode reatribuir qualquer tarefa. A tarefa registra quem realizou a delegação.

**Substituição por ausência ([#418](https://github.com/alaorneto/skeleton/issues/418)).** Cada pessoa pode cadastrar seu período de ausência e um substituto (`FlowAbsence`, via REST em `GET` e `POST /api/v1/o/<org>/flows/absences/` e `DELETE .../absences/<id>/` com `If-Match`); quem administra fluxos pode registrar ausências para qualquer membro. Pessoa e substituto devem ser membros humanos ativos e distintos, com período máximo de um ano. Durante a ausência, novas tarefas atribuídas à pessoa nascem atribuídas ao substituto, com `delegated_from` apontando para o titular (registrado no histórico como `absence_delegated`). Se o substituto não puder ler o registro de negócio, a tarefa permanece com o titular e o histórico registra `absence_substitute_refused`. A substituição afeta apenas tarefas criadas durante o período — tarefas já abertas e tarefas de fila não são alteradas.

### Prazos (SLA)

`Sla(due_seconds, remind_before_seconds=(), on_overdue="notify", escalate_to=(), business_hours=False)`:

- a tarefa nasce com o prazo `due_at`;
- cada valor em `remind_before_seconds` agenda um lembrete (`flows.task_reminder`) para o responsável ou para a fila;
- ao vencer o prazo, `on_overdue` define a ação: `notify` avisa o responsável e o escalonamento (`escalate_to`); `reassign` transfere a tarefa ao escalonamento; `expire` encerra a etapa com o resultado `expired` e segue pela transição `on="expired"`.

Os prazos podem correr em tempo corrido ou em **horas úteis** com `business_hours=True` ([#417](https://github.com/alaorneto/skeleton/issues/417)). Em horas úteis, o vencimento soma `due_seconds` de expediente conforme o [calendário da organização](data-model.md), e cada lembrete dispara quando resta a quantidade correspondente de expediente a partir da criação da tarefa. Se a organização não tiver calendário configurado, o cálculo recua para tempo corrido e registra `sla_elapsed_fallback` no histórico (pois um check estático não enxerga o calendário de cada tenant). Alterações no calendário passam a valer para tarefas criadas depois. O campo `business_hours` fica fora da forma canônica enquanto falso, mantendo inalterado o digest de versões já publicadas. Após uma suspensão, o job de vencimento é reagendado, mas lembretes que venceram durante a pausa não são reenviados.

### Validação estrutural

O check `skeleton.E029` (executado em `manage.py check`, sem consultar o banco) reprova:

- chave mal formada, registro duplicado com specs diferentes ou versão menor que 1;
- ausência ou duplicidade de etapa de início, ausência de etapa de fim, chave repetida de etapa ou transição, ou transição apontando para etapa inexistente;
- **etapa inalcançável** a partir do início ou **beco sem saída** (etapa incapaz de alcançar qualquer fim);
- gateway exclusivo sem exatamente uma transição padrão ou com saída sem condição; bifurcação com menos de duas saídas;
- **bifurcação sem junção estruturada**: `join` inexistente, ramo que não alcança a junção, ramo que termina ou retorna à bifurcação sem passar pela junção, entrada no bloco por fora da bifurcação, ou junção referenciada por mais de uma bifurcação ou por nenhuma;
- aprovação sem as transições `approved` e `rejected`; serviço, temporizador ou espera cujas saídas não cobrem seus resultados; `on_overdue="expire"` sem transição `expired`;
- condição com operador ou campo desconhecido; condição, handler ou regra de atribuição não registrados ou pertencentes a outra app;
- campo de formulário com tipo, sensibilidade ou escolhas divergentes entre formulários do mesmo fluxo;
- `subject_table` sem `TableSpec`, que não seja `SecuredRecord` exposto ou pertença a outra app; sequência de numeração não declarada.

Blocos paralelos e inclusivos são sempre **estruturados** (toda bifurcação declara explicitamente sua junção), o que torna a validação decidível e dá à junção inclusiva uma regra clara: aguardar exatamente os ramos que foram ativados.

## Definições versionadas

A declaração em código representa a versão **publicada**. Para evitar que atualizações da app alterem regras de instâncias já em andamento:

1. Ao iniciar a primeira instância de um fluxo em uma organização, o motor grava a forma canônica (JSON) da spec em `FlowDefinition`, junto com o digest SHA-256, chave e versão.
2. A instância referencia essa linha e é executada **sempre** a partir dela, nunca diretamente do código.
3. `FlowDefinition` é imutável (protegida por guard no banco). Alterar o fluxo exige incrementar `version`: novas instâncias usam a nova versão, enquanto as antigas concluem na versão original.
4. Alterar a spec **sem** incrementar `version` é tratado como erro de publicação: o digest do código diverge do gravado e `start` falha fechado (`Conflict("definition_changed")`).

### Migrar instâncias para uma versão nova

Por padrão, toda instância termina na versão em que começou. Quando é necessário corrigir um fluxo publicado e levar instâncias abertas para a nova versão ([#413](https://github.com/alaorneto/skeleton/issues/413)), a app declara em código o mapeamento entre etapas antigas e novas:

```python
from skeleton.flows.registry import register_flow_migration

register_flow_migration(
    "tasks.approval", from_version=1, to_version=2, steps={"fill": "draft", "review": "review"}, owner_app="tasks"
)
```

- `to_version` deve ser a versão declarada em código, e cada etapa de destino precisa existir nela (validado por `skeleton.E029`).
- Quem administra fluxos consulta a prévia em `GET /api/v1/o/<org>/flows/migrations/<chave>/<versão>/` (mostrando o destino de cada instância em andamento ou suspensa sem gravar nada) e aplica a migração via `POST` na mesma rota, enfileirando um job na organização.
- Para cada etapa aberta: se mapeada para a mesma chave, a ativação e as tarefas são preservadas; se mapeada para outra chave, a etapa e suas tarefas são canceladas e a etapa de destino é ativada na nova versão com novas tarefas.
- Instâncias com etapa aberta sem destino no mapa ou localizadas dentro de um bloco paralelo são puladas e listadas na prévia com o motivo (`unmapped:<etapa>` ou `inside_block`).
- Cada migração entra no histórico (`instance_migrated`, com versões e movimentações). Desde `sk_flows.0005`, `definition` e `flow_version` deixaram de ser imutáveis no guard para que apenas o serviço de migração possa atualizá-los, preservando identidade, histórico e tarefas ativas.
- Downgrade para versões anteriores e migrações sem mapa explícito não são suportados.

Handlers, condições e regras de atribuição continuam sendo resolvidos por nome no código atual. Se uma versão antiga referenciar um nome removido, a instância falha naquela etapa (`failed`, registrando o motivo no histórico) em vez de executar outro código; mantenha esses nomes registrados enquanto houver instâncias antigas abertas.

### Fluxos definidos em dados

Além dos fluxos declarados em código, a organização pode criar e publicar fluxos definidos em dados ([#414](https://github.com/alaorneto/skeleton/issues/414), [#415](https://github.com/alaorneto/skeleton/issues/415)). Apenas quem administra fluxos tem acesso para ler, editar e publicar rascunhos.

- Um **rascunho** (`FlowDraft`, tabela `sk_flows_flowdraft`) armazena o JSON no formato de `FlowSpec.to_json`, sem o número de versão. A chave segue o padrão `app_label.nome` e não pode colidir com um fluxo declarado em código.
- O rascunho só pode referenciar elementos já registrados em código pela app dona (handlers automáticos, condições, regras de atribuição, tabelas expostas, sequências e tipos de evento), e a app precisa estar ativa na organização. A validação usa exatamente `spec_problems`, e nenhum código arbitrário é executado a partir dos dados.
- **Publicar** trava o rascunho, confere a revisão (CAS), executa a validação e grava uma `FlowDefinition` imutável com a próxima versão da chave. Novas instâncias passam a usar essa definição, enquanto as antigas seguem na versão anterior. Rascunhos inválidos não publicam (400 com a lista de problemas).
- Iniciar instâncias continua sob controle da app dona: `FlowService().start(flow_key=...)` aceita chaves de fluxos publicados em dados sob as mesmas regras de autorização.
- **Superfícies:** REST em `GET` e `POST /api/v1/o/<org>/flows/drafts/`, `GET` e `PATCH .../drafts/<id>/` (`If-Match`) e `POST .../drafts/<id>/publish/` (`If-Match`); interface web em `/o/<org>/flows/drafts/`, listando rascunhos, permitindo criar a partir do JSON e editar com visualização de etapas, transições, problemas de validação e nomes registrados pelas apps.
- O editor manipula diretamente o JSON acompanhado das tabelas de etapas, transições e validação — o mesmo formato da API REST, evitando duas fontes de verdade.

## Modelo de dados

O subsistema possui oito tabelas, todas `OrganizationRecord` (UUID, organização imutável, revisão, exclusão lógica, auditoria por trigger e RLS de tenant) e nenhuma exposta no gateway genérico:

| Modelo | O que guarda | Campos imutáveis |
|---|---|---|
| `FlowDefinition` | `key`, `version`, `app_label`, `label`, `digest`, `specification` (JSON) | Todos |
| `FlowAbsence` | `user`, `substitute`, `starts_at`, `ends_at` | `user` |
| `FlowDraft` | `key`, `label`, `app_label`, `specification` (JSON, sem a versão), `published_version`, `published_at` | `key` |
| `FlowInstance` | `definition`, `flow_key`, `flow_version`, `number`, `subject_table`, `subject_sysid`, `status`, `outcome`, `starter`, `data`, `sensitive_data`, `idempotency_key`, `run_count`, `activity_count`, `finished_at`, `parent`, `parent_run` | Chave, número, registro de negócio, quem iniciou, chave de idempotência, pai e etapa do pai (`definition` e `flow_version` mudam apenas pela migração) |
| `FlowStepRun` | `instance`, `step_key`, `kind`, `status`, `sequence`, `scope`, `branch`, `via`, `outcome`, `state` (JSON), `due_at`, `finished_at` | Instância, etapa, tipo, ordem, escopo, ramo e origem |
| `FlowTask` | `instance`, `step_run`, `step_key`, `kind`, `status`, `slot`, `assignee`, `candidate_group`, `candidate_role`, `delegated_from`, `decision`, `action`, `comment`, `due_at`, `reminded_count`, `escalated_at`, `completed_by`, `completed_at` | Instância, ativação, etapa, tipo e vaga |
| `FlowSubmission` | `instance`, `step_run`, `task`, `step_key`, `transition_key`, `origin`, `values`, `sensitive_values` | Todos |
| `FlowActivity` | `instance`, `sequence`, `kind`, `step_key`, `step_run`, `task`, `detail` (JSON com identificadores e rótulos), `comment` | Todos |

- `sensitive_data`, `sensitive_values` e `comment` são `sensitive_fields`, gravados como `[REDACTED]` na auditoria.
- Um índice único parcial garante no máximo **uma instância ativa** (`running` ou `suspended`) por fluxo e registro de negócio.
- Nada é excluído fisicamente: a role runtime não tem permissão `DELETE` e não há serviço de exclusão.

**Estados.** Instância: `running`, `suspended`, `completed`, `cancelled`, `failed`. Ativação: `active`, `waiting` (junção aguardando ramos), `completed`, `cancelled`, `failed`. Tarefa: `open`, `completed`, `cancelled`.

## Execução

1. **Início.** `start` valida a app dona, o registro de negócio e o `start_form`, congela a definição, cria a instância e ativa a etapa de início.
2. **Avanço.** O motor percorre transições até atingir uma etapa que aguarda algo (tarefa, aprovação, serviço, temporizador, espera ou junção incompleta) ou uma etapa de fim. Gateways são avaliados imediatamente.
3. **Paralelismo.** Uma bifurcação cria uma ativação por ramo, registrando o **escopo** (ativação da bifurcação) e o **ramo** (transição de origem). A junção contabiliza os ramos recebidos e só avança quando todos os ramos ativados chegam.
4. **Fim.** Ao alcançar uma etapa de fim, a instância passa para `completed` com o `outcome` correspondente. A validação estrutural assegura que nenhum ramo permaneça ativo.

- **Etapas automáticas.** Uma etapa de serviço enfileira `flows.service` na mesma transação do avanço. O job executa em `system_context` da organização e invoca o handler com um contexto somente leitura. O handler devolve `None`, um texto (resultado) ou `StepResult(outcome, data)` (com dados restritos aos campos declarados em `outputs`). Escritas no banco e avanço comitam juntos; efeitos externos são **pelo menos uma vez**, como em qualquer [job](jobs.md).
- **Falhas e retentativas.** Se o handler lançar exceção, o motor desfaz a transação, incrementa a tentativa e agenda uma nova execução com espera exponencial (`retry_seconds` dobrando a cada tentativa). Esgotado `max_attempts`, a instância fica `failed` e o histórico registra apenas a classe do erro (nunca a mensagem, que pode conter dados sensíveis). A ação `retry` reativa a etapa. Erros de execução da definição (handler, condição ou regra ausentes, resultado sem transição correspondente ou laço infinito) marcam a instância como `failed` quando disparados por um job, ou recusam o comando humano com `Conflict("flow_failed")` sem alterar o estado.
- **Jobs e tíquetes.** Cada job carrega apenas o identificador da ativação e um tíquete. Qualquer reagendamento (retomada, nova tentativa ou reativação da app) emite um novo tíquete, tornando jobs atrasados ou duplicados inócuos.
- **App desativada.** Enquanto a app dona estiver desativada, jobs disparados não executam nenhuma ação. Quando a app volta a ser ativada na organização, o consumidor de `app.enabled` reagenda automaticamente o que as instâncias em andamento aguardavam.
- **Temporizadores e esperas.** Temporizadores usam o job `flows.timer` com `run_after`. Esperas avançam quando a app envia o sinal (`signal`), podendo incluir os dados do formulário da espera; com `timeout_seconds`, um job encerra a espera com resultado `timeout`.
- **Cancelar, suspender e retomar.** `cancel` encerra ativações e tarefas abertas. `suspend` congela a instância (recusando ações em tarefas e ignorando jobs disparados). `resume` reagenda as pendências.

## Concorrência

- **Um bloqueio por instância.** Todo comando que modifica uma instância bloqueia primeiro a linha de `FlowInstance` (`SELECT ... FOR UPDATE`) antes de ler ativações e tarefas. Ramos paralelos concluídos simultaneamente são serializados, garantindo que a junção dispare exatamente uma vez. A ordem de bloqueio é sempre instância → tarefa.
- **Controle otimista (CAS).** Ações em tarefas exigem a revisão da tarefa; `cancel`, `suspend`, `resume` e `retry` exigem a revisão da instância. A revisão é conferida após o bloqueio; revisões antigas retornam `Conflict("stale_revision")` sem retentativa automática.
- **Um único vencedor.** Se dois atores agirem simultaneamente na mesma tarefa com a mesma revisão, o primeiro vence e o segundo recebe `Conflict("stale_revision")` (409). Em tarefas de **fila**, quem permanece na fila continua podendo ler a tarefa mesmo após outra pessoa encerrá-la (pois o registro `task_created` do histórico preserva a fila original), e novas tentativas com a revisão atualizada recebem `Conflict("task_closed")`. Nunca ocorre segunda escrita.
- **Inícios simultâneos.** `start` bloqueia a linha do registro de negócio: duas chamadas concorrentes para o mesmo fluxo e registro criam apenas uma instância, e a outra recebe `Conflict("already_running")`.
- **Idempotência.** `start` aceita `idempotency_key`, devolvendo a instância já existente (`created=False`) em chamadas repetidas. Todos os jobs do motor usam chaves de idempotência e revalidam o estado antes de avançar.

## Autorização

Todo acesso é negado por padrão e revalidado no servidor em cada operação:

| Operação | Quem pode executar |
|---|---|
| Iniciar (`start`) e enviar sinal (`signal`) | Código da **app dona** do fluxo, para um ator com a ação `start_action` (padrão `update`) no registro de negócio, ou em `system_context` sem ator (jobs e consumidores de eventos da app) |
| Ler instância e histórico | Quem lê o registro de negócio (`authorized_queryset`), quem iniciou a instância, quem tem ou teve tarefa na instância, candidatos a tarefas abertas e quem administra fluxos |
| Ler e agir em tarefa | Responsável pela tarefa; em tarefas de fila, quem pertence ao grupo ou papel candidato **no momento da chamada**. Após o encerramento, membros da fila continuam lendo a tarefa sem poder agir |
| Reatribuir | Responsável (em etapas `delegable`) ou quem administra fluxos |
| Cancelar | Quem administra fluxos, ou quem iniciou quando `starter_can_cancel=True` |
| Suspender, retomar e repetir | Quem administra fluxos |

**Quem administra fluxos** inclui o administrador da organização e quem possui a capacidade `flows.manage`, concedida exclusivamente pelo papel interno "Gestor de processos" (`process_manager`, [#407](https://github.com/alaorneto/skeleton/issues/407)). Essa capacidade restringe-se a fluxos — não concede gestão de membros, apps nem permissões — e cessa na requisição seguinte à revogação do papel. Organizações existentes recebem o papel via reconciliação (`manage.py reconcile_builtin_roles`, executada também na sincronização de deployment).

- **App desativada bloqueia operações.** Com a app dona desativada ou indisponível na organização, qualquer operação sobre seus fluxos responde `Unavailable`, e as listagens omitem suas tarefas e instâncias.
- **Inexistente e não autorizado respondem igual (`NotFound`)**, inclusive para registros de outra organização.
- **Revogação imediata.** Perder um papel ou sair de um grupo remove o acesso à fila já na chamada seguinte, sem cache.
- **Ator explícito.** Todo método recebe `actor`, validado contra o contexto atual.
- **Ordem das verificações.** Visibilidade (`NotFound`), permissão para agir (`Forbidden`), revisão (`Conflict("stale_revision")`), estado da tarefa (`task_closed`) e estado da instância (`instance_suspended`, `instance_closed`).
- O motor nunca abre `system_context` para ampliar privilégios de um ator; os jobs internos usam `system_context` apenas por executarem rotinas automatizadas sem ator.

**Decisão: autorização derivada, sem `TablePermission` própria.** *Contexto:* as tabelas de fluxos não são `SecuredRecord` expostos e, portanto, não recebem `TablePermission`. *Decisão:* a leitura de uma instância deriva do registro de negócio e da participação; a ação em uma tarefa deriva da atribuição. *Consequência:* um aprovador sem acesso de leitura ao registro de negócio ainda vê a instância e os dados dos formulários necessários para decidir, sem ganhar acesso ao registro em si.

## Integrações

| Subsistema | Uso |
|---|---|
| [Jobs](jobs.md) | `flows.service`, `flows.timer`, `flows.wait_timeout`, `flows.task_reminder` e `flows.task_due`, todos em modo sistema, por organização, com payload contendo apenas identificadores |
| [Eventos](events.md) | `flows.instance_started`, `flows.instance_completed`, `flows.instance_cancelled` e `flows.instance_failed` sobre `sk_flows.flowinstance`, com `flow_key`, `flow_version`, `status`, `outcome`, `subject_table` e `subject_sysid` |
| [Webhooks](webhooks.md) | O fluxo pode mapear marcos (`started`, `completed`, `cancelled`, `failed`) para tipos de evento **da própria app sobre o registro de negócio** (`subject_events`). Como o alvo é um `SecuredRecord` exposto, a entrega do webhook reautoriza a leitura pelo principal do endpoint normalmente |
| [Notificações](notifications.md) | `flows.task_assigned`, `flows.task_reminder` e `flows.task_overdue`, levando rótulo do fluxo e da etapa, número do processo e prazo (`{due_at}`, exibido nos textos de lembrete e atraso). Abrir a notificação reautoriza o acesso à tarefa |
| [Auditoria](audit-soft-delete.md) | Triggers em todas as tabelas; enquanto `FlowActivity` conta a narrativa do processo, a auditoria permanece como fonte de verdade de cada escrita |
| [Sequências](sequences.md) | `number_sequence` numera as instâncias (`PC-2026-0042`) sem lacunas, na mesma transação do início |

Os eventos do núcleo sobre `FlowInstance` não vão para webhooks, pois essa tabela não possui autorização por registro verificável pelo principal de um endpoint.

## Histórico

`FlowActivity` registra em ordem cronológica cada evento da instância: `instance_started`, `gateway`, `join`, `step_entered`, `step_completed`, `task_created`, `assignment_unresolved`, `task_claimed`, `task_released`, `task_reassigned`, `task_completed`, `task_decided`, `task_cancelled`, `task_reminded`, `task_overdue`, `signal_received`, `service_retry`, `instance_suspended`, `instance_resumed`, `instance_retried`, `instance_failed`, `instance_cancelled` e `instance_completed`.

O autor vem sempre do contexto (`created_by`), nunca de dados enviados; `detail` guarda apenas identificadores e rótulos; `comment` contém o comentário do processo (visível a quem lê a instância e redigido na auditoria). Os valores preenchidos em cada etapa ficam em `FlowSubmission`.

## Superfícies

- **API de serviços** (`skeleton.service_api.flows.FlowService`): contrato principal para apps. Veja o [catálogo do núcleo](service-api.md#flowservice).
- **REST** em `/api/v1/o/<org>/flows/`: tarefas, instâncias, histórico e ações com `If-Match`, autenticáveis por sessão ou por token de API com `flows:read` e `flows:write` ([#410](https://github.com/alaorneto/skeleton/issues/410)). Veja [API](api.md#fluxos-de-processo).
- **Páginas web** em `/o/<org>/flows/`: caixa de tarefas, detalhe de tarefa, listagem de instâncias e página da instância com histórico, funcionando sem JavaScript.
- **Comentários do registro** nas telas de tarefa e de instância ([#421](https://github.com/alaorneto/skeleton/issues/421)): exibe o painel de [comentários](comments.md) do registro de negócio (com menções) para quem lê o registro, quando a tabela habilita comentários. Não existem comentários separados para a instância; observações de cada passo permanecem no histórico.
- **Diagrama** na página da instância ([#419](https://github.com/alaorneto/skeleton/issues/419)): renderiza no servidor, em SVG, o fluxo da versão da instância, dispondo etapas em colunas pela distância a partir do início, destacando a etapa atual, marcando etapas concluídas e tracejando devoluções. Rótulos são escapados, sem scripts nem bibliotecas externas (preservando a CSP), acompanhados de lista textual acessível para leitores de tela e sem exibir dados de formulário.
- **Minhas tarefas** em `/flows/tasks/` e contador **Tarefas** no cabeçalho ([#409](https://github.com/alaorneto/skeleton/issues/409)): reúne tarefas abertas atribuídas à pessoa em todas as organizações com vínculo ativo via autodescoberta em `FlowTask.assignee`. Tarefas de fila ficam na caixa de cada organização, pois dependem dos grupos e papéis do tenant. Clicar em um item abre a tarefa na organização, onde a autorização é revalidada.
- **Painel de operação** em `/o/<org>/flows/operations/` (e `GET /api/v1/o/<org>/flows/operations/`), restrito a quem administra fluxos ([#408](https://github.com/alaorneto/skeleton/issues/408)): contabiliza e lista instâncias com falha e tarefas abertas sem responsável, atrasadas ou a vencer em 24 horas, com filtro por fluxo e versão e atalhos para reatribuir, retomar ou cancelar. Exibe apenas identificadores, situações e datas — nunca dados de formulário — com número constante de consultas e limite de 50 itens por lista.

Não existe rota genérica no núcleo para iniciar instâncias: iniciar um fluxo é sempre uma operação de negócio da app dona, que chama `FlowService().start(...)` em sua própria view ou serviço.

### Métricas

Com `/metrics` habilitado ([implantação](deployment.md)), a coleta expõe por chave de fluxo o total de instâncias por estado, tarefas abertas e vencidas e etapas automáticas com falha. Nenhum rótulo expõe organização, pessoa ou registro.

## O que entra na primeira entrega

O [exemplo Notes](../examples/notes/flows.py) declara o fluxo `example_notes.publication`, cobrindo tarefa com formulário, gateway exclusivo, duas aprovações paralelas, devolução e etapa de serviço; `tools/flow_fixtures.py` inclui um fluxo de teste com todos os tipos de etapa.

A primeira entrega contempla toda a base descrita: declaração em código com validação estrutural; etapas de início, fim, tarefa, aprovação, serviço, temporizador e espera; gateways exclusivo, paralelo e inclusivo; formulários por etapa e por transição; políticas de aprovação `any`, `all`, `quorum` e `sequential`; delegação, devolução, comentários, prazos, lembretes e escalonamento; atribuição por pessoa, papel, grupo ou regra; caixa de tarefas; vínculo ao registro de negócio; histórico; cancelar, suspender, retomar e repetir; integração com jobs, eventos, webhooks, notificações, auditoria e sequências; `FlowService`, REST e páginas web; além do fluxo de exemplo no Notes.

## Evoluções do epic #208

Os recursos planejados para a etapa seguinte foram entregues no epic [#208](https://github.com/alaorneto/skeleton/issues/208): papel de gestor de processos, painel de operação, caixa global de tarefas, suporte a tokens de API, esperas por evento, subfluxos, migração de instâncias, fluxos definidos em dados com editor, calendário útil e SLA em horas úteis, substituição por ausência, diagrama SVG na instância, métricas Prometheus, comentários do registro nas telas de fluxo e retenção dos dados de formulário.

## Decisões confirmadas pelo responsável

Confirmadas em 2026-11-12, conforme o desenho proposto:

1. **Nome** `skeleton/flows` (label `sk_flows`, "fluxos") em vez de "workflow", evitando custos de migração de dados após a criação das migrations.
2. **Fluxos apenas em código** na primeira entrega; posteriormente, com [#414](https://github.com/alaorneto/skeleton/issues/414) e [#415](https://github.com/alaorneto/skeleton/issues/415), passaram a ser suportados também em dados, com rascunho, publicação e editor.
3. **Registro de negócio obrigatório:** todo fluxo opera sobre um `SecuredRecord` exposto da app dona, do qual deriva a leitura da instância.
4. **Uma única instância ativa** por par de fluxo e registro de negócio.
5. **Administração de fluxos** reservada inicialmente ao administrador da organização e, com [#407](https://github.com/alaorneto/skeleton/issues/407), estendida a quem possui `flows.manage` (papel "Gestor de processos").
6. **Aprovadores veem os dados de formulário** da instância mesmo sem permissão de leitura sobre o registro de negócio.
7. **Sem exclusão física** nem retenção automática na primeira entrega.
8. **Versão do core inalterada nesse epic:** os métodos de `FlowService` declaram `since="0.1.0"`, correspondente à primeira release publicada.
9. **Concorrência em tarefas de fila retorna conflito (409), não 404:** a tarefa encerrada permanece legível para os membros da fila.
