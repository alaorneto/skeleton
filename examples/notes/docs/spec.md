# Anotações

App opcional de exemplo que demonstra como implementar regras de negócio fora do núcleo. Para o vocabulário usado aqui, consulte [apps e instalação](../../../docs/concepts.md#apps-pacotes-e-deployment) e [autorização e contexto](../../../docs/concepts.md#autorização-e-contexto). O passo a passo de modelo, migration e requisições está em [extensão](../../../docs/extending.md).

## Contract

`example_notes` é habilitada explicitamente e tem suas dependências técnicas instaladas offline. Instalar o código no ambiente não ativa a app automaticamente nas organizações nem concede permissões.

Cada `Note` pertence a uma organização e herda de `SecuredRecord`, com UUID imutável, revisão, proprietário e visibilidade. Os campos de negócio são `code` (até 64 caracteres, único entre notas ativas da mesma organização), `title` (até 200) e `body` (opcional, até 10.000). Ao criar uma nota, o proprietário é o ator e a visibilidade nasce privada; compartilhar exige a operação dedicada de gestão de acesso. O campo `body` é legível por quem tem autorização, mas sai como `[REDACTED]` na auditoria.

As anotações participam da busca global da organização (`GlobalSearchPolicy`): quem pode lê-las encontra registros pelo código ou pelo título. O gateway expõe a tabela como `example_notes.note`, oferecendo criação, leitura, edição, exclusão lógica e restauração conforme as permissões. Edição, exclusão e restauração exigem a revisão atual (CAS); a exclusão lógica preserva UUID e histórico, e a restauração valida referências e unicidade de código.

As páginas próprias ficam em `/o/<org>/apps/example_notes/` e o sumário REST em `/api/v1/o/<org>/apps/example_notes/summary/`. Navegação e servidor exigem a app ativa, permissão de leitura na tabela (`table.read`) e autorização por registro; o sumário contabiliza apenas as notas que o ator pode ler.

O parâmetro `heading` aceita texto simples de até 200 caracteres para apresentação (sem segredos). Apenas `organization_admin` (ou quem tem a capacidade de gestão de apps) altera ativação e configuração, sempre com CAS. A app nasce indisponível na plataforma e desativada na organização (`default_enabled: false`, campo obsoleto), usando revisão virtual `"0"` na primeira gravação. Em desenvolvimento, `manage.py set_app_availability example_notes --mode all --enable <slug>` libera e ativa o exemplo. O JSON de configuração é integralmente redigido na auditoria, e não existe administração global de notas organizacionais.

A API de serviços da app é `NoteService`, declarada em `runtime.service_api` e documentada em [service-api.md](service-api.md) — único módulo importável por outras apps. Tanto a página própria quanto o sumário REST chamam `NoteService` em vez de consultar o modelo diretamente, e os DTOs nunca incluem o campo sensível `body`.

O Notes usa manifesto v2 compatível com `>=0.0.0,<1.0.0` e declara layout em `runtime.model_ui` desde a versão `0.2.0`. A app recebe grupo automático de parâmetros no painel administrativo com a seção `general`, sem listar nem editar notas ali. O viewer de negócio fica em `/o/<org>/tables/example_notes.note/` (acessível também pelo catálogo), com listagem por código, título e atualização, filtro por código, abas *Identificação* e *Conteúdo* e rótulos *Código*, *Título* e *Texto*. Na grade de 12 colunas, `code` ocupa 4, `title` ocupa 8 e `body` ocupa 12 (textarea de 8 linhas com texto de ajuda *"Conteúdo da anotação."*), empilhando em largura total no celular. O pack SSR `skeleton_crud` do `django-crispy-forms` 2.7 renderiza os formulários existentes sem alterar allowlists, autorização, auditoria ou CAS.

`NoteCategory` (`example_notes.notecategory`, desde `0.5.0`) é um catálogo global de categorias compartilhado entre todas as organizações: `SystemRecord` com `TableSpec(scope="global")`, fora do gateway e com código único entre categorias ativas. Somente o comando `manage.py load_note_categories CODIGO=Título ...` escreve nessa tabela, dentro de `command_context`; qualquer tentativa de escrita a partir de view, serviço ou contexto de organização é recusada pelo modelo e pelo guard do banco ([tabelas globais de apps](../../../docs/table-catalog.md#tabelas-globais-de-apps)). A auditoria registra cada alteração sem organização, com `source=system:command:example_notes:load_note_categories`.

Desde `0.6.0`, a app registra o quadro de painel `example_notes.recent` (`widgets.py`), exibindo as cinco anotações mais recentes legíveis pelo usuário via `NoteService.recent_notes`, com link para a lista completa. O portão exige permissão de leitura em `example_notes.note` e app ativa; o serviço continua filtrando apenas registros legíveis. O administrador posiciona o quadro na página inicial interna da organização ([quadros do painel](../../../docs/extending.md#quadros-do-painel-da-organização)).

## Acceptance

Os critérios abaixo definem os comportamentos esperados (não substituem os registros de execução de testes):

- Entre duas organizações A e B, um membro de A nunca acessa notas ou configurações de B, mesmo conhecendo o UUID.
- Sem `table.read`, a página e o sumário são negados; com `table.read`, o sumário continua excluindo notas privadas de terceiros sem concessão de leitura.
- Desativar a app bloqueia o gateway e oculta a navegação, preservando dados e auditoria; reativar mantém UUIDs e regras de acesso (ACL) sem conceder novas permissões.
- Após editar `body`, a auditoria comprova a alteração sem expor o conteúdo; alterações de configuração também ocultam o JSON no histórico.
- Duas edições concorrentes com a mesma revisão têm apenas um vencedor; restaurar uma nota cujo código já está em uso retorna conflito sem sobrescrever o registro ativo.
- `NoteService` passa em `assert_service_contract` e, em PostgreSQL com a role runtime, aplica default deny, isolamento A/B, `Unavailable` com a app desativada, recusa de ator divergente e CAS com vencedor único.
- Escrever em `NoteCategory` a partir de contexto organizacional ou consumidor de evento falha; `load_note_categories` grava com sucesso e registra o motivo na auditoria.
- Os checks estáticos validam as seções obrigatórias de `spec.md`, `adr.md` e `service-api.md` (`E012`) e a declaração da API (`E018`), complementando — sem substituir — os testes de isolamento, autorização e concorrência.

## Fluxo de publicação

A app declara o fluxo `example_notes.publication` em `flows.py` (registrado no `ready()`), vinculado à própria anotação. As etapas são: preparação pelo autor (formulário com público, resumo e uma observação sensível), bifurcação exclusiva conforme o público, revisão única para público interno (`organization_admin`) ou duas revisões paralelas para público externo (`note_reviewer` e `organization_admin`, com junção), devolução ao autor em caso de rejeição, etapa de serviço que registra a publicação e os estados finais `published` e `withdrawn`.

- Iniciar o fluxo exige permissão de leitura e edição na anotação (`NoteService.request_publication`), acionado pela página da app ou por `POST /api/v1/o/<org>/apps/example_notes/publication/`.
- Tarefas e aprovações são operadas nas telas do núcleo (`/o/<org>/flows/tasks/`).
- A observação reservada é um campo sensível e não aparece em auditoria, eventos, notificações ou DTOs.
- Se o papel `note_reviewer` não existir na organização, a tarefa de conteúdo nasce sem responsável e pode ser reatribuída pelo administrador.
