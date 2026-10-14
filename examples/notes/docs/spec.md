# Anotações

Esta app opcional demonstra como implementar negócio sem inserir suas regras no núcleo. Para os conceitos usados aqui, leia [apps e instalação](../../../docs/concepts.md#apps-pacotes-e-deployment) e [autorização e contexto](../../../docs/concepts.md#autorização-e-contexto). O roteiro com modelo, migration e requisições está em [extensão](../../../docs/extending.md).

## Contract
`example_notes` é habilitada explicitamente e suas dependências técnicas são instaladas offline. Instalar seu código no ambiente não a ativa automaticamente para toda organização e não concede permissões.

Cada `Note` pertence a uma organização e deriva de `SecuredRecord`: tem UUID imutável, revisão, proprietário e visibilidade. Os campos de negócio são `code` (até 64 caracteres), `title` (até 200) e `body` (opcional, até 10000). O código é único entre notas vivas da mesma organização. Criar atribui o proprietário ao ator e visibilidade privada; compartilhar exige a operação separada de gestão de acesso. O conteúdo de `body` pode ser lido por quem tem autorização, mas é substituído por `[REDACTED]` nos eventos de auditoria.

O gateway usa `example_notes.note` e oferece criação, leitura, alteração, exclusão lógica e restauração conforme as permissões. Alterar, excluir ou restaurar exige a revisão atual; uma revisão desatualizada não sobrescreve mudanças. A exclusão lógica preserva UUID e histórico. Restaurar valida referências e unicidade: se outra nota viva já usa o código, há conflito.

As páginas próprias ficam sob `/o/<org>/apps/example_notes/`; o sumário está em `/api/v1/o/<org>/apps/example_notes/summary/`. Navegação e servidor exigem app ativa e permissão de leitura da tabela (`table.read`), além da autorização dos registros. O sumário conta somente notas que o ator pode ler, não todas as notas da organização.

A configuração `heading` é um texto de até 200 caracteres para apresentação, sem segredos. Somente `organization_admin` administra ativação/configuração, com CAS: compara a revisão lida antes de gravar, evitando perda de uma alteração concorrente. Sem estado persistido, vale `default_enabled` do manifesto; a primeira escrita usa revisão virtual `"0"`. O JSON da configuração é integralmente redigido na auditoria. Não há administração global dos registros organizacionais.

Notes 0.2.0 usa manifesto v2 com compatibilidade `>=0.3.0,<1.0.0`, pois declara dimensões em `runtime.model_ui` do arquivo canônico. A app recebe automaticamente grupo de parâmetros no admin, com seção de configuração `general`. Esse painel não lista/edita notas: o viewer está em `/o/<org>/tables/example_notes.note/`, descoberto pelo catálogo mesmo sem o menu próprio. Sua lista declara código/título/atualização e filtro por código; leitura/criação/edição usam abas Identificação/Conteúdo e labels Código/Título/Texto. Na grade de 12 unidades, `code` ocupa 4, `title` 8 e `body` 12 com textarea de 8 linhas e ajuda “Conteúdo da anotação.”; mobile usa largura inteira e conserva a ordem. O pack SSR `skeleton_crud` de django-crispy-forms 2.7 renderiza os Django Forms existentes, sem ampliar allowlists, acesso ou campos auditáveis nem mudar CAS/persistência.

## Acceptance
Os critérios abaixo descrevem comportamentos observáveis, não resultados de testes já executados:

- Com duas organizações A/B, um membro de A não obtém notas nem configuração de B, inclusive por UUID conhecido.
- Sem permissão de leitura da tabela, a página e o sumário são negados. Com essa permissão, o sumário ainda exclui notas privadas de terceiros que não tenham concessão de leitura.
- Desligar a app bloqueia também seu gateway e remove a navegação, preservando dados e auditoria. Reativar conserva UUIDs e ACL (regras de acesso), sem conceder novas permissões.
- Depois de alterar `body`, o evento comprova a alteração sem apresentar seu conteúdo. Uma mudança de configuração também não expõe o JSON no histórico.
- Duas alterações com a mesma revisão não vencem ambas. Uma restauração que colide com um código vivo retorna conflito sem substituir o outro registro.
- O check estático exige `spec.md` e `adr.md` com as seções obrigatórias. Ele verifica o contrato documental, não substitui os testes de isolamento, autorização e concorrência.
