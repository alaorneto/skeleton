# Administração de apps e interfaces padronizadas

**Estado: contrato de administração implementado no core e agente 0.2.0, com renderer e dimensões evoluídos no 0.3.0.** Este documento registra o contrato que resultou do planejamento, incluindo a instrução posterior de que o admin serve somente para configurações e parametrizações. As evidências de execução ficam em [testing](testing.md); a existência do contrato não substitui prova de um backend de deployment.

## 1. Fronteiras das interfaces

| Superfície | Responsabilidade | Não faz |
|---|---|---|
| `/admin/` | Configurações/parametrizações da plataforma, grupos de apps e controle de releases | Não lista tabelas nem apresenta formulários de edição de registros |
| `/platform/records/` | Registros globais autorizados: identidade, organizações, catálogo e auditoria | Não é o painel de configurações; não abre negócio tenant |
| `/o/<org>/apps/` e `apps/<app_label>/admin/` | Configuração, ativação e parâmetros organizacionais das apps | Não apresentam tabelas, CRUD ou atalhos de modelos |
| `/o/<org>/catalog/` e `tables/<table_key>/` | Descoberta e viewer padronizado de negócio: lista, detalhe e formulário | Não instalam software nem administram configuração de plataforma |
| `/api/v1/o/<org>/tables/<table_key>/` | Recursos de negócio e seleção autorizada de referências | Não aceitam classe Python, nome SQL ou bypass de autorização |

Há **um único `PlatformAdminSite` e um namespace `admin`**, não duas instâncias com políticas divergentes. As rotas de registros convencionais desse site foram movidas para `/platform/records/`, com shell web próprio e sem sidebar de configurações. Seus nomes `admin:<app>_<model>_*` continuam resolvendo, agora fora do painel. As antigas URLs de tabelas em `/admin/<app>/<model>/` retornam 404, sem aliases. `admin:index` resolve `/admin/`; `admin:records_index` resolve `/platform/records/`.

O namespace de uma rota é uma identidade de integração Django, não o painel em que ela aparece. A separação efetiva é a URL, o conteúdo e o layout. Os serviços de identidade, CAS, desativação, último administrador, MFA e CSRF continuam protegendo os registros globais.

“Toda tabela de negócio” continua significando `SecuredRecord` explicitamente exposto por `TableSpec.expose_api=True`, com código instalado e app habilitada no tenant. Tabelas globais, técnicas, desconhecidas, modelos não expostos e schema retido de apps retiradas não entram no viewer tenant. O catálogo físico não concede acesso.

## 2. Grupo automático de configuração

Cada manifesto carregado cria um grupo imutável de apresentação na memória do processo. Sua identidade é `app_id`; `app_label` identifica modelos/rotas e não é renomeado numa atualização. Não há tabela de menus/layouts ou DDL no `ready()`.

O grupo da plataforma contém visão geral, versão/origem/compatibilidade, seleção de organização, documentação local escapada e telas próprias de **parâmetros**. Não mostra registros, contagens de negócio ou valores de configuração de outras organizações. A seleção lista apenas vínculos ativos do ator e não concede vínculo/papel adicional.

A configuração tenant continua em `AppInstallation`, com revisão virtual 0 para a primeira escrita. Campos escalarmente declarados recebem rótulo, help text e seção opcionais. A gravação usa o mesmo `configure_application`, bloqueia a organização antes de avaliar revisão/dependências e não sobrescreve valores persistidos por causa de uma mudança visual.

Configuração e parâmetros tenant exigem `organization_admin`; usuário com leitura de negócio usa o catálogo/viewer, não o painel de parâmetros. Telas de configuração permanecem acessíveis quando a app está desativada. Rotas próprias de negócio/API, listas, formulários e policies continuam bloqueadas pela ativação.

Configuração global não é inferida a partir da configuração tenant. Uma app que precisa de parâmetros globais registra uma tela própria e usa seu modelo/serviço global específico. Segredos pertencem ao deployment privado, nunca ao JSON organizacional.

### Lifecycle

| Evento | Apresentação | Persistência |
|---|---|---|
| Instalar | Grupo aparece no release confirmado; app local também recebe grupo | Defaults sem materializar linhas em todos os tenants |
| Atualizar | Seções/telas/layouts recompostos por IDs estáveis | Valores e UUIDs preservados; mudanças incompatíveis exigem migration |
| Desativar no tenant | Parâmetros disponíveis; negócio bloqueado | Dados, configuração e ACL conservados |
| Retirar globalmente | Nenhuma tela executável da app; histórico no controle de releases | Schema, audit, migrations e configuração retidos |
| Reinstalar | Grupo volta após validação | Ativação explícita por organização; IDs não recriados |
| Falhar | Sem publicação parcial do destino | Manutenção/backup/recuperação seguem o agente |

## 3. Manifesto canônico v2

`manifest_version=2` conserva o piso histórico core >=0.2.0. Novos manifestos gerados por `init` começam com `>=0.3.0,<1.0.0`. A presença de `col_span` ou `rows` em um objeto de campo de `runtime.model_ui`, inclusive com valor `null`, exige piso >=0.3.0. O manifesto fonte deve declarar esse piso antes de `build`; publicação por `build_artifact` valida a apresentação e acrescenta o piso necessário antes de assinar. Leitura/verificação/reassinatura de artefatos v1 e v2 históricos conserva os bytes do JSON assinado, digest e inventário, sem normalizar nem inserir defaults; somente as representações em memória recebem defaults de apresentação.

`runtime.administration` contém `configuration_sections`, `sections` e `screens`. A tela administrativa admite somente `configuration` ou `custom`; **`model_list`, `table_key` e `action` são recusados nessa seção**. Metadados de tabelas pertencem exclusivamente a `runtime.model_ui` e navegação de negócio.

| Campo administrativo | Regra |
|---|---|
| `id`, `section_id`, `label`, `order` | IDs simples, únicos/estáveis; seção existente; ordenação com desempate por ID |
| `scope` | `platform` ou `organization` explícito |
| `kind` | `configuration` ou `custom` de parâmetros |
| `url_name` | Somente custom tenant, resolvível no namespace da própria app |
| `handler_id` | Somente custom plataforma, callable registrado pelo código da app |
| `organization_admin_only` | Obrigatório true para custom tenant |
| `sensitive` | Exige reautenticação recente na tela global |

Handlers recebem a request já protegida; não recebem permissão universal nem imports definidos pelo cliente. Seus módulos pertencem à app e IDs não colidem. Código assinado é confiável para execução, **não** sandbox: telas próprias têm contrato de parâmetros e não devem implementar um viewer disfarçado.

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
    "list": {"columns": ["code", "title", "updated_at"], "filters": ["code"], "title_field": "title"},
    "form": {
      "tabs": [{"id": "general", "label": "Identificação"}, {"id": "content", "label": "Conteúdo"}],
      "sections": [
        {"id": "identification", "label": "Identificação", "tab_id": "general", "columns": 2, "fields": [{"name": "code", "label": "Código", "col_span": 4}, {"name": "title", "label": "Título", "col_span": 8}]},
        {"id": "content", "label": "Conteúdo", "tab_id": "content", "fields": [{"name": "body", "label": "Texto", "help_text": "Conteúdo da anotação.", "widget": "textarea", "col_span": 12, "rows": 8}]}
      ]
    }
  }]
}
```

Esse fragmento pertence ao `runtime` do `applications/<package>/manifest.json` canônico de um manifesto completo; não é um pacote ou spec de segurança independente. Registre-o com `AppManifest.from_json(..., app_config=self)` em `ready()`, junto ao modelo elegível e seu `TableSpec`, e use as URLs genéricas do viewer. Não há `ui.json` paralelo, editor de layout no admin, Form Python ou template próprios obrigatórios. Notes 0.2.0 oferece o exemplo completo em `examples/notes/manifest.json`, com compatibilidade `>=0.3.0,<1.0.0`.

### Validação

Antes de importar código, o pacote valida schema fechado, tipos, ownership, namespaces, limites e assinatura/inventário. Há limites de 100 entradas por lista de seções/telas/abas, 200 modelos de apresentação, 200 entradas de campo por layout, labels de 200 e help text de 2000 caracteres.

Com Django no staging, `skeleton.E013` verifica modelos expostos da própria app, campos concretos/allowlists, cobertura de formulário, widgets, título escalar, rotas próprias HTML/API, handlers e colisões com `admin/`. Declaração inválida falha antes do cutover; não ganha fallback silencioso.

## 4. Viewer de negócio fora do admin

`TableSpec` permanece a autoridade sobre modelo/leitura/escrita/sensibilidade. `ModelUISpec`, `ListSpec` e `FormLayoutSpec` são apresentação imutável, indexada por `table_key`, sem ampliar exposição ou permissões. O catálogo e dashboard agrupam modelos legíveis por app; um modelo elegível sem menu/layout continua descoberto.

A lista usa colunas declaradas, título escalar seguro, filtros exatos visíveis, paginação e estado vazio. Sem declaração, usa rótulo seguro, UUID e atualização. Não chama `__str__` de negócio nem entrega objetos ORM relacionados crus ao template. Referências invisíveis não mostram label/UUID. A projeção avalia visibilidade em lote por política de alvo, não por célula; `.only()` inclui as dependências de metadados para evitar consultas diferidas por linha.

Tenant, ativação, permissão de tabela e visibilidade precedem filtro/count/paginação. HTML mantém 25 registros e ordem `created_at, sysid`; API mantém 25 e máximo 100. Não há busca regex/DSL, ordenação livre, bulk editing ou joins escolhidos pelo cliente. Filtros vazios do painel HTML são omitidos, chaves desconhecidas continuam recusadas e paginação preserva filtros validados.

### Referências

- API: `/api/v1/o/<org>/tables/<table_key>/references/<field>/`.
- Página seletora: `/o/<org>/tables/<table_key>/references/<field>/`.
- Query aceita somente `record`, `access` (`0/1`), `page` e `page_size`, sem duplicatas.
- Criação exige capacidade create na origem; edição recebe `record` e exige update; acesso exige manage_access no registro. Campo/alvo nunca são uma tabela livre.
- Alvos de negócio usam `authorized_queryset`; identidade/acesso usam resolvedores dedicados ativos/same-tenant. Identidades são representadas por UUID, não lista global de emails.
- POST revalida a referência, mesmo que ela fosse legível quando o formulário abriu.
- JavaScript melhora a seleção com uma janela auxiliar e mensagem same-origin vinculada ao campo/registro. Sem JavaScript, a página paginada permite copiar o UUID para o formulário aberto.

## 5. Formulário e detalhe padronizados

Criação, edição e leitura usam Django Forms renderizados no servidor por **django-crispy-forms==2.7** e pelo pack próprio `skeleton_crud`, distribuído no wheel do core 0.3.0. Crispy é somente o motor de apresentação: o core compila o envelope seguro em layout, sem substituir autorização, validação, CAS, serviços ou persistência. Não há pack externo, CDN ou frontend adicional. Sem JavaScript, as seções ficam sequenciais. Erros selecionam a aba correta, conservam valores enviados e oferecem resumo focável e links para campos.

| Dimensão | Contrato |
|---|---|
| `col_span` | Omitido/`null` conserva largura automática; inteiro exato de 1 a 12 define a fração da grade de 12 unidades |
| `rows` | Omitido/`null` conserva o widget; inteiro exato de 1 a 40 define altura de textarea editável, sem alterar validators ou limites |
| `columns` da seção | 1 ou 2 define somente a largura padrão: 12 ou 6; não limita spans explícitos |
| Ordem e mobile | Arrays de abas/seções/campos definem DOM e foco, sem compactação densa; até 600px todos os campos ocupam a linha inteira |

Bool, string, float e inteiros fora dos intervalos são recusados. Sem span explícito, campos largos também usam 12. `rows` não é permitido com `read_only=True`, relações, inputs ou selects; override explícito exige `textarea`/`json` compatível com o tipo, e sem override o widget efetivo deve ser `forms.Textarea`. Validação pura, check `skeleton.E013` e formulário verificam esse contrato, sem conversão implícita. Labels/help são texto escapado, não templates ou HTML executável.

Perfil, gestão dedicada de acesso e configuração usam o mesmo renderer de formulários, sem receber layout de negócio. Sem seções declaradas, campos visíveis do formulário já construído recebem a seção padrão `fields`/`Dados do registro`, na sua ordem e com span 12. O shell conserva um CSRF, campos hidden uma vez e `form.media` uma vez; o helper crispy não cria outro `<form>` nem renderiza campos omitidos silenciosamente.

- Todo campo gravável aparece exatamente uma vez na edição; sem layout usa-se `write_fields`.
- Campo somente leitura deve estar em `read_fields` ou metadados sistêmicos permitidos. Leitura acrescenta campos legíveis não posicionados em uma seção padrão.
- Campo gravável não legível nunca revela valor persistido na edição. Controle `__change_<campo>` distingue preservar de substituir/limpar; não é repassado ao serviço ou aceito pela API como campo de modelo.
- Obrigatoriedade, choices, validators e limites continuam Django/modelo/serviço. Widgets são allowlist compatível com tipo; nullable boolean conserva null e não vira false implicitamente.
- Datas/horas HTML usam fuso do perfil; persistência continua UTC.
- FK usa UUID e picker limitado, sem carregar todos os alvos em `ModelChoiceField`.
- Owner/visibilidade/grupo mudam apenas pela ação de acesso separada. UUID/tenant/ator/revisão não entram na escrita comum.
- Delete/restore são confirmações com revisão/CSRF, sem campos de negócio ou hard delete; ator delete-sem-read não recebe título nem resumo do registro. Cancelar não escreve: criação volta à lista, edit/access/delete de registro vivo volta ao detalhe somente se leitura for permitida (caso contrário, à lista), e restore volta à lista de excluídos. Destinos são rotas fixas resolvidas por `reverse`, não URLs fornecidas pelo cliente.
- HTML stale retorna 409; API stale retorna 412; não há retry que sobrescreva concorrência.
- FileField requer superfície dedicada: não ganhou upload genérico neste contrato.

### Serviços específicos da app

Uma app pode registrar `RecordOperations` em código por `table_key`, com handlers create/update/delete/restore próprios. Os serviços recebem `(record, *, user, values)` em create/update e `(record, *, user)` no lifecycle. O core valida campos, coerção, referência e autorização antes de chamar o serviço; ele persiste usando CAS na mesma transação e não substitui triggers de auditoria.

HTML e API usam o mesmo dispatcher. Se há adaptador e uma operação não foi declarada, ela é negada; não há fallback ao CRUD genérico. Sem adaptador, os serviços atuais continuam sendo usados. Handlers pertencem à app, assinaturas/colisões são verificadas e retirada limpa os registros em memória. Notes não precisa de adaptador.

## 6. Atualização e configuração persistida

O preview aprovado mostra alterações de grupos, seções, telas, layouts e configuração além de arquivos/migrations. Redução de limite, troca de tipo ou remoção de campo de configuração recebe indicação de incompatibilidade. Reordenar apresentação não apaga valores.

O bloqueio antigo que recusava toda mudança de `configuration_fields` foi substituído por validação do contrato e check real após migrations. `check_application_configuration` usa transações independentes por organização e credencial runtime para validar configurações persistidas dos apps carregados. Campo removido/renomeado exige migration explícita com modelos históricos; não existe transformação escolhida por HTTP. Falha impede cutover e conserva manutenção. Alterar `default_enabled` de uma instalação existente continua exigindo revisão explícita, sem ativação silenciosa.

Atualize o agente independente para 0.3.0 e o core do release para >=0.3.0 antes de publicar/consumir dimensões. O piso histórico do formato 2 sem dimensões continua 0.2.0. O web continua sem pip, DDL ou credencial owner. Este escopo não acrescenta entidades do núcleo nem expõe `SystemRecord`/`OrganizationRecord` por um gateway genérico: seus serviços e superfícies dedicados permanecem.

## 7. Mapa da implementação

| Subsistema | Arquivos principais |
|---|---|
| Contratos e checks | `system/packages.py`, `app_registry.py`, `presentation.py`, `checks.py` |
| Configuração/admin | `accounts/admin_site.py`, `system/administration.py`, `application_views.py`, `application_urls.py`, `conf/urls.py` |
| Viewer/forms | `web/views.py`, `forms.py`, `presentation.py`, `form_engine.py`, `templatetags/crud_forms.py`, `references.py`, `services.py`, pack `templates/skeleton_crud` e static |
| API/picker/dispatch | `api/views.py`, `api/urls.py`, `system/record_operations.py` |
| Release/config check | `deployment_agent/releases.py`, `service.py`, `check_application_configuration`, preview do release |

## 8. Critérios de aceitação

Os IDs do planejamento são conservados, com ADM atualizado pela separação solicitada pelo usuário. Resultados e comandos observados são registrados em [testing](testing.md), não inferidos da tabela abaixo.

| ID | Critério |
|---|---|
| ADM-01 | Grupo único de parâmetros após instalação real e para app local; admin sem registros/modelos |
| ADM-02 | Seções/telas de configuração próprias ordenadas; navegação e rota direta com mesma autorização |
| ADM-03 | Admin tenant não instala software nem entra em global; plataforma sem vínculo não abre parâmetros/dados tenant |
| ADM-04 | Configuração virtual 0, CAS com um vencedor e isolamento A/B |
| ADM-05 | Update troca tela sem duplicar grupo; retirada/reinstalação conservam UUID/configuração e eliminam rotas antigas |
| LIST-01 | Dois modelos elegíveis, um sem UI/menu, descobertos fora do admin; não expostos/técnicos excluídos |
| LIST-02 | Lista/count/filtro/página sem A/B, privado ou tombstone; filtro desconhecido recusado |
| LIST-03 | FK invisível sem label/UUID; nenhum `__str__` sensível; picker não abre alvo negado |
| LIST-04 | Queries limitadas por política/tipo de referência, não por linha/célula; picker paginado |
| FORM-01 | Layouts customizado/default em leitura/criação/edição, fora do admin |
| FORM-02 | Write-only sem disclosure, preservar/limpar explícitos e persistência observada |
| FORM-03 | Erro em aba oculta visível/focável; valores conservados, teclado/no-JS/390px |
| FORM-04 | HTML 409/API 412, sem overwrite e com auditoria somente da escrita vencedora |
| FORM-05 | Revogação de FK entre GET/POST e mass assignment recusados sem escrita residual |
| FORM-06 | Delete/restore preservam UUID/CAS/referências/unicidade; acesso fora do form comum |
| SAFE-01 | Metadados inseguros, tabelas no admin, campo fora da allowlist e rotas externas recusados no pacote/staging |
| SAFE-02 | Desativação bloqueia negócio aberto antes; parâmetros permitem reativar |
| SAFE-03 | V1 assinado permanece verificável; v2 usa core compatível; upload não importa código incompatível |
| SAFE-04 | Migration/check inválido não publica destino; recuperação conserva release/configuração anteriores |

## Referências

[Arquitetura](architecture.md), [dados](data-model.md), [acesso](access-control.md), [auditoria](audit-soft-delete.md), [catálogo](table-catalog.md), [extensão](extending.md), [API](api.md), [deployment](deployment.md) e [evidência](testing.md).
