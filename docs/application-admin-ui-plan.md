# Administração de apps e interfaces padronizadas

**Estado: contrato de administração, renderer e dimensões implementados em todo núcleo publicado (0.1.0 em diante).** Este documento consolida o contrato resultante do planejamento, incluindo a regra de que a área administrativa serve exclusivamente para configurações e parametrizações. As evidências de execução ficam em [testing](testing.md); a existência do contrato não substitui a verificação de um backend de deployment.

## 1. Fronteiras das interfaces

Cada superfície do Skeleton tem responsabilidade bem delimitada e não invade o papel das demais:

| Superfície | Responsabilidade | Não faz |
|---|---|---|
| `/platform/` | Administração da plataforma em views próprias: identidades, organizações, aplicativos instalados (visão geral, documentação e telas de escopo plataforma), catálogo de tabelas e auditoria global | Não abre negócio da organização nem seleciona organizações; não há editor genérico de modelos |
| `/platform/updates/` e `/platform/updates/apps/` | Atualizações da plataforma (núcleo e agente) e de aplicativos; cada plano segue quatro etapas: escolher, conferir o impacto, aprovar e acompanhar | Não executa `pip` nem DDL; apenas solicita prévia e aprovação ao agente |
| `/o/<org>/apps/` e `apps/<app_label>/admin/` | Configuração, ativação e parâmetros organizacionais das apps | Não apresenta tabelas, CRUD nem atalhos de modelos |
| `/o/<org>/catalog/` e `tables/<table_key>/` | Descoberta e viewer padronizado de negócio: lista, detalhe e formulário | Não instala software nem administra configurações da plataforma |
| `/api/v1/o/<org>/tables/<table_key>/` | Recursos de negócio e seleção autorizada de referências | Não aceita classe Python, nome SQL nem contorno de autorização |

A administração da plataforma utiliza **views explícitas** em `skeleton/web/platform` (namespace `platform`), protegidas por `platform_page`: visitantes anônimos vão ao login, usuários sem `staff` e `superuser` ativos recebem 403, pendência de MFA redireciona ao cadastro de 2FA e o contexto precisa ser global. Identidades e organizações só mudam pelos serviços de ciclo de vida (`update_platform_user`, `create_identity`, `deactivate_user`, `create_organization`, `update_organization`, `designate_organization_admin`), com CAS por revisão (na designação, pela versão do conjunto de administradores).

Na página de uma organização na plataforma (`organization_overview`, leitura restrita descrita em [controle de acesso](access-control.md#designação-de-administrador-pela-plataforma)), aparecem apenas a contagem de membros ativos, os e-mails dos administradores e o cartão **Aplicativos**, com a disponibilidade (portão da plataforma) e a ativação (escolha da organização) de cada app instalada — sem parâmetros nem dados de negócio. No catálogo de tabelas, apenas rótulo e descrição são editáveis.

**Disponibilidade de apps.** `/platform/apps/<app_label>/availability/` é a única tela que altera a disponibilidade de uma app, sempre pelos serviços de `skeleton.system.availability`:

- o formulário de modo (`all` ou `selected`) usa CAS sobre a linha `AppAvailability`, com revisão virtual `0` antes da primeira gravação;
- na lista de organizações concedidas, a revogação exige a revisão exibida da concessão; revisão desatualizada ou concessão já revogada responde 409;
- o seletor de concessão lista apenas organizações ativas, não excluídas e ainda sem concessão;
- o cartão **Dependências** informa quais apps precisam estar disponíveis antes de conceder e quais dependentes concedidas impedem a revogação (marcadas com "Bloqueada por …").

Toda escrita exige CSRF e autenticação recente; abrir as telas não grava nem audita nada. Revogar apenas fecha o portão: a ativação e os parâmetros da organização permanecem preservados e voltam a valer numa nova concessão. O cartão **Disponibilidade** em `/platform/apps/<app_label>/` resume o modo atual e leva a essa tela.

Nenhum modelo é registrado no Django admin e nenhum changelist genérico é roteado: `/platform/records/`, `/admin/<app>/<model>/` e o antigo seletor de "configuração por organização" retornam 404, sem aliases. O `django.contrib.admin` permanece instalado apenas como dependência técnica (modelo `LogEntry` e migrations históricas que concedem privilégios em `django_admin_log`), via `SimpleAdminConfig`, sem autodiscover, site ou rotas; `/admin/…` responde 404.

O namespace de uma rota é apenas uma identidade de integração Django, não o painel em que a tela aparece: a separação efetiva está na URL, no conteúdo e no layout. Os serviços de identidade, CAS, desativação, último administrador, MFA e CSRF continuam protegendo todos os registros globais.

Neste contrato, "toda tabela de negócio" significa exclusivamente `SecuredRecord` exposto via `TableSpec.expose_api=True`, com código instalado e app habilitada na organização. Tabelas globais, técnicas, desconhecidas, modelos não expostos e schemas retidos de apps removidas não entram no viewer organizacional. O catálogo físico nunca concede acesso.

## 2. Grupo automático de configuração

Cada manifesto carregado gera um grupo imutável de apresentação na memória do processo. Sua identidade é o `app_id`; o `app_label` identifica modelos e rotas e não muda entre atualizações. Não há tabela de menus ou layouts nem execução de DDL em `ready()`.

O grupo exibido na plataforma reúne visão geral, versão, origem, compatibilidade, documentação local escapada e telas próprias de **parâmetros**. Ele nunca exibe registros, contagens de negócio nem valores de configuração das organizações.

A configuração organizacional continua persistida em `AppInstallation`, com revisão virtual `0` na primeira escrita. Campos escalares declarados podem informar rótulo, texto de ajuda e seção. A gravação usa `configure_application`, bloqueia a organização antes de validar revisão e dependências e preserva valores persistidos quando muda apenas a apresentação.

Configuração e parâmetros da organização exigem a capacidade `apps.manage` (gestor de apps ou administrador); sem ela, o painel responde 404. Usuários com acesso apenas aos dados de negócio utilizam o catálogo e o viewer, não o painel de parâmetros. O painel exibe somente as apps disponibilizadas pela plataforma e sinaliza as ativas que foram retiradas ("Indisponibilizada pela plataforma"), permitindo apenas desativá-las. Telas de configuração permanecem acessíveis enquanto a app está desativada; já rotas próprias de negócio e API, listas, formulários e policies continuam bloqueadas pela ativação.

Configurações globais não são inferidas da configuração da organização: uma app que precise de parâmetros globais declara tela própria de escopo `platform` e usa modelo e serviço globais dedicados. Segredos pertencem exclusivamente à configuração privada do deployment, nunca ao JSON organizacional.

### Lifecycle

| Evento | Apresentação | Persistência |
|---|---|---|
| Instalar | Grupo aparece no release confirmado; app local também recebe grupo | Defaults sem materializar linhas em todos os tenants |
| Atualizar | Seções, telas e layouts recompostos por IDs estáveis | Valores e UUIDs preservados; mudanças incompatíveis exigem migration |
| Desativar no tenant | Parâmetros disponíveis; negócio bloqueado | Dados, configuração e ACL conservados |
| Retirar globalmente | Nenhuma tela executável da app; histórico no controle de releases | Schema, audit, migrations e configuração retidos |
| Reinstalar | Grupo volta após validação | Ativação explícita por organização; IDs não recriados |
| Falhar | Sem publicação parcial do destino | Manutenção, backup e recuperação seguem o agente |

## 3. Manifesto canônico v2

Todo núcleo publicado entende `manifest_version=2` e as dimensões `col_span` e `rows` de `runtime.model_ui`: por isso, os pisos `MANIFEST_V2_CORE_MINIMUM` e `FIELD_DIMENSIONS_CORE_MINIMUM` são `0.0.0`. Novos manifestos criados por `init` começam com `>=0.0.0,<1.0.0`. A publicação via `build_artifact` valida a apresentação antes de assinar. Leitura, verificação e reassinatura de artefatos v1 e v2 históricos preservam os bytes originais do JSON assinado, o digest e o inventário, sem normalizar nem injetar defaults; apenas as representações em memória recebem valores padrão de apresentação.

`runtime.administration` aceita `configuration_sections`, `sections` e `screens`. Telas administrativas admitem somente `kind` igual a `configuration` ou `custom`; **`model_list`, `table_key` e `action` são recusados nessa seção**. Metadados de tabelas pertencem exclusivamente a `runtime.model_ui` e à navegação de negócio.

| Campo administrativo | Regra |
|---|---|
| `id`, `section_id`, `label`, `order` | IDs simples, únicos e estáveis; seção existente; ordenação com desempate por ID |
| `scope` | `platform` ou `organization` explícito |
| `kind` | `configuration` ou `custom` de parâmetros |
| `url_name` | Somente `custom` de organização, resolvível no namespace da própria app |
| `handler_id` | Somente `custom` de plataforma, callable registrado pelo código da app |
| `organization_admin_only` | Obrigatório `true` para `custom` de organização; o nome é histórico, a tela exige `apps.manage` |
| `sensitive` | Exige reautenticação recente na tela global |

Handlers de plataforma recebem a `request` já protegida, sem permissão universal nem imports controlados pelo cliente. Seus módulos pertencem à app e os IDs não podem colidir. Código assinado é código confiável para execução, **não** uma sandbox: telas próprias devem respeitar o contrato de parâmetros e nunca implementar um viewer disfarçado.

Exemplo do contrato atual:

```json
{
  "administration": {
    "configuration_sections": [{"id": "general", "label": "Geral", "order": 10}],
    "sections": [{"id": "parameters", "label": "Parâmetros", "order": 10}],
    "screens": [{"id": "configuration", "section_id": "parameters", "label": "Configuração", "scope": "organization", "kind": "configuration", "organization_admin_only": true}]
  },
  "model_ui": [{
    "table_key": "example_notes.note",
    "list": {"columns": ["code", "title", "body", "updated_at"], "filters": ["code"], "title_field": "title"},
    "form": {
      "tabs": [{"id": "general", "label": "Identificação"}, {"id": "content", "label": "Conteúdo"}],
      "sections": [
        {"id": "identification", "label": "Identificação", "tab_id": "general", "columns": 2, "fields": [{"name": "code", "label": "Código", "col_span": 4}, {"name": "title", "label": "Título", "col_span": 8}]},
        {"id": "content", "label": "Conteúdo", "tab_id": "content", "fields": [{"name": "body", "label": "Texto", "help_text": "Conteúdo da anotação, com texto formatado.", "widget": "markdown", "col_span": 12, "rows": 8}]}
      ]
    }
  }]
}
```

Esse bloco pertence à chave `runtime` do `applications/<package>/manifest.json` canônico, não a um pacote ou especificação de segurança separados. Registre o manifesto com `AppManifest.from_json(..., app_config=self)` em `ready()`, junto ao modelo elegível e ao seu `TableSpec`, e utilize as URLs genéricas do viewer. Não existem `ui.json` paralelo, editor de layout no admin nem obrigação de criar `Form` Python ou template próprios. O exemplo Notes traz o contrato completo em `examples/notes/manifest.json`.

### Validação

Antes de importar qualquer código, o validador do pacote verifica esquema fechado, tipos, propriedade, namespaces, limites, assinatura e inventário. Os limites estruturais são: até 100 entradas por lista de seções, telas ou abas; até 200 modelos de apresentação; até 200 entradas de campo por layout; rótulos de até 200 caracteres; textos de ajuda de até 2000 caracteres.

Com o Django carregado no staging, `skeleton.E013` verifica modelos expostos da própria app, campos concretos e allowlists, cobertura do formulário, widgets, título escalar, rotas próprias HTML e API, handlers e colisões com as raízes reservadas `platform/` e `admin/`. Qualquer declaração inválida bloqueia o cutover, sem fallback silencioso.

## 4. Viewer de negócio fora do admin

`TableSpec` é a única autoridade sobre modelo, leitura, escrita e sensibilidade. `ModelUISpec`, `ListSpec` e `FormLayoutSpec` são apenas apresentações imutáveis indexadas por `table_key`, incapazes de ampliar exposição ou permissões. O catálogo e o painel agrupam modelos legíveis por app; modelos elegíveis sem menu ou layout continuam descobertos.

A listagem renderiza colunas declaradas, título escalar seguro, filtros exatos visíveis, paginação e estado vazio. Na ausência de declaração, exibe rótulo seguro, UUID e data de atualização. O viewer nunca chama `__str__` de negócio nem entrega objetos ORM relacionados diretamente ao template. Referências invisíveis aparecem sem rótulo nem UUID. A projeção avalia visibilidade em lote por política do modelo alvo (nunca célula a célula), e `.only()` inclui as colunas necessárias para evitar consultas diferidas por linha.

Isolamento por organização, ativação da app, permissão de tabela e visibilidade são aplicados antes de filtro, contagem e paginação. O HTML usa 25 registros por página e ordenação `created_at, sysid`; a API mantém padrão 25 e máximo 100. Não há busca por regex ou DSL, ordenação livre, edição em lote nem joins escolhidos pelo cliente. Filtros vazios do painel HTML são omitidos, chaves desconhecidas são recusadas e a paginação preserva os filtros validados.

### Referências

- **API:** `/api/v1/o/<org>/tables/<table_key>/references/<field>/`.
- **Página seletora:** `/o/<org>/tables/<table_key>/references/<field>/`.
- A query aceita somente `record`, `access` (`0` ou `1`), `page` e `page_size`, sem parâmetros duplicados.
- Criação exige `create` na origem; edição envia `record` e exige `update`; gestão de acesso exige `manage_access` no registro. Campo e alvo nunca abrem consulta livre a tabelas arbitrárias.
- Alvos de negócio usam `authorized_queryset`; identidade e acesso usam resolvedores dedicados ativos na mesma organização. Identidades são representadas por UUID, nunca por uma lista global de e-mails.
- Todo POST revalida a referência no servidor, mesmo que o alvo estivesse legível quando o formulário foi aberto.
- Com JavaScript, a seleção abre janela auxiliar com comunicação `same-origin` vinculada ao campo e ao registro. Sem JavaScript, a página paginada permite copiar o UUID para o formulário aberto.

## 5. Formulário e detalhe padronizados

Criação, edição e leitura utilizam Django Forms renderizados no servidor por **django-crispy-forms==2.7** com o pack próprio `skeleton_crud`, distribuído no wheel do núcleo. O Crispy atua apenas como motor de apresentação: o núcleo compila o envelope seguro em layout sem substituir autorização, validação, CAS, serviços ou persistência.

Não há pack Crispy externo, CDN nem framework JavaScript; o visual vem do CSS compilado e versionado do núcleo (Tailwind e Flowbite; veja [interface e design](ui-design.md)). Sem JavaScript, as seções são exibidas em sequência. Quando há erro de validação, a aba afetada é selecionada automaticamente, os valores enviados são preservados e um resumo focável leva diretamente a cada campo.

| Dimensão | Contrato |
|---|---|
| `col_span` | Omitido ou `null` conserva a largura automática; inteiro exato de 1 a 12 define a fração na grade de 12 colunas |
| `rows` | Omitido ou `null` conserva o widget; inteiro exato de 1 a 40 define a altura de textarea editável, sem alterar validators ou limites |
| `columns` da seção | `1` ou `2` define apenas a largura padrão (12 ou 6); não limita spans explícitos |
| Ordem e mobile | Arrays de abas, seções e campos definem ordem de DOM e foco, sem compactação densa; até 600px todos os campos ocupam a linha inteira |

O widget `markdown` (texto formatado, piso de núcleo 0.9.0) aceita `rows` e a lista opcional `markdown_features`; o formulário renderiza o editor e a página de detalhe exibe o HTML gerado no servidor ([texto formatado](ui-design.md#campo-formatado-na-interface-declarativa)).

Valores booleanos, strings, floats e inteiros fora do intervalo são recusados. Sem span explícito, campos largos ocupam 12 colunas. `rows` não é permitido com `read_only=True`, relações, inputs ou selects: um override explícito exige `textarea`, `markdown` ou `json` compatível com o tipo, e sem override o widget efetivo precisa ser `forms.Textarea`. Esse contrato é validado tanto de forma pura quanto no check `skeleton.E013` e no formulário, sem conversão implícita. Rótulos e textos de ajuda são sempre texto escapado, nunca templates ou HTML executável.

Perfil, gestão dedicada de acesso e configuração compartilham o mesmo renderer de formulários, sem adotar layout de negócio. Quando não há seções declaradas, os campos visíveis entram na seção padrão (`fields`/`Dados do registro`), na ordem original e com span 12. O shell garante um único token CSRF, campos ocultos e `form.media` renderizados uma só vez; o helper Crispy não cria um segundo `<form>` nem oculta campos silenciosamente.

Regras adicionais de formulário e ciclo de vida:

- todo campo gravável aparece exatamente uma vez na edição; sem layout declarado, usa-se `write_fields`;
- campos somente leitura precisam constar em `read_fields` ou nos metadados sistêmicos permitidos; na tela de detalhe, campos legíveis não posicionados entram em uma seção padrão;
- campos graváveis não legíveis nunca revelam o valor persistido na edição; o controle `__change_<campo>` distingue preservar de substituir ou limpar e não é repassado ao serviço nem aceito pela API como campo do modelo;
- obrigatoriedade, `choices`, validators e limites continuam definidos por Django, modelo e serviço; widgets seguem allowlist compatível com o tipo, e booleanos anuláveis preservam `null` em vez de virar `false` implicitamente;
- datas e horas em HTML usam o fuso do perfil, mas a persistência permanece em UTC;
- FKs usam UUID e seletor limitado, evitando carregar todos os alvos em um `ModelChoiceField`;
- `owner`, visibilidade e grupo só mudam pela ação de acesso separada; UUID, organização, ator e revisão nunca entram na escrita comum;
- exclusão e restauração são confirmações com revisão e CSRF, sem campos de negócio nem hard delete; um ator com permissão de exclusão sem leitura não vê título nem resumo do registro. Cancelar nunca grava: criação volta à lista; edição, acesso e exclusão de registro vivo voltam ao detalhe apenas se o ator puder lê-lo (caso contrário, à lista); restauração volta à lista de excluídos. Todos os destinos são rotas fixas via `reverse`, nunca URLs vindas do cliente;
- revisão desatualizada responde 409 em HTML e 412 na API, sem retentativa automática que sobrescreva edições concorrentes;
- `FileField` requer superfície dedicada e não recebe upload genérico por este contrato.

### Serviços específicos da app

Uma app pode registrar `RecordOperations` em código por `table_key` para impor handlers próprios de `create`, `update`, `delete` e `restore`. Os handlers recebem `(record, *, user, values)` em criação e atualização e `(record, *, user)` no ciclo de vida. O núcleo valida campos, coerção, referências e autorização antes de invocar o serviço; o serviço persiste com CAS na mesma transação e mantém intactos os triggers de auditoria.

HTML e API usam o mesmo dispatcher. Quando existe um adaptador registrado e uma operação não foi declarada, ela é negada sem fallback para o CRUD genérico. Sem adaptador, os serviços padrão continuam valendo. Os handlers precisam pertencer à app, têm assinaturas e colisões verificadas e são removidos da memória quando a app é retirada. O exemplo Notes não precisa de adaptador.

## 6. Atualização e configuração persistida

A prévia aprovada de um release destaca alterações em grupos, seções, telas, layouts e campos de configuração, além de arquivos e migrations. Redução de limite, troca de tipo ou remoção de campo de configuração é sinalizada como incompatibilidade; reordenar a apresentação nunca apaga valores gravados.

O bloqueio anterior que rejeitava qualquer mudança em `configuration_fields` foi substituído pela validação do contrato somada à checagem real após as migrations. O comando `check_application_configuration` executa transações independentes por organização, com a credencial runtime, para validar as configurações persistidas das apps carregadas. Remover ou renomear campos exige migration explícita com modelos históricos — não há transformação configurada via HTTP. Qualquer falha bloqueia o cutover e mantém o modo de manutenção. `default_enabled` é ignorado em todo núcleo publicado: a ativação nasce desligada e só muda por decisão explícita do administrador da organização (ou pelo upgrade único que preserva apps já ativas).

O processo web continua sem `pip`, sem DDL e sem credencial owner. Este contrato não cria novas entidades no núcleo nem expõe `SystemRecord` ou `OrganizationRecord` em um gateway genérico: seus serviços e superfícies dedicados permanecem inalterados.

## 7. Mapa da implementação

| Subsistema | Arquivos principais |
|---|---|
| Contratos e checks | `system/packages.py`, `app_registry.py`, `presentation.py`, `checks.py` |
| Administração | `web/platform/` (plataforma), `system/administration.py`, `application_views.py`, `application_urls.py`, `conf/urls.py` |
| Viewer e formulários | `web/views.py`, `forms.py`, `presentation.py`, `form_engine.py`, `templatetags/crud_forms.py`, `references.py`, `services.py`, pack `templates/skeleton_crud` e static |
| API, picker e dispatch | `api/views.py`, `api/urls.py`, `system/record_operations.py` |
| Release e checagem de configuração | `deployment_agent/releases.py`, `service.py`, `check_application_configuration`, preview do release |

## 8. Critérios de aceitação

Os IDs originais do planejamento são preservados, com os itens `ADM` atualizados conforme a separação entre administração de parâmetros e viewer de negócio. Resultados e comandos executados constam em [testing](testing.md) e não devem ser presumidos a partir desta tabela.

| ID | Critério |
|---|---|
| ADM-01 | Grupo único de parâmetros após instalação real e para app local; admin sem registros nem modelos |
| ADM-02 | Seções e telas de configuração próprias ordenadas; navegação e rota direta com a mesma autorização |
| ADM-03 | Admin da organização não instala software nem entra em telas globais; plataforma sem vínculo não abre parâmetros nem dados da organização |
| ADM-04 | Configuração virtual `0`, CAS com um vencedor e isolamento A/B |
| ADM-05 | Atualização troca telas sem duplicar grupo; retirada e reinstalação conservam UUID e configuração e eliminam rotas antigas |
| LIST-01 | Dois modelos elegíveis, um sem UI ou menu, descobertos fora do admin; não expostos e técnicos excluídos |
| LIST-02 | Lista, contagem, filtro e página sem vazamento A/B, registro privado ou tombstone; filtro desconhecido recusado |
| LIST-03 | FK invisível sem rótulo nem UUID; nenhum `__str__` sensível; picker não abre alvo negado |
| LIST-04 | Consultas limitadas por política e tipo de referência, não por linha ou célula; picker paginado |
| FORM-01 | Layouts customizado e padrão em leitura, criação e edição, fora do admin |
| FORM-02 | Campos write-only sem exposição, com preservar e limpar explícitos e persistência verificada |
| FORM-03 | Erro em aba oculta visível e focável; valores preservados; teclado, sem JS e largura 390px funcionando |
| FORM-04 | HTML 409 e API 412, sem sobrescrita e com auditoria apenas da escrita vencedora |
| FORM-05 | Revogação de FK entre GET e POST e atribuição indevida de campos recusadas sem escrita residual |
| FORM-06 | Exclusão e restauração preservam UUID, CAS, referências e unicidade; acesso fora do formulário comum |
| SAFE-01 | Metadados inseguros, tabelas no admin, campo fora da allowlist e rotas externas recusados no pacote e no staging |
| SAFE-02 | Desativação bloqueia negócio aberto anteriormente; painel de parâmetros continua permitindo reativar |
| SAFE-03 | Artefato v1 assinado permanece verificável; v2 exige núcleo compatível; upload não importa código incompatível |
| SAFE-04 | Migration ou check inválido impede publicação do destino; recuperação conserva release e configuração anteriores |

## Referências

[Arquitetura](architecture.md), [dados](data-model.md), [acesso](access-control.md), [auditoria](audit-soft-delete.md), [catálogo](table-catalog.md), [extensão](extending.md), [API](api.md), [deployment](deployment.md) e [evidência](testing.md).
