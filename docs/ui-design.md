# Interface SSR: linguagem visual, tokens e build do CSS

A interface do Skeleton é renderizada no servidor com templates Django. O sistema visual usa **Tailwind CSS 4** e componentes do **Flowbite 4** (navbar, dropdown, collapse, tabs, breadcrumb, cards, tabelas, alertas, badges e paginação). O CSS é **compilado e versionado** dentro do pacote: o wheel não depende de Node em runtime e nenhum asset é carregado de CDN.

Este guia especifica os tokens visuais, os componentes reutilizáveis e o processo de build. Ele não altera o contrato de segurança: formulários, CSRF, CAS, autorização e autoescape seguem exatamente [arquitetura](architecture.md) e [extensão](extending.md).

## Direção visual: "Cobalto e Ardósia"

| Papel | Token | Claro | Escuro | Uso |
|---|---|---|---|---|
| Marca | `brand` | cobalto-600 `#3859ed` | cobalto-600 | botão primário, foco, aba ativa |
| Marca forte | `brand-strong` | cobalto-700 | cobalto-700 | hover e active do botão primário |
| Texto de marca | `fg-brand` | cobalto-700 `#2d49d0` | cobalto-300 | links, eyebrow, item ativo |
| Fundo | `canvas` | `#fafbfd` | `#0a0c12` | `body` |
| Superfície | `neutral-primary-soft` | branco | `#11141c` | cartões, cabeçalho, tabelas |
| Superfície secundária | `neutral-secondary-soft` | slate-50 | ink-850 | cabeçalho de tabela, painéis |
| Campo | `neutral-secondary-medium` | branco | ink-850 | inputs, selects |
| Bordas | `default` / `default-medium` / `default-strong` | slate-200/300/400 | ink-750/700/600 | divisórias, campos, hover |
| Texto | `heading` / `body` / `body-subtle` | slate-900/600/500 | slate-50/300/400 | títulos, corpo, metadados |
| Sucesso | `success*`, `fg-success*` | emerald | emerald 300–400 | mensagens e badges |
| Aviso | `warning*`, `fg-warning*` | amber | amber 300–400 | confirmações, avisos |
| Perigo | `danger*`, `fg-danger*` | rose | rose 300–400 | erros, exclusão |
| Foco | `focus` | cobalto-600 | cobalto-400 | contorno de 2 px, afastamento de 2 px |

- O matiz 268 (entre azul e índigo) mantém sobriedade e convive bem com o botão oficial gov.br (`#1351b4`).
- Os neutros `ink` do modo escuro recebem leve matiz da cor da marca para coesão visual.
- Contraste medido (WCAG 2.2 AA para texto pequeno): branco sobre `brand` 5,5:1; `fg-brand` sobre branco 7,1:1; `fg-brand` escuro sobre superfície 10:1; `body-subtle` sobre o fundo 4,6:1 (claro) e 7:1 (escuro).
- Links no meio de parágrafos são sublinhados, para que a cor não seja o único indicador.

Esses valores vivem em `frontend/src/tokens.css`, que define as escalas primitivas (`cobalt-*`, `ink-*`) e o mapeamento semântico do Flowbite 4. A escala `--color-cobalt-*` é o ponto de troca da marca tanto no build (editando a escala e recompilando) quanto em tempo de execução, via tema ativo da instância ([marca e tema da instância](#marca-e-tema-da-instância)), que redefine a escala inteira em folha própria sem rebuild. Por isso, o `site.css` compilado preserva `--color-brand: var(--color-cobalt-600)` e os demais tokens da marca como referências à escala (`tests/unit/test_appearance_themes.py` verifica isso).

### Marca por organização

Em `/o/<org>/settings/`, o administrador escolhe nome de exibição, cor primária e logo ([#168](https://github.com/alaorneto/skeleton/issues/168)). A mesma tela inclui a seção **Endereço próprio** ([#480](https://github.com/alaorneto/skeleton/issues/480)), no mesmo formulário e botão de salvar: quando o domínio base está configurado e o slug é um rótulo DNS válido, mostra o endereço resultante, o aviso do que se torna público e a opção **Ativar endereço próprio**; caso contrário, informa apenas o motivo.

Nas telas da organização, nome e logo substituem "Skeleton" no cabeçalho. A cor sobrescreve apenas `--color-brand`, `--color-brand-strong`, `--color-fg-brand` e `--color-fg-brand-strong` por meio de um `<style>` em `base.html`:

- **Contraste:** a cor precisa atingir WCAG AA (4,5:1) com branco (usado em botões e sobre fundo claro). No modo escuro, os tokens de texto recebem uma variante clareada até atingir 4,5:1 sobre o fundo escuro. O cálculo está em `skeleton/tenancy/branding.py`.
- **CSS:** é gerado somente a partir de valores `#rrggbb` validados (reforçado por constraint no banco), nunca de texto livre. Com CSP global, esse `<style>` utiliza nonce.
- **Logo:** aceita PNG, JPEG ou WebP de até 512 KB, verificado pelo conteúdo (SVG é recusado). É servido inline apenas para membros em `/o/<org>/settings/logo/`, com `Cache-Control: private`, `nosniff` e `ETag`. Fora das telas da organização (`/me`, login, `/platform/`), prevalece a marca da instância ou o tema do núcleo.

### Marca e tema da instância

Em `/platform/appearance/`, o administrador da plataforma configura nome do produto, rodapé, logo (com variante opcional para modo escuro), ícone e tema ativo para toda a instância ([aparência](appearance.md), epic [#591](https://github.com/alaorneto/skeleton/issues/591)). A precedência é sempre **organização → instância → núcleo**:

- **Folha do tema:** `base.html` (além do portal e das páginas de erro) insere `<link rel="stylesheet" href="{{ instance_brand.theme_url }}">` no bloco `theme_link`, **depois** de `site.css` e **antes** do `<style nonce>` da organização, de modo que a organização prevalece pela ordem. Sem tema ativo, nenhum `<link>` é emitido e o HTML permanece inalterado. CSS livre nunca é embutido no HTML.
- **Nome:** `site_name` resolve para o nome da organização no host próprio dela, depois para `instance_brand.name` e, por fim, "Skeleton". Títulos terminam com `· {{ site_name }}` (e na plataforma, `Plataforma · {{ site_name }}`).
- **Logo do cabeçalho:** segue a ordem logo da organização → logo da instância (enviado ou, na ausência, o do tema ativo de pacote, inclusive SVG) via `components/instance-logo.html` (um `<picture>` com `<source media="(prefers-color-scheme: dark)">` apenas quando há variante escura e a pessoa segue o sistema; quando o modo é forçado, o servidor escolhe diretamente a imagem) → ícone do núcleo. O layout de entrada exibe o logo ou o nome da instância no cartão.
- **Favicon e manifesto:** favicon segue ícone da organização → ícone da instância (`/brand/icon/?v=…`, enviado ou do tema de pacote) → `favicon.svg` (`/favicon.ico` segue a mesma ordem). O manifesto PWA é `/site/manifest.webmanifest` no host da organização e `/brand/manifest.webmanifest` (`instance_brand.manifest_url`) no domínio principal, emitido apenas quando a instância define nome, ícone ou cor. `<meta name="theme-color">` usa a cor da organização ou, na falta dela, a do tema da instância.
- **Rodapé:** havendo nome ou rodapé da instância, exibe `footer_text` e `site_name`; caso contrário, mantém o texto padrão do Skeleton.
- **Páginas de erro:** `error_base.html` usa `{% load appearance %}{% appearance_cached as brand %}`, lendo apenas da memória do processo (nunca do banco) e mantendo-se sem `{% url %}` nem variáveis de contexto.
- **Modo seguro:** páginas sob `/platform/appearance/…` carregam o tema com `safe=1` (somente tokens, sem CSS livre); apenas a pré-visualização sobrescreve `theme_link` com o CSS completo do candidato.
- Setup e página de manutenção continuam usando o tema do núcleo, pois são renderizados antes de acessar o banco.

O processador `organization_shell` expõe `instance_brand` em todo contexto com `name`, `footer_text`, `color`, `theme_url`, `logo_url`, `logo_dark_url`, `icon_url` e `manifest_url` (URLs versionadas com `?v=<versão>`, vazias quando não configuradas).

Para criar um tema a partir de um sistema de design, siga [criar e publicar um tema](themes.md). O **contrato estável para CSS personalizado** inclui:

- tokens `--color-*` (escalas `cobalt-*` e `ink-*` e tokens semânticos `brand*`, `fg-*`, `neutral-*`, `default*`, `success*`, `warning*`, `danger*`, `focus`), `--radius-base`, `--radius-card`, `--font-sans` e `--font-body` (pacotes com fontes inserem sua família no início de `--font-sans`);
- classes de componente documentadas nesta página (`.card`, `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-danger`, `.badge*`, `.counter`, `.attention-dot`, `.activity-pill`, `.spinner`, `.danger-zone*`, `.create-panel`, `.message` + `.info`, `.alert*`, `.page-header`, `.page-eyebrow`, `.page-description`, `.sidebar*`, `.link-tabs`, `.table-scroll`, `.table-stack`, `.form-stack`, `.form-actions`, `.error-summary`, `.app-header`, `.app-footer`).

Classes utilitárias do Tailwind e a estrutura interna dos templates não são contrato estável e podem mudar entre versões.

### Modo escuro

Cada pessoa pode escolher **Claro**, **Escuro** ou **Seguir o sistema** (padrão), sem JavaScript, sem flash de tema e compatível com CSP (epic [#667](https://github.com/alaorneto/skeleton/issues/667)):

- **Onde escolher:** em `/profile/` (campo "Tema") e no menu da conta no cabeçalho, via três botões de envio em um `<form method="post">` para `/me/appearance/` (com CSRF e revisão do perfil; o formulário nunca transporta URL de retorno — a volta usa `Referer` apenas se for do mesmo host, senão `/profile/`). Revisão desatualizada não grava e exibe aviso. O seletor só é exibido em respostas que possuem token CSRF.
- **Onde fica:** `UserProfile.color_scheme` (`""`, `light` ou `dark`) é a fonte de verdade. O cookie `sk_color_scheme` (`light` ou `dark`, `HttpOnly`, `SameSite=Lax`, validade de 1 ano, no domínio do cookie de sessão) espelha a escolha para telas pré-login e pós-logout no mesmo navegador. `skeleton/web/color_scheme.py` resolve a precedência perfil → cookie → sistema, e `ColorSchemeMiddleware` preenche `request.color_scheme` e sincroniza o cookie fora de `/api/`.
- **Como é aplicado:** `<html data-color-scheme="light|dark">` é emitido somente quando o modo é forçado, acompanhado por `<meta name="color-scheme">` (`light`, `dark` ou `light dark`). Em `frontend/src/tokens.css`, o variant `dark` casa `[data-color-scheme=dark]` ou, dentro de `@media (prefers-color-scheme: dark)`, elementos fora de `[data-color-scheme=light]`, sempre envolvidos em `:where()` para preservar a especificidade de `:root`. Nunca use a classe `dark`, pois ela ativaria o bloco `.dark` do Flowbite.
- **Tema e marca:** a folha do tema recebe `&scheme=` ([aparência](appearance.md#rotas-públicas)) e o `<style>` da organização passa pela mesma substituição de consultas `prefers-color-scheme` (`skeleton/appearance/color_scheme.py`), permitindo que temas continuem usando `@media (prefers-color-scheme: dark)` normalmente.
- **Seguem o sistema:** páginas de erro (sem contexto), setup, manutenção e e-mails.

### Tipografia

- **IBM Plex Sans Variable** (OFL 1.1), auto-hospedada em `static/skeleton/web/vendor/ibm-plex-sans-5.3.0/` (subsets `latin` e `latin-ext`, normal e itálico verdadeiro, pesos 100 a 700, `font-display: swap`), sem requisições externas. O desenho distingue claramente `l`, `I` e `1`. Temas com `font: default` herdam essa fonte; `system` e `serif` usam pilhas do sistema.
- Monoespaçada do sistema (`ui-monospace`, Cascadia Mono, SF Mono…) para UUIDs, digests e códigos.
- Escala tipográfica: H1 28/36 semibold (24/32 no celular), H2 18/28, H3 16/24, corpo 16/24, tabelas e metadados 14/20, rótulos de seção 12–14 em maiúsculas com tracking. Tabelas utilizam algarismos tabulares.
- Campos de entrada usam 16 px no celular (evitando zoom automático no iOS) e 14 px a partir de 640 px.

### Espaçamento, forma e profundidade

- Grade base de 4 px; contêiner máximo de 1280 px com gutter de 16/24/32 px; telas de detalhe e formulário limitam-se a 1024 px.
- Cabeçalho de página padrão (`.page-header`): breadcrumb ou eyebrow → `h1` → descrição → espaçamento de 32 px (24 px no celular) até o conteúdo; ações alinhadas à direita no desktop e abaixo no celular.
- Raios: controles 8 px (`rounded-base`), cartões 12 px (`rounded-card`), avatares e pílulas totalmente redondos.
- Profundidade construída por bordas e sombra sutil (`shadow-card`), reservando sombra elevada (`shadow-pop`) para menus suspensos.

### Anatomia de uma página

```django
<div class="page-header">
  <div class="min-w-0">
    [breadcrumbs OU <p class="page-eyebrow">Área</p>]
    <h1>Título</h1>
    [<p class="page-description">Uma frase.</p>]
  </div>
  [<nav class="toolbar" aria-label="Ações">…</nav>]
</div>
```

- O breadcrumb fica dentro do primeiro `div` de `.page-header`. Toda tela apresenta eyebrow ou breadcrumb, e listas trazem descrição de uma frase.
- **Texto da página** ([#780](https://github.com/alaorneto/skeleton/issues/780)): a descrição explica em uma ou duas frases curtas o que a pessoa faz ali, na linguagem de quem usa. Termos de implementação (schema, payload, transação, migration, CSS, UUID, worker, API interna) não vão para a interface — pertencem à documentação. Formulários sem seções declaradas usam "Informações gerais" (nunca "Dados do registro"). Cartões de navegação no hub de configuração (`SettingsItem.description`) levam uma linha descrevendo a tarefa. Em telas de lista com criação na própria tela, a lista vem primeiro e o formulário fica recolhido em `<details class="create-panel">` (sem script), abrindo automaticamente se voltar com erro.
- A barra de ações no cabeçalho usa `<nav class="toolbar" aria-label="Ações">`, com botão principal `btn btn-primary` (`.button` permanece apenas como alias legado).
- O título da aba segue `<título da tela> · Skeleton` (ou `<título> · Plataforma · Skeleton` na plataforma).
- **Largura** ([#803](https://github.com/alaorneto/skeleton/issues/803)): toda tela dentro do shell ocupa a largura total do grid (`max-w-7xl` em `base.html` e `account/base_manage.html`, ao lado da barra lateral quando houver). Nem o conteúdo nem cartões ou formulários de seção definem `max-w-*` próprio; a largura de leitura fica nos textos (`page-description` e `max-w-prose`). Exceções restritas a formulários simples/mensagens centralizados (entrada/login, erro, manutenção, setup sem etapas, acesso negado, cadastro obrigatório de MFA) e à página pública em blocos; `tests/unit/test_page_width.py` lista cada exceção com justificativa e bloqueia qualquer outro `max-w-*` em templates.

### Portal de pessoas externas

As páginas do [portal](portal.md) estendem `skeleton/portal/base.html`: cabeçalho com a marca da organização (logo, nome e cores, conforme [marca por organização](#marca-por-organização)), botão de sair e conteúdo principal, sem barra lateral, menus de membros, contadores nem JavaScript. Os componentes padrão (`link-card`, tabelas, formulários e `error-summary`) continuam disponíveis.

## Navegação

A interface divide-se em áreas distintas, sempre identificáveis pelo cabeçalho e pelo menu lateral:

| Área | Entrada | Conteúdo |
|---|---|---|
| Organização (`/o/<org>/…`) | Seletor de organização no cabeçalho | Menu lateral com **Início** e as entradas `navigation` dos apps ativos (cada uma com o ícone da app e marcada com `aria-current`; quando fora das entradas diretas, como num registro ou formulário, marca a primeira entrada da app via `mark_current_entry`; sem apps, exibe "Nenhum aplicativo disponível."). Ao lado do nome da organização aparece a **engrenagem** (`aria-label="Configuração da organização"`) para quem tem acesso a algum item de configuração. Tarefas e Notificações ficam no cabeçalho em pílula dividida. Telas de registro (`web:records`, `web:record`…) mantêm este menu |
| Configuração da organização (`/o/<org>/admin/`) | Engrenagem do menu lateral | Hub com cartões por grupo e, em toda rota da área (`SETTINGS_ROUTE_NAMES`), menu lateral de configuração com "Voltar para a organização", "Visão geral" e os grupos **Organização** (Geral; Domínios próprios, quando habilitado; Página pública; Página inicial; Notificações, com `notifications.manage`), **Pessoas e acesso** (Pessoas, Grupos, Papéis e permissões, Segurança, Pessoas externas), **Admissão** (Solicitações, Links de ingresso, Admissão, Domínios verificados, Identidade), **Aplicativos e integrações** (Aplicativos, Webhooks, Contas de serviço, Fluxos), **Dados** (Tabelas, Auditoria) e **Cobrança** (somente com `SKELETON_BILLING_ENABLED`). Cada item aparece apenas para quem passa em sua porta, com `aria-current` na tela atual |
| Administração da plataforma (`/platform/…`) | Link "Plataforma" no cabeçalho (somente `staff` + `superuser`) | Views próprias em `skeleton/web/platform`, com menu lateral agrupado por assunto (detalhes abaixo); cada link aparece só para o papel autorizado e grupos vazios são ocultados com o título |
| Minha conta | Menu da conta | Perfil, Email, Senha e Autenticação em dois fatores |
| Página inicial pública (`/` sem login) | Raiz do host da plataforma | Página em blocos publicada em `/platform/appearance/home/` no layout `pages/public.html` com a marca da instância; sem página publicada, redireciona ao login |

**Organização do menu da plataforma (`/platform/…`):**

- **Topo (sem título):** Visão geral, Configuração da instância, Atualizações (Atualizações da plataforma), Organizações, Clientes da API, Instâncias remotas e Notificações.
- **Identidade:** Usuários, Provedores de identidade e Pedidos de eliminação.
- **Aplicativos:** Aplicativos instalados, Pacotes de apps, Atualizações (Atualizações de aplicativos) e Clientes nativos.
- **Dados:** Catálogo de tabelas, Armazenamento e Auditoria global.
- **Operação:** Indicadores, Domínios verificados, Cobrança, Aparência e Jobs.

Em `/platform/notifications/`, qualquer papel da plataforma acessa a aba **Caixa** (avisos da plataforma para a pessoa, sinalizados pelo `attention-dot` sem número no menu e no link "Plataforma" do cabeçalho enquanto houver não lidos; veja [avisos da plataforma](notifications.md#avisos-da-plataforma)). O administrador completo acessa também as abas **Avisos** (catálogo de tipos, canais padrão e textos; [notificações](notifications.md#configuração-da-plataforma)) e **Comunicados** (mensagens às organizações com rascunho, revisão e envio confirmado; [comunicados](notifications.md#comunicados-da-plataforma)). **Aparência** (`/platform/appearance/`) é exclusiva do administrador completo.

As atualizações dividem-se em duas áreas apoiadas pelo mesmo motor ([plataforma e aplicativos](updates.md#plataforma-e-aplicativos)), ambas para quem tem `updates.operate`: **Atualizações da plataforma** (`/platform/updates/`, com abas sem número Visão geral, Histórico, Fontes e Enviar pacote; cartão do núcleo com o caminho de atualização onde só o primeiro passo tem botão, status do agente, nós do cluster e detalhes técnicos recolhidos) e **Atualizações de aplicativos** (`/platform/updates/apps/`, com abas Visão geral, Histórico e Enviar pacote para atualizar, instalar, retirar e reinstalar apps sem digitar digests ou IDs).

As telas de execução de um plano (escolher, conferir o impacto, aprovar e acompanhar) trocam as abas pelas etapas (`components/flow-steps.html`) e herdam a área da operação (`core_update` pertence à plataforma). Planos aguardando aprovação aparecem no topo da área com ações de revisar e descartar. Quando um passo do core exige agente mais novo, o fluxo atualiza o agente primeiro passando por uma página de espera auto-recarregada até exibir o plano (ou abre "Atualize o agente primeiro" com as instruções do layout quando o agente não suporta autoatualização); ao concluir, a página oferece o próximo passo. Nas demais telas de `/platform/`, quem tem `updates.operate` vê uma faixa de aviso (`message`, `role="status"`, bloco `notices` de `base.html`) e o `attention-dot` no item de menu correspondente quando há novidade, enquanto a visão geral resume o estado ou exibe "Tudo atualizado".

Regras complementares de navegação:

- **Seletor de organizações:** lista os vínculos ativos via `memberships_for_user`; apenas gera links, e toda view destino revalida autorização.
- **Endereço da organização:** o processador `organization_shell` injeta em todo contexto (inclusive anônimo) `site_mode` (`True` em `<slug>.<domínio base>`) e `organization_site` (o `Site` do índice — nomeado assim porque o allauth já ocupa `site` — com `organization_id`, `slug`, `name`, `brand_color`, `logo_key` e `default_language`). Em `site_mode`, o cabeçalho oculta o seletor de organização, "Todas as organizações" e "Plataforma" (mesmo para administradores da plataforma); "Notificações" e "Tarefas" (as duas metades de `activity-pill`) filtram e contam apenas aquela organização; o rodapé exibe apenas o nome da organização. Textos visíveis a membros comuns que mencionam "organizações" ou plataforma usam `{% if site_mode %}`; telas administrativas que necessariamente citam a plataforma (cobrança, disponibilidade de apps) mantêm o texto original.
- **Cadastro obrigatório de MFA:** enquanto uma regra exigir MFA ainda não cadastrado ([quem cadastra o MFA](authentication-sso.md#mfa-cifrado-e-obrigatório)), toda página redireciona ao cadastro de MFA e o shell oculta navegação principal, barra lateral e menu "Minha conta" (`request.mfa_enrollment_pending` em `skeleton/web/base.html` e `account/base_manage.html`), exibindo apenas a marca (link para a página de MFA) e o botão de sair.
- **Administração de acesso:** agrupada por tarefa em `skeleton/web/navigation.py` (`ACCESS_SECTIONS`) em **Pessoas** (membros, convites e papéis de membros), **Grupos** (grupos, membros e papéis de grupos) e **Papéis e permissões** (papéis, permissões de tabelas e concessões de registros). Cada seção vira item da área de configuração e seus recursos aparecem como abas na listagem.
- **Área de configuração:** `SETTINGS_GROUPS` em `skeleton/web/navigation.py` é tabela declarativa (`SettingsItem(url_name, rótulo, ícone, porta, rotas)`). As portas aceitas são capacidades (`members.view`, `access.manage`, `admission.decide`, `admission.configure`, `invitations.manage`, `identity.configure`, `external.manage`, `integrations.manage`, `flows.manage`, `apps.manage`), `organization_admin`, `custom_domains` (recurso ativo e administrador), `billing` (cobrança ativa e `billing.view`), `audit` (administrador ou permissão `audit` em alguma tabela), `member` (Tabelas) e `access:<seção>` (seção visível de `ACCESS_SECTIONS`). `organization_shell` expõe `settings_area` (rota atual em `SETTINGS_ROUTE_NAMES`), `settings_navigation` (grupos filtrados) e `can_open_settings` (há algum item além dos abertos a todo membro); o hub (`skeleton/web/settings_views.py`, `@tenant_page()`) responde 404 neutro a quem não tem acesso. O shell reaproveita as consultas já feitas por `application_navigation` sem repetir queries. A área apenas reorganiza links, sem alterar autorização de URLs diretas.
- **Parâmetros de apps:** telas de `parameter_panel` não exibem atalhos para rotas de negócio, seguindo o [contrato de administração](application-admin-ui-plan.md).
- **Busca global:** dentro de uma organização, quando existe ao menos uma tabela com `TableSpec.global_search`, o cabeçalho inclui `.header-search` como primeiro item de `header-end` (decidido em memória via `global_search_declared()`, sem consulta): no celular fica no menu recolhível; a partir de 1280 px (`xl`) aparece direto no cabeçalho; entre 768 e 1280 px vira apenas o ícone-link `.header-search-link` para a página de busca, com o e-mail do botão da conta limitado a `max-w-32`. É um formulário `GET` sem JavaScript apontando para `/o/<org>/search/?q=<termo>`. A página mostra um cartão por tabela com resultados e o link "Ver todos os resultados" quando há mais itens que o limite (abrindo a lista da tabela com o mesmo `q`); sem correspondências exibe "Nenhum resultado.", e termo inválido devolve status 400 com mensagem no campo. Fora de uma organização não há caixa de busca.
- No celular, o menu principal e o menu da organização são painéis recolhíveis (Flowbite collapse) que permanecem abertos sem JavaScript.

## Componentes para templates

Use estas classes nos templates do núcleo e das apps; elas encapsulam os padrões do Flowbite integrados aos tokens:

| Classe | Uso |
|---|---|
| `page-header`, `page-eyebrow`, `page-description`, `section-heading` | Cabeçalho de página e rótulos de seção. |
| `btn` + `btn-primary` / `btn-secondary` / `btn-danger` / `btn-danger-outline` / `btn-ghost`, `btn-sm` | Botões e links estilizados como botão (`.button` permanece como alias do primário). |
| `card`, `link-card` (+ `link-card-icon`, `link-card-title`, `link-card-text`) | Superfícies e atalhos navegáveis. Em grades (`<ul class="grid ..."><li>`), todos os `link-card` compartilham a altura mínima de título + duas linhas de descrição e acompanham a linha quando um vizinho cresce. |
| `table-scroll` | Tabela responsiva que rola dentro do cartão, nunca na página; classes de coluna e célula em [Tabelas](#tabelas). Lista vazia usa `components/empty-row.html` (tabela) ou `components/empty-state.html` (cartão ou lista); veja [Retorno ao usuário](#retorno-ao-usuário). |
| `pagination` | Componente `skeleton/web/pagination.html` (resumo "Mostrando X–Y de N" e botões Anterior/Próxima). |
| `components/cursor-pagination.html` | Paginação por cursor (`next_url`, `label`): exibe "Carregar mais" apenas quando há próxima página. A view monta `next_url` com `urlencode` preservando os demais parâmetros. |
| `stat-grid`, `stat` | Indicadores: `<dl class="stat-grid">` com `{% include "skeleton/web/components/stat.html" with label=… value=… hint=… key=… %}` (`key` vira `data-count`). Duas colunas no celular, quatro a partir de 640 px. |
| `filter-bar` | Filtro em linha: `<form method="get" class="filter-bar" aria-label="Filtros">`, um `<div class="field">` por campo com rótulo visível e `<div class="filter-actions">` com "Filtrar" e, apenas com filtro ativo, "Limpar" (`href="?"`). |
| `breadcrumbs`, `toolbar`, `filter-panel`, `record-tabs` | Navegação estrutural, barra de ações, painel de filtros e abas de registro. |
| `header-search` | Caixa de [busca global](#navegação) no cabeçalho (`<form class="header-search" method="get" role="search">` com rótulo `sr-only`, `input type="search"` e botão `btn btn-secondary btn-sm` com ícone). Uso exclusivo do cabeçalho; entre `md` e `xl` dá lugar a `header-link header-search-link`. |
| `link-tabs`, `segmented` | `link-tabs`: abas entre telas irmãs (`aria-current="page"`). `segmented` (`components/segmented.html` com `options=[{label, url, current}]` e `label`): alternância de filtro na mesma tela com `aria-current`. Nunca use um `badge` como opção ativa ao lado de botões do mesmo grupo. |
| `message` (+ `info`, `success`, `warning`, `error`), `components/error-summary.html`, `components/form-errors.html`, `confirmation` | [Retorno ao usuário](#retorno-ao-usuário). `alert*` permanece no CSS apenas como alias para temas antigos. |
| `badge` (+ `badge-brand`, `badge-success`, `badge-warning`, `badge-danger`, `badge-neutral`, `badge-dot`), `counter`, `uuid`, `meta-list` | Estados, marcadores, contagens e metadados, seguindo o [vocabulário visual](#vocabulário-visual): nunca escritos à mão nos templates. |
| `form-stack`, `form-field`, `helptext`, `errorlist`, `form-actions` | Formulários simples; `components/form-fields.html` substitui `form.as_p` preservando IDs, ajuda, erros e `aria-describedby`. Campos obrigatórios (Django `required`, `marked_required` ou `data-required`) levam `*`, opcionais levam "(opcional)" (exceto checkboxes), e o topo exibe uma vez "Campos com * são obrigatórios.". `hide_marks=True` desliga as marcas (login, LDAP, token do setup). O renderer `skeleton_crud/field.html` segue a mesma regra, omitindo "(opcional)" em campos com caixa "alterar este campo". `form-field.html` renderiza `field.help_text` sob autoescape (texto simples sempre escapado; `SafeString` do Django, como ajuda de senha, continua formatado), e `tests/unit/test_template_safe_filter.py` recusa `|safe` em componentes e fora das exceções. |
| `required`, `optional` | Marcas de rótulo (`*` via `show_required` e "(opcional)" via `show_optional`), passadas ao `include` de `form-fields.html`. |
| `setup-steps`, `setup-step-number` | Etapas numeradas do setup inicial (vertical no desktop, rolável no celular); `setup-step-done` e `setup-step-later` indicam o progresso. |
| `flow-steps`, `flow-step-number`, `flow-step-done` | Etapas de fluxos em várias telas: `{% include "skeleton/web/components/flow-steps.html" with steps=… %}` (`[{number, label, current, done}]`), com `aria-current="step"` na etapa atual e ícone de concluído nas anteriores. Use apenas onde houver sequência real; telas irmãs usam `link-tabs`, sem numeração. |
| `setup-files`, `data-files-section` | Etapa de anexos do setup ([#729](https://github.com/alaorneto/skeleton/issues/729)): envolve os rádios `files_backend` e as seções `data-files-section="local"` e `"s3"`; o CSS (`:has(input:checked)`) mostra apenas a seção selecionada sem script. Sem suporte a `:has`, ambas aparecem e o servidor valida normalmente. |

### Vocabulário visual

Cada elemento visual tem um único significado consistente em toda a interface ([#767](https://github.com/alaorneto/skeleton/issues/767)): a pessoa distingue imediatamente estado, marcador, identificador técnico e valor comum, e as cores mantêm sempre o mesmo sentido.

| Forma | Significado | Como se escreve | Exemplos |
|---|---|---|---|
| Pílula redonda **com ponto** | **Estado**: muda ao longo do tempo (ciclo de vida ou resultado) | somente `{{ codigo|status_badge }}` | Ativa, Concluído, Falhou, Pendente, Publicada |
| Etiqueta retangular **sem ponto** | **Marcador**: propriedade fixa que altera comportamento ou acesso | somente `{{ "codigo"|marker }}` | Obrigatório, Somente leitura, Segredo, Administrador da plataforma, Padrão, Travada |
| Número em pílula cheia (`counter`) | **Contagem** | `<span class="counter">` (no cabeçalho, `activity-count`) | não lidas, tarefas abertas |
| Ponto cheio sem número (`attention-dot`) | **Atenção**: há novidade sem exibir quantidade | `<span class="attention-dot" aria-hidden="true"></span>` com motivo em `sr-only` no mesmo link | avisos da plataforma não lidos, atualização disponível |
| `code` na segunda linha (`cell-secondary`) | **Identificador técnico** | `<code class="uuid">` | UUID, código, chave, variável de ambiente, slug, código HTTP |
| Texto | **Valor ou categoria** | texto simples; códigos traduzidos via `{{ codigo|code_label:"dominio" }}` | origem, tipo, fila, versão ("v0.6.0"), Sim/Não de campo comum |

Significado único dos tons: `success` para ativo ou concluído; `brand` para em andamento; `warning` para atenção ou ação pendente; `danger` para falha, bloqueio ou ação irreversível; `neutral` para inativo, encerrado ou rascunho. Falhas são sempre `danger`. Booleanos que representam estado (como `is_active`) usam `status_badge`; booleanos comuns aparecem como "Sim"/"Não" em texto.

- `status_badge` consulta `STATUS`, `marker` consulta `MARKERS` e `code_label` consulta `CODE_LABELS` (`table_origin`, `table_scope`, `audit_mode`, `app_origin`, `setting_origin`) em `skeleton/web/templatetags/skeleton_format.py`. Códigos desconhecidos renderizam em neutro (ou texto) com o próprio valor, sem quebrar a página. Para rótulos dinâmicos, passe o texto como argumento (`{{ "managed"|marker:rotulo }}`, montado com `{% blocktranslate asvar rotulo %}`).
- Novos estados, marcadores ou códigos devem entrar nesses mapas, nunca como `<span class="badge …">` manual: `tests/unit/test_ui_vocabulary.py` bloqueia `badge` escrito à mão (única exceção: `allauth/elements/badge.html`, que traduz tags do allauth) e valida os tons.
- Códigos crus (`succeeded`, `bundled`, `third_party`) nunca aparecem diretamente na interface: passe-os por `status_badge` ou `code_label`.

#### Estados e marcadores de apps

Apps que possuem estados ou marcadores próprios não devem reutilizar códigos do núcleo apenas pela cor nem escrever `badge` manualmente. Em vez disso, registram seus mapas em `AppConfig.ready()` ([#833](https://github.com/alaorneto/skeleton/issues/833)) via `skeleton.web.vocabulary` ([superfície pública](service-api.md#superfícies-públicas-do-núcleo-para-apps)) e usam os mesmos filtros informando o domínio:

```python
from django.utils.translation import gettext_lazy as _

from skeleton.web.vocabulary import register_marker, register_status

register_status(
    "med_scheduling.appointment",
    {
        "scheduled": (_("Agendada"), "neutral"),
        "arrived": (_("Paciente chegou"), "warning"),
        "in_progress": (_("Em atendimento"), "brand"),
        "done": (_("Atendida"), "success"),
        "no_show": (_("Falta"), "danger"),
    },
)
register_marker("med_scheduling.appointment", {"fit_in": (_("Encaixe"), "brand")})
```

```django
{% load skeleton_format %}
{{ appointment.state|status_badge:"med_scheduling.appointment" }} {{ "fit_in"|marker:"med_scheduling.appointment" }}
```

- O domínio tem a forma `<app_label>.<nome>`, com o rótulo de uma app instalada e `nome` em `[a-z0-9_]{1,40}`; domínios do núcleo (`flows`, `invoice`…) não contêm ponto e nunca colidem com os das apps.
- Cada entrada mapeia código para `(rótulo, tom)`. Estados aceitam `success`, `brand`, `warning`, `danger` e `neutral`; marcadores não aceitam `success`. Tom inválido, domínio sem prefixo, mapa vazio ou redefinição conflitante levantam `ImproperlyConfigured` no `ready()` (redeclarar exatamente o mesmo mapa é idempotente).
- O domínio de uma app é isolado: códigos não declarados saem em neutro com o valor cru, sem herdar tons do mapa do núcleo.

### Ações pela intenção

A cor de um botão expressa sua intenção, nunca sua posição na tela ([#769](https://github.com/alaorneto/skeleton/issues/769)):

| Classe | Quando |
|---|---|
| `btn-primary` | Ação principal da região: no máximo uma por cabeçalho ou formulário. |
| `btn-secondary` | Ação neutra, navegação ou operação reversível ("Editar", "Voltar ao padrão", "Reenviar"). |
| `btn-danger-outline` | **Entrada** de uma ação destrutiva: ação em linha de tabela, link para confirmação ou botão em zona de perigo ("Sair da organização", "Remover", "Revogar", "Desativar", "Anular"). |
| `btn-danger` | Exclusivamente o **envio final** de uma ação destrutiva na página de confirmação. |
| `btn-ghost` | Ação terciária (como "Limpar" filtros). |

- **Barra de ações do cabeçalho** (`components/toolbar.html`): cada item define `url`, `label`, `kind` (`primary`, `secondary` ou `danger`) e `icon` (nome já resolvido). No detalhe de registro, "Editar" é `primary`, "Gerenciar acesso" é `secondary` e "Remover" é `danger`.
- **Zona de perigo** (`components/danger-zone.html`, classes `danger-zone`, `danger-zone-title`, `danger-zone-text`): isola ações destrutivas do restante do formulário, explicando a consequência em uma frase e oferecendo o botão `btn-danger-outline` que leva à confirmação: `{% url 'web:leave-organization' organization_id as leave_url %}{% include "skeleton/web/components/danger-zone.html" with heading_id="dashboard-membership" title=_("Seu vínculo") text=_("…") url=leave_url label=_("Sair da organização") %}`. Utilizada em "Seu vínculo", remoção de webhook, desativação de conta de serviço e exclusão de calendário.
- **Confirmação:** ações irreversíveis ou que afetam outras pessoas nunca executam em um clique (aprovar ou solicitar eliminação de identidade, remover webhook, rotacionar segredo, desativar conta de serviço ou cliente da API, revogar papel de plataforma, cancelar processo ou assinatura, excluir calendário, anular fatura, dar baixa manual com motivo, reiniciar instância ou nós). O botão na tela é um link `GET` para a própria URL da ação, que renderiza a página de confirmação (alvo, consequência, campo de motivo quando aplicável, botão `btn-danger` e "Cancelar"); só o botão vermelho dispara o `POST` com CSRF e revisão. Em `skeleton/web/confirm.py`, `@confirmation_step(descrever)` vai logo abaixo dos decoradores de autorização e acima de `require_POST`: `descrever(request, …)` localiza o alvo pela mesma busca autorizada da view (respondendo 404 neutro para inexistente ou sem acesso) e devolve um `Confirmation` (ou `None` se não houver confirmação). Os templates são `skeleton/web/confirm.html` (organização e conta) e `skeleton/web/platform/confirm.html` (plataforma), ambos apoiados em `components/confirm-card.html`. Apps podem reutilizar `skeleton.web.confirm` ([superfície pública](service-api.md#superfícies-públicas-do-núcleo-para-apps)).
- Ações reversíveis de uma linha (revogar token, remover anexo, recusar solicitação, descartar job) usam `btn-danger-outline`, sem tela extra de confirmação.
- `tests/unit/test_ui_vocabulary.py` recusa botões `btn-primary`, `btn-secondary` ou `btn-ghost` iniciados por verbos destrutivos (Remover, Revogar, Excluir, Desativar, Sair da organização, Cancelar a assinatura, Cancelar processo, Cancelar título, Descartar, Anular, Eliminar, Estornar, Rotacionar segredo, Redefinir MFA, Destravar).

### Retorno ao usuário

Avisos, erros e estados vazios seguem componentes padronizados em todas as telas ([#776](https://github.com/alaorneto/skeleton/issues/776)):

- **Aviso:** `<div class="message <tom>" role="status">` com o ícone do tom e texto em `<div class="min-w-0">`. Tons e ícones fixos: `info` (`info`, padrão; usado também na faixa de atualização disponível), `success` (`check-circle`), `warning` (`warning`) e `error` (`error`). Mensagens do Django (`messages`) são renderizadas por `components/messages.html` (níveis desconhecidos caem em `info`). Nunca use `alert` nem exponha códigos crus (`agent_unavailable`): erros do agente são traduzidos para frases legíveis, com códigos sem frase exibidos como "Código para o suporte".
- **Erros:** erros de formulário usam `components/form-errors.html` (com link para cada campo); views que repassam `ValidationError` de serviço ao formulário usam `skeleton.web.forms.form_error` (ou `validation_messages` para listas), que tratam tanto erros em dicionário quanto em lista sem falhar com `AttributeError`. Erros fora de campo usam `{% include "skeleton/web/components/error-summary.html" with message=error title=_("Não foi possível revogar") %}`, aceitando `errors` (lista), `heading="h1"` (páginas de erro), `back_url`/`back_label` e `show` (quando há apenas título).
- **Lista vazia:** tabelas usam `components/empty-row.html`; cartões e listas usam `{% include "skeleton/web/components/empty-state.html" with message=_("Nenhum aplicativo instalado.") icon="apps"|concept_icon %}` (com `hint`, `action_url` e `action_label` opcionais), sempre com o ícone do conceito listado.
- `tests/unit/test_ui_vocabulary.py` bloqueia `alert`, `error-summary` e `empty-state` escritos manualmente fora dos componentes (única exceção: o estado vazio da página inicial, com seus links).

### Tabelas

Toda listagem tabular usa `<div class="table-scroll table-stack"><table>…</table></div>` ([#469](https://github.com/alaorneto/skeleton/issues/469), [#777](https://github.com/alaorneto/skeleton/issues/777)), evitando rolagem horizontal da página. Abaixo de 640 px, `table-stack` converte cada linha em cartão e exibe o rótulo de cada célula via `data-label` (idêntico ao cabeçalho: `<td data-label="{% translate "Nome" %}">`; a coluna de ações leva `data-label=""`).

Tabelas densas que exigem comparação linha a linha (auditoria, jobs, plano de atualização, relatórios contábeis) ou com colunas condicionais mantêm apenas rolagem horizontal dentro do cartão: elas são listadas com justificativa em `SCROLL_ONLY` de `tests/unit/test_table_stack.py`, que recusa qualquer outra tabela sem `table-stack` ou célula sem `data-label`. Cabeçalhos de linha (`th scope="row"`) quebram normalmente, campos dentro de células respeitam largura mínima e células ficam centralizadas verticalmente sem preservar espaços do template nem quebrar palavras letra a letra.

| Classe | Onde | Uso |
|---|---|---|
| `col-num` | `th` e `td` | Números alinhados à direita, sem quebra. |
| `col-date` | `th` e `td` | Datas (com filtro `moment`), sem quebra. |
| `col-actions` | `th` e `td` | Última coluna (ações), largura mínima, alinhada à direita. |
| `col-text` | `td` | Texto multilinha (até 28rem, preservando quebras). |
| `cell-primary` | link ou texto | Nome principal do item, na primeira coluna. |
| `cell-secondary` | `span` | Segunda linha da célula (identificador técnico ou detalhe). |
| `cell-truncate` | `span` | Texto truncado com reticências em uma linha. |
| `row-actions` | `div` | Grupo de botões na coluna de ações. |

- **Estados:** `{% load skeleton_format %}{{ estado|status_badge }}` renderiza o código traduzido e colorido. Passe o domínio (`"flows"`, `"invoice"`, `"jobs"`…) quando houver ambiguidade entre subsistemas, o domínio registrado por uma app ([estados e marcadores de apps](#estados-e-marcadores-de-apps)) ou um rótulo explícito (`{{ job.state|status_badge:job.get_state_display }}`). Códigos desconhecidos saem em neutro com o próprio texto. Todo `TextChoices` `*Status`/`*State` do núcleo é coberto pelo mapa (`tests/unit/test_format_filters.py`).
- **Nomes de pessoas:** a view reúne os identificadores da página e chama `skeleton.web.people.people_labels(ids, actor=request.user)` **uma única vez** (via `IdentityService.people`, restrito a membros ativos da organização atual). O template renderiza `{{ row.actor_sysid|person:people }}`, mostrando o nome, "Sistema" para vazio ou "Pessoa indisponível" com os 8 primeiros dígitos do UUID para não membros ativos — evitando vazar nomes de outras organizações. A auditoria global (`/platform/audit/`) não resolve nomes ([#475](https://github.com/alaorneto/skeleton/issues/475)) e exibe apenas "Usuário" ou "Sistema". Nas duas telas de auditoria, o botão "Detalhes" expande logo abaixo da linha um painel de largura total com identificador completo do ator, requisição, origem e tabela Campo/Antes/Depois (Flowbite collapse, aberto por padrão sem script; [#834](https://github.com/alaorneto/skeleton/issues/834)).
- **Lista vazia e paginação:** use `{% include "skeleton/web/components/empty-row.html" with colspan=4 message=_("Nenhum token.") hint=_("Crie um token para integrar.") icon="api_tokens"|concept_icon %}`. Listas vazias não exibem paginação, e resultados de uma única página mostram apenas o resumo sem "Anterior"/"Próxima" (`skeleton/web/pagination.html`).
- **Tabela genérica de registros (`components/record-table.html`):** `list_context` entrega cada célula como `{"text", "kind"}` e cada cabeçalho como `{"label", "kind"}` (`kind` em `text`, `num`, `date` ou `bool`), truncando na lista textos com mais de 160 caracteres (o detalhe exibe tudo). Templates derivados antigos que usavam `{{ cell }}` e `{{ heading }}` devem migrar para `{{ cell.text }}` e `{{ heading.label }}`.

Regras gerais para tabelas:

1. A primeira coluna é o nome do item como link (`cell-primary`).
2. Identificadores técnicos (UUID, código) vão na segunda linha (`cell-secondary`), nunca em coluna própria, salvo se forem o único identificador.
3. Números alinham-se à direita (`col-num`); datas usam `col-date` com o filtro `moment`.
4. Estados usam sempre `status_badge`, nunca código cru.
5. Ações ficam na última coluna (`col-actions`), dentro de `<div class="row-actions">`, com `btn btn-secondary btn-sm` e ações destrutivas em `btn btn-danger-outline btn-sm`; o cabeçalho usa `<span class="sr-only">Ações</span>`.
6. Listas vazias usam `empty-row.html`.
7. Nunca use `<br>` para empilhar linhas na célula: use `cell-secondary`.

### Páginas de erro

Toda recusa renderiza uma página em português com a marca, uma explicação clara e um caminho de retorno ([#473](https://github.com/alaorneto/skeleton/issues/473)):

- **Erros do Django:** `400.html`, `403.html`, `403_csrf.html` ("A página expirou", com "Entrar novamente"), `404.html` e `500.html` vivem na raiz de `skeleton/web/templates/` e estendem `skeleton/web/error_base.html` — layout autônomo que não estende `base.html`, não usa `{% url %}` nem variáveis de contexto, funcionando mesmo no erro 500 e no modo setup. A ação padrão leva a `/` ("Ir para o início").
- **Isolamento no 404 e 500:** `404.html` exibe o mesmo texto neutro para "não existe" e "sem acesso", sem ecoar o caminho solicitado, produzindo respostas idênticas byte a byte (`tests/integration/test_error_pages.py`). O erro 500 nunca mostra a exceção na página: o traceback vai apenas para logs e relato de erros. A página `403_csrf.html` não altera a verificação do CSRF (não há `CSRF_FAILURE_VIEW`).
- **Erros de operação:** use `skeleton.web.errors.error_response(request, detail, *, status, back_url=None, back_label=None)`, que renderiza `skeleton/web/error.html` com título adequado ao status (400 "Não foi possível concluir", 403 "Acesso não permitido", 404 "Página não encontrada", 409 "Os dados mudaram", 413 "Arquivo grande demais", 429 "Muitas tentativas"), detalhe e botão "Voltar" ("Voltar e recarregar" no 409) apontando para `back_url` ou para `Referer` validado com `url_has_allowed_host_and_scheme` contra o host da requisição, além de "Ir para o início". Nunca chame `render` diretamente com `error.html`.
- Erros de validação vinculados a uma tela de origem (como envio de anexo) voltam à tela original via `messages.error`, em vez de abrir uma página de erro separada.

### Datas e horas

Toda data e hora exibida na interface segue o formato `15/11/2026 14:30` (data: `15/11/2026`; hora: `14:30`), ajustado ao fuso efetivo da pessoa, da organização ou da instância ([#468](https://github.com/alaorneto/skeleton/issues/468)). O formato é centralizado em `FORMAT_MODULE_PATH = ["skeleton.conf.formats"]` (`skeleton/conf/formats/pt_BR/`), atendendo também `presentation._value` nas telas genéricas.

- Use `{% load skeleton_format %}` com `{{ valor|moment }}`, `{{ valor|moment:"date" }}` ou `{{ valor|moment:"time" }}`. O filtro emite `<time datetime="ISO">texto</time>`, retorna "—" para vazio e escapa textos que não sejam data.
- Nunca imprima datas cruas (`{{ x.created_at }}`) nem use `|default:"—"` em datas: `tests/unit/test_template_dates.py` bloqueia (exceção: corpos de e-mail).
- Os valores "Antes" e "Depois" da auditoria representam o dado bruto gravado e não passam pelo filtro.

### Ícones

Os ícones seguem o estilo Heroicons (traço de 24 px, MIT) e são puramente decorativos (`aria-hidden`), acompanhados de texto visível ou `aria-label`. A regra central é **um conceito, um ícone**: cada conceito usa o mesmo ícone em toda a aplicação, e nenhum ícone representa dois conceitos distintos ([#768](https://github.com/alaorneto/skeleton/issues/768)).

- O mapeamento fica em `skeleton/common/icons.py` (reexportado por `skeleton/web/icons.py`, que fornece o filtro `concept_icon`), separado em dois conjuntos disjuntos: `CONCEPT_ICONS` (conceito → ícone) e `ACTION_ICONS` (ações, retorno e controles: `plus`, `pencil`, `trash`, `restore`, `refresh`, `archive`, `copy`, `eye`, `arrow-up-tray`, `external`, `search`, `filter`, `login`, `logout`, setas e chevrons, `menu`, `close`, `check`, `sun`/`moon`/`computer`, `info`/`check-circle`/`warning`/`error`, `clock` e `logo`).
- Templates referenciam sempre o conceito, nunca o nome do ícone: `{% include "skeleton/web/components/icon.html" with name="billing"|concept_icon class="size-5" %}` e `{% include "skeleton/web/components/empty-row.html" with … icon="webhooks"|concept_icon %}`. Apenas ícones de ação são citados diretamente pelo nome. `SettingsItem.icon` também guarda o conceito.
- **Ícones de apps:** cada entrada `navigation` usa o conceito declarado em `icon` ou o SVG de traço em `icon_svg` ([extensão](extending.md)), com fallback para `apps`. Ícones próprios passam por `skeleton/common/stroke_icons.py`, têm a geometria reconstruída e entram em `components/icon.html` via `app_icon_markup` dentro do mesmo `<svg>` do núcleo. Um SVG idêntico a um ícone do núcleo ou de outra app gera `skeleton.E051` e cai para `apps`. Os cartões de aplicativos na página inicial reutilizam o mesmo ícone.
- `tests/unit/test_concept_icons.py` garante bijeção entre conceitos e ícones, existência em `components/icon.html` e ausência de nomes diretos de ícones de conceito em templates.

| Conceito | Ícone | Conceito | Ícone |
|---|---|---|---|
| `home` (Início) | `home` | `overview` (Visão geral) | `grid` |
| `organization` | `building` | `configuration` (configuração da organização, de uma app) | `cog` |
| `instance_configuration` | `settings` | `platform` | `server` |
| `account` (Minha conta, Perfil) | `user` | `notifications` | `bell` |
| `tasks` | `clipboard-check` | `people` (Pessoas, Usuários) | `users` |
| `groups` | `user-group` | `roles` (Papéis e permissões, acesso do registro) | `key` |
| `external_people` | `user-plus` | `security` (Segurança, MFA) | `shield` |
| `password` | `lock` | `email` | `envelope` |
| `api_tokens` | `ticket` | `connected_devices` | `signal` |
| `identity` (Identidade, provedores) | `finger-print` | `privacy` (eliminação) | `user-minus` |
| `join_requests` | `inbox` | `join_links` | `link` |
| `admission` | `badge-check` | `email_domains` (domínios verificados) | `at-symbol` |
| `site_domains` (domínios próprios) | `globe` | `public_page` (páginas em blocos) | `window` |
| `apps` | `cube` | `webhooks` | `bolt` |
| `service_accounts` | `cpu-chip` | `flows` | `share` |
| `calendar` | `calendar` | `documentation` | `book-open` |
| `tables` | `table` | `audit` (auditoria, histórico) | `document-search` |
| `files` (anexos) | `document` | `transfers` (exportação e importação) | `arrow-down-tray` |
| `billing` | `credit-card` | `appearance` | `paint-brush` |
| `jobs` | `queue-list` | `updates` | `arrow-up-circle` |
| `native_clients` | `device-phone` | `api_clients` | `command-line` |
| `remote_instances` | `server-stack` | `bundles` (pacotes de apps) | `rectangle-stack` |
| `storage` | `database` | `insights` (indicadores) | `chart-bar` |

Todo conceito novo precisa de um ícone exclusivo desenhado em `components/icon.html`; reutilizar um ícone já atribuído a outro conceito reprova no teste.

Elementos HTML sem classe (`h1`–`h3`, links, listas, `code`, `pre`, inputs, selects e `button`) recebem estilização de base consistente, mantendo legíveis telas de apps que ainda usam marcação simples.

### Linha do tempo do registro

A página de detalhe exibe uma **linha do tempo** unificada com eventos de auditoria e anexos do registro, do mais recente ao mais antigo, limitada a 50 itens ([#185](https://github.com/alaorneto/skeleton/issues/185)):

- **Auditoria:** consultada via `AuditService.history`, exigindo administração da organização ou `TablePermission audit` somada à leitura do registro; exibe apenas os nomes das colunas alteradas.
- **Anexos:** respeitam a autorização do registro alvo.

Novas fontes podem ser conectadas via `skeleton.web.timeline.register_timeline_source`, devolvendo apenas dados que o ator já teria permissão para ver isoladamente.

### Título e marca no endereço da organização

O título de toda página termina com `· {{ site_name }}` (`<tela> · {{ site_name }}`), nunca com o literal "Skeleton" ([#484](https://github.com/alaorneto/skeleton/issues/484)). `site_name` é fornecido por `organization_shell`: corresponde ao nome público da organização no host `<slug>.<domínio base>` e, nos demais hosts, ao nome do produto da instância ou "Skeleton" ([#598](https://github.com/alaorneto/skeleton/issues/598)).

No host da organização, a marca também aparece nas telas anônimas (login, cadastro, senha, MFA, "sem vínculo"): sem contexto de tenant, `organization_brand` lê o índice `OrganizationSite` (nome, paleta e `/site/logo/`), sem consultar `OrganizationSettings`; dentro do tenant, lê as configurações normalmente, mas usando a URL pública `/site/logo/?v=…`. O layout de entrada (`account/base_entrance.html`) exibe logo ou nome no cartão, o rodapé traz apenas o nome e `<meta name="theme-color">` recebe a cor da marca. Não há templates próprios de 404/500 por organização: respostas de erro do Django seguem sem marca específica da organização.

### Favicon, ícone da organização e manifesto

O favicon padrão do núcleo (`frontend/src/favicon.svg`, na cor padrão da marca) é copiado pelo build (`frontend/scripts/build.mjs`) para `skeleton/web/static/skeleton/web/favicon.svg`, e `npm run check` falha se houver divergência — nunca edite arquivos manualmente em `static/`. `base.html` (incluindo entrada e plataforma) e o layout do setup inserem `<link rel="icon">`.

Em `/o/<org>/settings/`, o administrador pode enviar um ícone PNG quadrado de exatamente 512 × 512 pixels e até 256 KB, validado pelo conteúdo (assinatura PNG e cabeçalho `IHDR`, sem biblioteca de imagem; [#485](https://github.com/alaorneto/skeleton/issues/485)). `organization_icon_url`, exposto por `organization_shell`, aponta para `/site/icon/?v=…` no host da organização e para `/o/<org>/settings/icon/?v=…` (somente membros) no domínio principal; se ausente, vale o favicon padrão. Alterar o ícone atualiza `?v=`. No host da organização, é incluído ainda `<link rel="manifest">` apontando para `/site/manifest.webmanifest` (nome, nome curto de até 12 caracteres, cor e ícone públicos).

### Texto nos templates

Todo texto visível em templates deve passar pelo gettext ([#172](https://github.com/alaorneto/skeleton/issues/172)), tanto no conteúdo entre tags (`{% translate "Salvar" %}`) quanto em atributos visíveis (`alt`, `title`, `placeholder`, `aria-label`). Em argumentos de tags, use `_("…")` (por exemplo, `{% include "…" with label=_("Abrir") %}`). As msgids são escritas em pt-BR e os templates carregam `{% load i18n %}`. Para frases com variáveis, prefira `{% blocktranslate trimmed %}` com nomes simples e cláusula `with` para atributos. Dados dinâmicos (nomes, e-mails, valores de negócio) nunca entram no gettext.

### Regras que não mudam

- Sem scripts inline nem CDN: o JavaScript do Flowbite é servido localmente de `static/skeleton/web/vendor/flowbite-4.0.2/`.
- Degradação graciosa sem JavaScript: menus móveis e menu da conta abrem via `@media (scripting: none)` (variante `noscript:`), e abas de registros voltam a seções empilhadas.
- Abas mantêm semântica ARIA e navegação por teclado (`ArrowLeft`, `ArrowRight`, `Home`, `End`) em `site.js`.
- **Proteção contra envio duplo:** `site.js` bloqueia um segundo envio do mesmo formulário POST até a navegação mudar, aplicando `aria-disabled` aos botões (nunca `disabled`, que omitiria `name`/`value` do botão acionado) e reabilitando ao voltar pelo histórico (`pageshow`). Formulários que devolvem download na mesma página usam `data-allow-resubmit`.
- **Estado de processamento:** o `<button>` que submeteu o formulário recebe `aria-busy="true"`, trocando seu ícone por um anel giratório via CSS; `data-busy-label` troca também o texto (por exemplo, "Gerando o plano…"), recomendado em operações de vários segundos como etapas de atualização. Páginas de espera auto-recarregadas usam `<span class="spinner" aria-hidden="true"></span>` no lugar do ícone de `message info`. Com `prefers-reduced-motion: reduce`, a rotação para, mantendo o feedback textual.
- **Pílula de atividade (`activity-pill`):** reúne Notificações e Tarefas no cabeçalho em um grupo rotulado "Atividade" com dois links — ícones nas pontas e contadores centralizados (`activity-count`, largura fixa para `"9+"`), sempre visíveis (`0` em cinza via `activity-count-empty`, valor na cor da marca e `"9+"` acima de 9, `COUNTER_CAP`). Nome e quantidade de cada link vão em `sr-only`, enquanto o número visível leva `aria-hidden`.
- Até 600 px o renderer usa coluna única sem rolagem horizontal; acima disso aplica a grade de 12 colunas (`field-span-1..12`), gerada fora do scanner do Tailwind porque vem do manifesto.

## Páginas em blocos

As páginas configuráveis pelo administrador — página inicial pública da plataforma, página pública da organização e início interno da organização (epic [#618](https://github.com/alaorneto/skeleton/issues/618)) — são documentos JSON versionados compostos por **blocos estruturados de texto simples** ([#619](https://github.com/alaorneto/skeleton/issues/619)). Não há HTML, Markdown, upload de imagens nem estilos por bloco: todo conteúdo sai com autoescape.

```json
{"version": 1, "blocks": [
  {"type": "hero", "title": "Empresa Exemplo", "subtitle": "Atendimento de segunda a sexta", "login_button": true},
  {"type": "text", "heading": "Quem somos", "body": "Parágrafo um.\n\nParágrafo dois."},
  {"type": "links", "heading": "Links", "items": [{"label": "Blog", "url": "https://exemplo.test/blog"}]}
]}
```

`skeleton.common.page_blocks.validate_page(document, *, kind, known_widgets=frozenset())` é a única função de validação: devolve o documento normalizado (textos com `strip` e padrões preenchidos) ou levanta `ValidationError` indexado por bloco e campo (`blocks.<i>.<campo>` e `blocks.<i>.items.<j>.<campo>`, base 0). O módulo é Python puro (regra R1) e exporta `PAGE_KINDS`, `BLOCK_TYPES`, `FIELDS` (lido pelo editor), `EMPTY_PAGE` e `PageState(document, published, revision)`. Com `allow_empty=True`, aceita documentos vazios de páginas ainda não publicadas.

| Tipo | Campos (com `strip`; limites em caracteres) | Vale em |
|---|---|---|
| `hero` | `title` 1–120 obrigatório; `subtitle` 0–300; `login_button` (padrão `true`, leva ao login) | `platform_home`, `organization_public` |
| `text` | `heading` 0–120; `body` 1–4000 (parágrafos separados por linha em branco) | todos |
| `contact` | `email` 0–254 (e-mail válido); `phone` 0–40 (somente números, espaço e `+ ( ) - .`); `address` 0–300; `hours` 0–300; ao menos um preenchido | todos |
| `links` | `heading` 0–120; `items` de 1 a 10 `{label 1–60, url}`; `url` é `https://…` (até 500) ou caminho local iniciado por `/`, sem `//`, `\`, espaços ou caracteres de controle | todos |
| `apps`, `tasks`, `notifications` | `heading` 0–120 | `organization_home` |
| `widget` | `name`, um [quadro registrado por uma app](extending.md#quadros-do-painel-da-organização) | `organization_home` |

Restrições gerais: `version` igual a `1`; entre 1 e 20 blocos; até 32 768 bytes em UTF-8 serializado; chaves ou tipos desconhecidos e tipos incompatíveis com a página são recusados; caracteres de controle são proibidos (exceto quebra de linha em `body`, `address` e `hours`).

- **Renderização:** `skeleton/web/templates/skeleton/web/pages/blocks.html` inclui `block-<tipo>.html` para cada bloco. Todo texto passa por autoescape (`body|linebreaks`); links externos recebem `rel="noopener noreferrer"`; nenhum template de página contém `style`, `script`, `|safe` ou atributos de evento (`tests/unit/test_page_templates.py`). O botão do `hero` aponta para `account_login`. Visitantes não autenticados veem `pages/public.html`, com a base visual da entrada e maior largura de conteúdo.
- **Editor:** `skeleton/web/page_editor.py` implementa o editor SSR compartilhado, sem JavaScript. Cada página instancia `PageEditor` com seu *store* (`load() -> PageState` e `save(document=, published=, expected_revision=)`), decoradores de autorização, template base e pré-visualização, expondo rotas via `editor_urls(prefixo, nome, view)`: lista de blocos, `preview/`, `publish/`, `blocks/new/?type=…`, `blocks/<i>/`, `blocks/<i>/move/` e `blocks/<i>/delete/` (confirmação em GET, exclusão em POST). Toda mutação exige POST com CSRF e revisão atual: revisão antiga responde 409 ("Esta página foi alterada por outra pessoa. Recarregue para ver a versão atual.") sem gravar; erro de validação retorna 400 com `error-summary` e erros nos campos; campos POST desconhecidos retornam 400. Itens de links são editados em um `textarea`, um por linha no formato `Rótulo | endereço`. A pré-visualização usa exatamente o mesmo template da página publicada.

### Início da organização

O painel inicial da organização (`/o/<org>/`, rota `web:dashboard`; [#624](https://github.com/alaorneto/skeleton/issues/624)) é configurável pelo administrador em **Configuração › Organização › Página inicial** (`/o/<org>/admin/pages/home/`), usando o mesmo editor de blocos. Sem página publicada, vale o padrão `DEFAULT_ORGANIZATION_HOME`: **Aplicativos**, **Minhas tarefas** e **Notificações**. O bloco **Seu vínculo** (sair da organização) permanece sempre no rodapé. A configuração é única por organização, mas cada membro vê apenas os dados que tem permissão para acessar.

| Bloco | O que mostra |
|---|---|
| `apps` | Cartões das entradas `navigation` dos apps ativos acessíveis ao papel da pessoa (`application_navigation`). |
| `tasks` | Até 5 tarefas abertas atribuídas à pessoa nesta organização (`skeleton/flows/inbox.py`), com link para `flows:tasks`. |
| `notifications` | Até 5 notificações não lidas da pessoa nesta organização (`skeleton/notifications/inbox.py`), com link para `notifications:organization`. |
| `widget` | Um [quadro de app](extending.md#quadros-do-painel-da-organização), exibido apenas se `visible_widgets` o autorizar; some se a app for desligada ou a porta falhar, e degrada para quadro neutro em caso de erro. |
| `text`, `contact`, `links` | Os mesmos blocos das páginas públicas. |

No editor, o seletor de `widget` lista os quadros visíveis ao administrador, mas a validação aceita qualquer nome registrado para que desativar uma app não invalide a página. A pré-visualização (`…/home/preview/`) renderiza o próprio painel com o rascunho. No layout padrão, o início de um membro executa um número fixo de consultas (`tests/integration/test_organization_home_page.py`).

## Texto formatado (Markdown)

Campos de texto longo podem habilitar **texto formatado**, armazenado em um dialeto pequeno e fechado de Markdown (epic [#827](https://github.com/alaorneto/skeleton/issues/827)). O recurso é opt-in por campo: comentários, páginas em blocos e textos de aviso permanecem em texto simples. A API, a exportação CSV, a busca global e a auditoria tratam o valor como texto puro (o próprio Markdown); apenas telas de leitura o renderizam formatado, e a edição usa o [editor](#editor-markdown).

### O dialeto

`skeleton/common/markdown.py` ([#824](https://github.com/alaorneto/skeleton/issues/824)) define o dialeto e o renderiza no servidor com `markdown-it-py`, par Python do `markdown-it` usado no navegador. Os dois lados compartilham as mesmas regras, comprovadas contra o corpus `tests/fixtures/markdown/corpus.json` (`tests/unit/test_markdown.py` e `frontend/tests`).

| Recurso | Sintaxe | HTML |
|---|---|---|
| `emphasis` | `**negrito**`, `*itálico*` (também `__` e `_`) | `<strong>`, `<em>` |
| `underline` | `++sublinhado++` | `<u>` |
| `strike` | `~~tachado~~` | `<s>` |
| `code` | `` `código` `` | `<code>` |
| `heading` | `#`, `##`, `###` (níveis maiores viram o 3) | `<h1>` a `<h3>` |
| `lists` | `- item`, `1. item` | `<ul>`, `<ol>` |
| `quote` | `> citação` | `<blockquote>` |
| `link` | `[rótulo](https://…)` ou `[rótulo](/caminho)` | `<a rel="nofollow noopener noreferrer">` |

Parágrafos (linha em branco), quebras de linha simples (`<br>`), escape com `\` e entidades funcionam sempre. Não são suportados HTML bruto, imagens, tabelas, blocos de código (indentados ou cercados), títulos sublinhados (`===`), linhas horizontais, autolinks nem referências de link: construções desse tipo permanecem como texto comum. Um campo pode habilitar apenas um subconjunto de recursos; a sintaxe de recursos desabilitados também permanece como texto.

- **Segurança:** o servidor nunca recebe nem persiste HTML. O parser roda com HTML bruto desativado, escapando todo `<` e `&`. Links aceitam apenas `https://` (com host) ou caminhos locais (`/…`, nunca `//` nem `\`); qualquer outro esquema (`javascript:`, `data:`, `http:`, `mailto:`) é mantido como texto. O aninhamento é limitado e a entrada tem teto de `MAX_SOURCE_LENGTH` (100 000 caracteres), levantando `MarkdownTooLong` acima disso.
- **Uso em Python** (superfície pública para apps; [API de serviços](service-api.md#superfícies-públicas-do-núcleo-para-apps)):

```python
from skeleton.common.markdown import render_html, to_plain_text

render_html("**a** <b>")  # '<p><strong>a</strong> &lt;b&gt;</p>\n'
render_html("# t", features=["lists"])  # '<p># t</p>\n'
to_plain_text("- **a**\n- b")  # 'a\nb'
```

Nos templates, carregue `{% load skeleton_markdown %}`: `{{ texto|markdown }}` envolve o HTML em `<div class="markdown">` (estilizado em `site.css` para claro e escuro), `{{ texto|markdown:"emphasis,lists" }}` restringe os recursos (use a mesma lista configurada no campo) e `{{ texto|markdown_plain }}` devolve o texto sem marcadores para listas e resumos. Valores persistidos acima do limite saem como texto escapado. Nunca marque texto formatado como seguro (`|safe`, `mark_safe`) por fora destes filtros.

### Editor Markdown

O editor ([#825](https://github.com/alaorneto/skeleton/issues/825)) é fornecido pelo widget `MarkdownTextarea` (`skeleton/web/markdown_widget.py`, [superfície pública](service-api.md#superfícies-públicas-do-núcleo-para-apps)), pronto para uso em formulários do núcleo ou de apps:

```python
from django import forms
from skeleton.web.markdown_widget import MarkdownTextarea


class AnotacaoForm(forms.Form):
    texto = forms.CharField(widget=MarkdownTextarea(attrs={"rows": 8}))
    resumo = forms.CharField(required=False, widget=MarkdownTextarea(features=["emphasis", "lists"]))
```

O controle subjacente continua sendo um `<textarea>` padrão: sem JavaScript, a pessoa edita o Markdown diretamente e o formulário funciona normalmente. O widget renderiza também um `<template>` com a barra de ferramentas (restrita aos recursos do campo), as abas de modo e o diálogo de link já traduzidos pelo servidor, carregando o módulo `skeleton/web/static/skeleton/web/markdown_editor.js`, que oculta o textarea e monta o editor sobre ele:

- **Visual (padrão):** edição WYSIWYG sem marcadores visíveis (ProseMirror). Colar de processadores de texto preserva negrito, itálico, sublinhado, tachado, títulos, listas, citações e links `https://`, convertendo o restante em texto; texto colado sem HTML é interpretado como Markdown. Atalhos no início da linha (`- `, `1. `, `> ` e `#`) criam listas, citações e títulos.
- **Markdown:** edição com marcadores à vista e realce sintático (CodeMirror 6): em `**negrito**`, texto e asteriscos ficam em negrito com marcadores esmaecidos. `Enter` continua listas e citações, encerrando-as em uma linha só com o marcador.
- **Barra de opções (idêntica nos dois modos):** negrito (`Ctrl+B`), itálico (`Ctrl+I`), sublinhado (`Ctrl+U`), tachado (`Ctrl+Shift+X`), código (`Ctrl+E`), título (`Ctrl+Alt+2`, além de `1` e `3` para os demais níveis), lista com marcadores (`Ctrl+Shift+8`), lista numerada (`Ctrl+Shift+7`), citação (`Ctrl+Shift+9`) e link (`Ctrl+K`, via `<dialog>` que aceita apenas `https://` ou caminho local). No modo Markdown insere/remove marcadores; no visual aplica/remove a formatação, indicando estado ativo conforme a seleção.

O `<textarea>` mantém sempre o Markdown atualizado a cada edição (disparando `input`). Alternar de modo sem editar não modifica o valor; editar no modo visual normaliza o texto para a forma canônica do dialeto (`*itálico*`, `- item`) preservando a formatação. A preferência de modo é salva em `localStorage` (`skeleton.markdown-editor.mode`). Atributos `required`, `rows`, `aria-invalid` e `aria-describedby` são espelhados no editor; campos obrigatórios vazios recebem foco e validação do navegador (além da validação no servidor). Textareas `disabled` ou `readonly` não são aprimorados. A barra de opções (`toolbar`) e as abas (`tablist`) suportam navegação por setas, e a área de edição é um `textbox` associado ao rótulo do campo.

- **CSP:** o módulo é servido da mesma origem sem scripts inline, carregando o modo Markdown sob demanda via `import()` no mesmo diretório. O CSS do CodeMirror usa folhas adotadas (CSSOM) permitidas pela CSP (o build ajusta `style-mod` e falha se o trecho mudar numa atualização). O realce usa classes `tok-*` e o modo visual usa `.markdown`, ambos em `site.css` sobre os tokens do tema. Ao colar HTML, atributos `style` e `on*` são renomeados antes da leitura (`frontend/src/markdown-editor/paste.js`), evitando violações de CSP sem perder a formatação de Google Docs e Word.
- **Como acrescentar um recurso:** a lista é fechada e espelhada entre servidor e cliente. É necessário atualizar o dialeto (`skeleton/common/markdown.py` e `frontend/src/markdown-editor/dialect.js`), o corpus (`tests/fixtures/markdown/corpus.json`), o schema visual (`schema.js`), a conversão (`conversion.js`), o realce (`source-mode.js`), o comando (`commands.js`) e o botão (`TOOL_GROUPS` em `markdown_widget.py`, conferido contra `COMMANDS` por `tests/unit/test_markdown_widget.py`). Valide com `npm test` em `frontend/` e `tests/unit/test_markdown.py`.

### Campo formatado na interface declarativa

Na [interface declarativa](application-admin-ui-plan.md#5-formulário-e-detalhe-padronizados), um `TextField` ou `CharField` pode declarar `"widget": "markdown"` ([#826](https://github.com/alaorneto/skeleton/issues/826)), aceitando opcionalmente `markdown_features` (lista não vazia de recursos sem repetição; padrão: todos) e `rows`:

```json
{"name": "body", "label": "Texto", "widget": "markdown", "markdown_features": ["emphasis", "lists", "link"], "rows": 8}
```

O formulário utiliza `MarkdownTextarea` com esses recursos; a página de detalhe e campos somente leitura renderizam o HTML no servidor com a mesma lista; a listagem exibe texto plano via `to_plain_text`, truncado como os demais campos. O widget exige o piso `MARKDOWN_WIDGET_CORE_MINIMUM` (0.9.0) em `compatibility.skeleton`: `package build` aplica o piso e a validação recusa versões inferiores. Declarar `markdown_features` sem o widget, informar recursos desconhecidos ou repetidos, ou aplicar o widget a campos não textuais é recusado tanto na validação pura quanto em `skeleton.E013`.

O exemplo `notes` demonstra o uso no campo de texto da anotação ([#830](https://github.com/alaorneto/skeleton/issues/830)): `examples/notes/manifest.json` configura `body` com `"widget": "markdown"` (todos os recursos), exibe o campo na lista e exige núcleo `>=0.9.0`.

## Django admin

Não existe interface do Django admin: toda a administração da plataforma usa views explícitas em `/platform/`, compartilhando o mesmo shell, componentes e tokens do restante do site. O pacote `django.contrib.admin` permanece instalado apenas como dependência técnica (modelo `LogEntry` e migrations históricas que concedem privilégios em `django_admin_log`) via `SimpleAdminConfig`, sem autodiscover, site ou rotas; `/admin/…` retorna 404.

## Build do CSS

```text
frontend/
  package.json, package-lock.json   versões fixas (Tailwind 4.3.5, Flowbite 4.0.2, IBM Plex Sans 5.3.0)
  src/tokens.css                    tokens compartilhados
  src/site.css                      interface SSR (base, componentes, grade do renderer)
  src/markdown-editor/              editor de texto formatado (CodeMirror 6, ProseMirror, markdown-it), empacotado com esbuild
  tests/                            testes do editor (npm test)
  scripts/build.mjs                 build e verificação
skeleton/web/static/skeleton/web/
  site.css                          saída compilada e minificada (versionada)
  markdown_editor.js                editor de texto formatado: src/markdown-editor e bibliotecas, em módulos ES
  markdown_editor-*-<hash>.js       pedaços do editor (o modo Markdown, carregado só quando aberto)
  markdown_editor.LICENSES.txt      versão e licença de cada biblioteca empacotada no editor
  vendor/flowbite-4.0.2/            flowbite.min.js + LICENSE.md
  vendor/ibm-plex-sans-5.3.0/       woff2 (normal e itálico) + LICENSE
  vendor/VENDOR.json                versões, licenças e SHA-256 dos arquivos vendorizados
```

```sh
cd frontend
npm ci --ignore-scripts      # instala exatamente o lockfile, sem scripts de instalação
npm run build                # gera site.css, o editor de texto formatado e copia os assets vendorizados
npm run check                # falha se static/ estiver desatualizado em relação às fontes
npm test                     # testes do editor de texto formatado
```

Execute `npm run build` sempre que alterar `frontend/src/` **ou templates**: o Tailwind gera apenas as classes encontradas nos templates do núcleo (`skeleton/**/templates`, verificado por `tests/unit/test_css_sources.py`), `skeleton/contrib`, `skeleton/web/static/skeleton/web/*.js`, `skeleton/system/app_template/*.html-tpl` e `examples/notes/templates`. Versione os artefatos gerados junto com a alteração.

Templates de apps derivadas em `applications/` não são varridos porque o CSS integra o wheel do núcleo. Utilize neles as classes de componente listadas acima e utilitários já presentes no núcleo — utilitários inéditos do Tailwind não existirão em `site.css`. Estilos próprios de uma app devem ficar em arquivo estático da própria app, incluído via bloco `extrahead` e apoiado nas variáveis `--color-*` dos tokens.

- O source map do Flowbite não é distribuído; o build remove `sourceMappingURL` para evitar falhas de `ManifestStaticFilesStorage` durante `collectstatic`.
- `overrides` em `package.json` impedem que dependências transitivas do Flowbite puxem outra versão do Tailwind.
- Para atualizar Flowbite, Tailwind ou IBM Plex Sans: atualize a versão em `package.json`, execute `npm install --ignore-scripts`, ajuste o caminho versionado em `skeleton/web/templates/skeleton/web/base.html`, `skeleton/web/templates/skeleton/portal/base.html` e `tokens.css` (o build recusa referências à versão antiga), rode `npm run build` e revise as telas.
- As bibliotecas do editor (CodeMirror, ProseMirror, markdown-it e dependências — todas MIT, exceto `entities`, BSD-2-Clause) são empacotadas em `markdown_editor*.js`; `VENDOR.json` registra cada uma com `bundled_in` e `markdown_editor.LICENSES.txt` reúne as licenças. O bundle principal (modo visual, ~120 KB comprimido) só carrega em páginas com campo formatado; o modo Markdown (CodeMirror, ~100 KB comprimido) fica em chunk separado com hash no nome, importado apenas quando aberto. Todos os arquivos ficam abaixo do teto de 500 KB do hook `check-added-large-files`.

O job `assets` do CI executa `npm ci --ignore-scripts`, `npm run check` e `npm test`; veja [testes](testing.md#integração-contínua).
