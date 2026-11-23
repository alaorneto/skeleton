# Interface SSR: linguagem visual, tokens e build do CSS

A interface do skeleton é renderizada no servidor com templates Django. O visual usa **Tailwind CSS 4** e os componentes do **Flowbite 4** (navbar, dropdown, collapse, tabs, breadcrumb, cards, tabelas, alertas, badges, paginação). O CSS é **compilado e versionado** dentro do pacote: o wheel não depende de Node em runtime, e nenhum asset vem de CDN.

Este guia define os tokens visuais, os componentes disponíveis e o build. Ele não muda o contrato de segurança: formulários, CSRF, CAS, autorização e autoescape continuam como descritos em [arquitetura](architecture.md) e [extensão](extending.md).

## Direção visual: "Cobalto e Ardósia"

| Papel | Token | Claro | Escuro | Uso |
|---|---|---|---|---|
| Marca | `brand` | cobalto-600 `#3859ed` | cobalto-600 | botão primário, foco, aba ativa |
| Marca forte | `brand-strong` | cobalto-700 | cobalto-700 | hover/active do botão primário |
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

- O matiz 268 (entre azul e índigo) é sóbrio e convive com o botão oficial gov.br (#1351b4).
- Os neutros "ink" do modo escuro são levemente tingidos pela marca, para coesão.
- Contraste medido (WCAG 2.2 AA para texto pequeno): branco sobre `brand` 5,5:1; `fg-brand` sobre branco 7,1:1; `fg-brand` escuro sobre superfície 10:1; `body-subtle` sobre o fundo 4,6:1 (claro) e 7:1 (escuro).
- Links no meio do texto são sublinhados; a cor não é o único indicador.

Os valores ficam em `frontend/src/tokens.css`: escalas primitivas (`cobalt-*`, `ink-*`) e o mapeamento semântico do Flowbite 4. A escala `--color-cobalt-*` é o ponto de troca da marca: no build (altere a escala e recompile) e também em tempo de execução, pelo tema ativo da instância ([marca e tema da instância](#marca-e-tema-da-instância)), que redefine a escala inteira numa folha de estilo própria sem recompilar. Por isso o `site.css` compilado mantém `--color-brand: var(--color-cobalt-600)` e os demais tokens da marca como referências à escala (`tests/unit/test_appearance_themes.py` confere).

### Marca por organização

O administrador escolhe, em `/o/<org>/settings/`, um nome de exibição, uma cor primária e um logo ([#168](https://github.com/alaorneto/skeleton/issues/168)). A mesma página tem a seção "Endereço próprio" ([#480](https://github.com/alaorneto/skeleton/issues/480)), no mesmo formulário e com o mesmo botão de salvar: com o domínio base configurado e um slug que serve como rótulo DNS, mostra o endereço, o aviso do que fica público e a caixa "Ativar endereço próprio"; senão, só o motivo. Nas telas da organização, o nome e o logo substituem "Skeleton" no cabeçalho. A cor substitui só os tokens `--color-brand`, `--color-brand-strong`, `--color-fg-brand` e `--color-fg-brand-strong`, por um `<style>` no `base.html`.

- **Contraste:**
  - A cor precisa de contraste WCAG AA (4,5:1) com o branco, porque os botões levam texto branco sobre ela e os links a usam sobre o fundo claro.
  - No modo escuro, os textos usam uma variante clareada até atingir 4,5:1 sobre o fundo escuro.
  - O cálculo fica em `skeleton/tenancy/branding.py`.
- **CSS:** sai só de valores `#rrggbb` validados (há também um check no banco), nunca de texto livre. Quando houver CSP global, esse `<style>` vai precisar de nonce.
- **Logo:**
  - PNG, JPEG ou WebP de até 512 KB, conferido pelo conteúdo; SVG é recusado.
  - É servido inline só para membros, em `/o/<org>/settings/logo/`, com `Cache-Control: private`, `nosniff` e ETag.
  - Fora das telas da organização (`/me`, login, `/platform/`), vale a marca da instância, e sem ela o tema do núcleo.

### Marca e tema da instância

O administrador da plataforma define em `/platform/appearance/` o nome do produto, o rodapé, o logo (com variante opcional para o modo escuro), o ícone e o tema ativo da instância inteira ([aparência](appearance.md), epic [#591](https://github.com/alaorneto/skeleton/issues/591)). A precedência é **organização → instância → núcleo**:

- **Folha do tema:** `base.html` (e o portal e as páginas de erro) põe `<link rel="stylesheet" href="{{ instance_brand.theme_url }}">` no bloco `theme_link`, **depois** do `site.css` e **antes** do `<style nonce>` da organização, que vence pela ordem. Sem tema ativo não há `<link>`: o HTML fica igual ao de antes. O CSS livre nunca é embutido no HTML.
- **Nome:** `site_name` é o nome da organização no host dela, senão `instance_brand.name`, senão "Skeleton". Os títulos terminam com `· {{ site_name }}` e a plataforma com `Plataforma · {{ site_name }}`.
- **Logo do cabeçalho:** logo da organização → logo da instância (o enviado ou, sem ele, o do tema ativo de pacote, inclusive SVG) (`components/instance-logo.html`, um `<picture>` com `<source media="(prefers-color-scheme: dark)">` só quando há a variante escura e a pessoa segue o sistema; com o modo forçado, o servidor escolhe a imagem) → ícone do núcleo. O leiaute de entrada mostra o logo ou o nome da instância no cartão.
- **Favicon:** ícone da organização → ícone da instância (`/brand/icon/?v=…`, o enviado ou, sem ele, o do tema ativo de pacote) → `favicon.svg`. `/favicon.ico` segue a mesma ordem. **Manifesto:** no host da organização, `/site/manifest.webmanifest`; no domínio principal, `/brand/manifest.webmanifest` (`instance_brand.manifest_url`), só quando a instância tem nome, ícone ou cor. `theme-color`: cor da organização, senão a do tema da instância.
- **Rodapé:** com nome ou rodapé da instância, `footer_text` e `site_name`; sem nada, a frase e o "Skeleton" de sempre.
- **Páginas de erro:** `error_base.html` usa `{% load appearance %}{% appearance_cached as brand %}`, que lê só a memória do processo (nunca o banco), e continua sem `{% url %}` nem variável de contexto.
- **Modo seguro:** em `/platform/appearance/…` o `<link>` leva `safe=1` (só os tokens, sem o CSS livre); a pré-visualização sobrescreve `theme_link` com o CSS completo do tema candidato.
- Setup e página de manutenção continuam com o tema do núcleo: são renderizados antes do banco.

O processador `organization_shell` põe `instance_brand` em todo contexto: `name`, `footer_text`, `color`, `theme_url`, `logo_url`, `logo_dark_url`, `icon_url` e `manifest_url` (URLs com `?v=<versão>`, vazias quando não há valor).

Para criar um tema a partir de um sistema de design, veja [criar e publicar um tema](themes.md). **Contrato estável para o CSS personalizado de um tema:** os tokens `--color-*` (escalas `cobalt-*` e `ink-*` e os semânticos `brand*`, `fg-*`, `neutral-*`, `default*`, `success*`, `warning*`, `danger*`, `focus`), `--radius-base`, `--radius-card`, `--font-sans` e `--font-body` (um tema de pacote com fontes põe a família dele na frente de `--font-sans`), e as classes de componente desta página (`.card`, `.btn`, `.btn-primary`, `.btn-secondary`, `.btn-danger`, `.badge*`, `.message`, `.alert*`, `.page-header`, `.page-eyebrow`, `.page-description`, `.sidebar*`, `.link-tabs`, `.table-scroll`, `.form-stack`, `.form-actions`, `.error-summary`, `.app-header`, `.app-footer`). Classes utilitárias do Tailwind e a estrutura interna dos templates podem mudar entre versões.

### Modo escuro

Cada pessoa escolhe **Claro**, **Escuro** ou **Seguir o sistema** (o padrão), sem script e sem flash, compatível com CSP (epic [#667](https://github.com/alaorneto/skeleton/issues/667)).

- **Onde escolher:** em `/profile/` (campo "Tema") e no menu da conta do cabeçalho, com três botões de envio num `<form method="post">` para `/me/appearance/` (CSRF e a revisão do perfil; o formulário nunca leva o caminho da página, e a volta é pelo `Referer`, aceito só quando é do mesmo host, senão `/profile/`). Revisão desatualizada não muda nada e avisa. O seletor só aparece numa resposta com token CSRF.
- **Onde fica:** `UserProfile.color_scheme` (`""`, `light`, `dark`) é a fonte de verdade; o cookie `sk_color_scheme` (só `light` ou `dark`, `HttpOnly`, `SameSite=Lax`, um ano, com o domínio do cookie de sessão) espelha a escolha para as telas antes do login e depois do logout no mesmo navegador. `skeleton/web/color_scheme.py` resolve a ordem perfil → cookie → sistema; o `ColorSchemeMiddleware` grava `request.color_scheme` e acerta o cookie quando ele diverge do perfil (fora de `/api/`).
- **Como sai:** `<html data-color-scheme="light|dark">` só quando o modo é forçado, e `<meta name="color-scheme">` com o modo (ou `light dark`). Em `frontend/src/tokens.css`, o variant `dark` casa `[data-color-scheme=dark]` ou, dentro de `@media (prefers-color-scheme: dark)`, o que não está sob `[data-color-scheme=light]`, sempre em `:where()`, para manter a especificidade de `:root`. Nunca a classe `dark`: ela ligaria o bloco `.dark` do Flowbite.
- **Tema e marca:** a folha do tema vem com `&scheme=` ([aparência](appearance.md#rotas-públicas)) e o `<style>` da organização passa pela mesma troca das consultas `prefers-color-scheme` (`skeleton/appearance/color_scheme.py`), então os temas continuam escrevendo `@media (prefers-color-scheme: dark)`.
- **Seguem o sistema:** páginas de erro (sem contexto), setup, manutenção e e-mails.

### Tipografia

- **IBM Plex Sans Variable** (OFL 1.1, #690), auto-hospedada em `static/skeleton/web/vendor/ibm-plex-sans-5.3.0/` (subsets latin e latin-ext, normal e itálico verdadeiro), pesos de 100 a 700, `font-display: swap`; nenhum pedido a domínio externo de fontes. O desenho já distingue l, I e 1. Um tema que não escolhe fonte (`font: default`) a herda; `system` e `serif` usam as fontes do sistema.
- Monoespaçada do sistema (`ui-monospace`, Cascadia Mono, SF Mono…) para UUID, digest e código.
- Escala: H1 28/36 semibold (24/32 no celular), H2 18/28, H3 16/24, corpo 16/24, tabelas e metadados 14/20, rótulos de seção 12–14 em maiúsculas com tracking. Números tabulares nas tabelas.
- Campos usam 16 px no celular (evita zoom do iOS) e 14 px a partir de 640 px.

### Espaçamento, forma e profundidade

- Grade de 4 px. Contêiner de até 1280 px com gutter de 16/24/32 px; telas de detalhe e formulário limitam-se a 1024 px.
- Cabeçalho de página padrão (`.page-header`): breadcrumb ou eyebrow → `h1` → descrição → 32 px (24 px no celular) até o conteúdo; ações à direita no desktop e abaixo no celular.
- Raios: controles 8 px (`rounded-base`), cartões 12 px (`rounded-card`), avatares e pílulas redondos.
- Profundidade por borda e sombra mínima (`shadow-card`); sombra maior (`shadow-pop`) apenas em menus suspensos.

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

- O breadcrumb fica dentro do primeiro `div` do `page-header`; toda tela tem eyebrow ou breadcrumb, e telas de lista têm descrição de uma frase.
- A barra de ações do cabeçalho é `<nav class="toolbar" aria-label="Ações">`; o botão principal é `btn btn-primary` (`.button` continua só como alias legado).
- O título da aba é `<título da tela> · Skeleton`; nas telas da plataforma, `<título> · Plataforma · Skeleton` (#476).

### Portal de pessoas externas

As páginas do [portal](portal.md) estendem `skeleton/portal/base.html`: cabeçalho com a marca da organização (logo, nome e cores, como em [marca por organização](#marca-por-organização)), o botão de sair e o conteúdo, sem barra lateral, menus de membro ou contadores, e sem JavaScript. Os componentes de sempre (`link-card`, tabelas, formulários, `error-summary`) valem ali.

## Navegação

A interface tem três áreas, sempre identificáveis pelo cabeçalho e pelo menu lateral:

| Área | Entrada | Conteúdo |
|---|---|---|
| Organização (`/o/<org>/…`) | seletor de organização no cabeçalho | menu lateral: só **Início** e as entradas `navigation` dos apps ativos (sem apps, "Nenhum aplicativo disponível."); ao lado do nome da organização, a **engrenagem** (`aria-label="Configuração da organização"`) para quem vê algum item da área de configuração. Tarefas e Notificações ficam no cabeçalho. As telas de registro (`web:records`, `web:record`…) mantêm esse menu, porque os apps linkam para elas |
| Configuração da organização (`/o/<org>/admin/`, #621) | engrenagem do menu lateral | hub com cartões por grupo e, em toda rota da área (`SETTINGS_ROUTE_NAMES`), o menu lateral de configuração: "Voltar para a organização", "Visão geral" e os grupos **Organização** (Geral; Domínios próprios, com o recurso ligado; Página pública, #623; Página inicial, #624), **Pessoas e acesso** (Pessoas, Grupos, Papéis e permissões, Segurança, #740, Pessoas externas), **Admissão** (Solicitações, Links de ingresso, Admissão, Domínios verificados, Identidade), **Aplicativos e integrações** (Aplicativos, Webhooks, Contas de serviço, Fluxos), **Dados** (Tabelas, Auditoria) e **Cobrança** (só com `SKELETON_BILLING_ENABLED`), cada item só para quem passa na porta dele, com `aria-current` no atual |
| Administração da plataforma (`/platform/…`) | link "Plataforma" no cabeçalho, só para staff+superuser | menu lateral: Visão geral · Identidade (Usuários, Organizações) · Aplicativos (Aplicativos instalados, Versões e atualizações) · Dados (Catálogo de tabelas, Auditoria global) · Operação (Cobrança, Aparência, Manutenção, Jobs, Pedidos de eliminação; Aparência, em `/platform/appearance/`, é a marca e o tema da instância, só para o administrador completo). Views próprias em `skeleton/web/platform`; Versões e atualizações (`/platform/updates/`) segue em etapas: instaladas, fontes (repositórios do GitHub consultados, #499), novas versões (a última consulta e um cartão por componente com o caminho de atualização, em que só o primeiro passo tem botão, #500), enviar pacote, preparar plano, operações; um passo do core que exige agente mais novo abre "Atualize o agente primeiro", e a página de uma operação concluída oferece o próximo passo. Nas outras páginas de `/platform/`, quem tem `updates.operate` vê uma faixa de aviso (`message`, `role="status"`, bloco `notices` do `base.html`) quando há atualização, e a visão geral mostra o resumo ou "Tudo atualizado" (#501). |
| Minha conta | menu da conta | Perfil, Email, Senha, Autenticação em dois fatores |
| Página inicial pública (`/` sem login, #622) | raiz do host da plataforma | a página em blocos publicada em `/platform/appearance/home/`, no leiaute `pages/public.html` com a marca da instância; sem página publicada, o login |

- O seletor lista os vínculos ativos do usuário (`memberships_for_user`); ele só cria links, e cada tela revalida a autorização.
- **Endereço da organização (#483).** O processador `organization_shell` põe em todo contexto, inclusive anônimo, `site_mode` (verdadeiro no host `<slug>.<domínio base>`) e `organization_site` (o `Site` do índice; não se chama `site` porque as views do allauth já põem um `site` no contexto: `organization_id`, `slug`, `name`, `brand_color`, `logo_key`, `default_language`). Em modo de site o cabeçalho não tem o seletor de organização, "Todas as organizações" nem "Plataforma" (nem para administrador da plataforma); "Notificações" e "Tarefas" levam às listas da organização e os contadores contam só ela; o rodapé mostra só o nome da organização. Textos alcançáveis por membro comum que falam de "organizações" ou da plataforma têm a alternativa `{% if site_mode %}`. Páginas de administração que citam a plataforma por necessidade (cobrança, disponibilidade de apps) ficam como estão.
- Enquanto uma regra exige MFA e a pessoa ainda não o cadastrou ([quem cadastra o MFA](authentication-sso.md#mfa-cifrado-e-obrigatório), #740), toda página vira a de cadastro do MFA, e o shell esconde a navegação principal, a barra lateral da organização e o menu "Minha conta": `request.mfa_enrollment_pending` decide em `skeleton/web/base.html` e `account/base_manage.html`. Ficam só a marca, que leva à página de MFA, e o botão de sair.
- A administração de acesso é agrupada por tarefa em `skeleton/web/navigation.py` (`ACCESS_SECTIONS`): **Pessoas** (membros, convites, papéis de membros), **Grupos** (grupos, membros de grupos, papéis de grupos) e **Papéis e permissões** (papéis, permissões de tabelas, concessões de registros). Cada seção é um item da área de configuração e seus recursos viram abas na lista.
- **Área de configuração (#621).** `SETTINGS_GROUPS` em `skeleton/web/navigation.py` é dado, não regra: cada item é `SettingsItem(url_name, rótulo, ícone, porta, rotas)`. As portas são uma capacidade (`members.view`, `access.manage`, `admission.decide`, `admission.configure`, `invitations.manage`, `identity.configure`, `external.manage`, `integrations.manage`, `flows.manage`, `apps.manage`), `organization_admin`, `custom_domains` (recurso ligado e administrador), `billing` (cobrança ligada e `billing.view`), `audit` (o administrador ou quem tem a ação `audit` em alguma tabela), `member` (Tabelas, que todo membro vê) e `access:<seção>` (uma seção de `ACCESS_SECTIONS` que a pessoa vê). `organization_shell` expõe `settings_area` (a rota atual está em `SETTINGS_ROUTE_NAMES`), `settings_navigation` (os grupos já filtrados) e `can_open_settings` (algum item além dos que todo membro vê); o hub (`skeleton/web/settings_views.py`, `@tenant_page()`) responde o 404 neutro a quem não tem nenhum. O shell não consulta o banco de novo: reaproveita as capacidades, o administrador e as permissões de tabela que o processador `application_navigation` já leu na mesma renderização. Só os links mudam de lugar: Tabelas, Auditoria e as telas de administração continuam abertas pela URL a quem já podia, e negadas a quem não podia.
- Telas de parâmetros de apps (`parameter_panel`) não mostram atalhos para rotas de negócio, conforme o [contrato de administração](application-admin-ui-plan.md).
- **Busca global (#584).** Dentro de uma organização, quando ao menos uma tabela registrada tem `TableSpec.global_search`, o cabeçalho traz a caixa de busca (`.header-search`, primeiro item de `header-end`): no celular ela fica dentro do menu recolhível e, a partir de 1280 px (`xl`), no próprio cabeçalho; entre 768 e 1280 px, onde ela não cabe ao lado dos outros itens, aparece só um link com o ícone (`.header-search-link`) para a página de busca, e o email no botão da conta fica limitado a `max-w-32`. `organization_shell` decide isso pelo registro em memória (`global_search_declared()`), sem consulta. A caixa é um formulário GET simples, sem JavaScript, que leva a `/o/<org>/search/?q=<termo>`: a página mostra o formulário, um cartão por tabela com resultado (rótulo da tabela e links para os registros) e, quando a tabela tem mais resultados que o limite, "Ver todos os resultados", que abre a lista da tabela com o mesmo `q`. Sem resultado (ou sem tabela legível), "Nenhum resultado."; termo inválido mantém a página com a mensagem no campo e status 400. Fora de uma organização (página inicial, plataforma) não há caixa.
- No celular, o menu principal e o menu da organização são painéis recolhíveis (Flowbite collapse); sem JavaScript, ficam abertos.

## Componentes para templates

Use as classes abaixo nos templates do núcleo e das apps; elas encapsulam os padrões do Flowbite com os tokens.

| Classe | Uso |
|---|---|
| `page-header`, `page-eyebrow`, `page-description`, `section-heading` | Cabeçalho de página e rótulos de seção. |
| `btn` + `btn-primary` / `btn-secondary` / `btn-danger` / `btn-danger-outline` / `btn-ghost`, `btn-sm` | Botões e links com aparência de botão. `.button` continua como alias do primário. |
| `card`, `link-card` (+ `link-card-icon`, `link-card-title`, `link-card-text`) | Superfícies e atalhos navegáveis. |
| `table-scroll` | Tabela responsiva (rola dentro do cartão, nunca a página); classes de coluna e de célula em [Tabelas](#tabelas). `empty-state` para listas vazias. |
| `pagination` | Componente `skeleton/web/pagination.html` (resumo "Mostrando X–Y de N" e Anterior/Próxima). |
| `components/cursor-pagination.html` | Paginação por cursor (`next_url`, `label`): "Carregar mais" só quando há próxima página. A view monta `next_url` com `urlencode`, preservando os outros parâmetros (o escopo das tarefas). |
| `stat-grid`, `stat` | Indicadores: `<dl class="stat-grid">` com `{% include "skeleton/web/components/stat.html" with label=… value=… hint=… key=… %}` (`key` vira `data-count`). Duas colunas no celular, quatro a partir de 640 px. |
| `filter-bar` | Filtro em linha: `<form method="get" class="filter-bar" aria-label="Filtros">`, um `<div class="field">` por campo com rótulo visível e `<div class="filter-actions">` com "Filtrar" e, só com filtro aplicado, "Limpar" (`href="?"`). |
| `breadcrumbs`, `toolbar`, `filter-panel`, `record-tabs` | Navegação, ações, filtros e abas. |
| `header-search` | Caixa da [busca global](#navegação) no cabeçalho: `<form class="header-search" method="get" role="search">` com rótulo `sr-only`, `input type="search"` e botão `btn btn-secondary btn-sm` só com o ícone. Só o cabeçalho a usa; entre `md` e `xl` ela some e o link `header-link header-search-link` (só o ícone, rótulo `sr-only`) toma o lugar. |
| `link-tabs`, `segmented` | `link-tabs`: abas entre telas irmãs (links com `aria-current="page"`). `segmented` (`components/segmented.html`, com `options` = `[{label, url, current}]` e `label`): alternância de um filtro na mesma tela, uma opção com `aria-current`. Nunca um `badge` como opção ativa ao lado de botões do mesmo grupo (#472). |
| `message`/`alert` (+ `success`, `warning`, `error`), `error-summary`, `confirmation` | Retorno ao usuário. |
| `badge` (+ `badge-brand`, `badge-success`, `badge-warning`, `badge-danger`, `badge-neutral`, `badge-dot`), `uuid`, `meta-list` | Estados e metadados; estado de um registro sempre pelo filtro `status_badge`. |
| `form-stack`, `form-field`, `helptext`, `errorlist`, `form-actions` | Formulários simples; `components/form-fields.html` substitui `form.as_p` mantendo ids, ajuda, erros e `aria-describedby`. Todo campo obrigatório (Django `required`, `marked_required` ou o atributo `data-required` no widget) leva `*` e os demais "(opcional)"; checkbox não leva marca; acima dos campos aparece uma vez "Campos com * são obrigatórios.". `hide_marks=True` no `include` desliga as marcas (login, LDAP, token do setup). O renderer de registros (`skeleton_crud/field.html`) segue a mesma regra, sem "(opcional)" nos campos com a caixa "alterar este campo" (#474). |
| `required`, `optional` | Marcas do rótulo: `*` nos obrigatórios (`show_required`) e "(opcional)" nos demais (`show_optional`), passadas ao `include` de `form-fields.html`. |
| `setup-steps`, `setup-step-number` | Lista numerada das etapas do setup do primeiro acesso (vertical no desktop, linha rolável no celular); `setup-step-done` e `setup-step-later` marcam o estado. |
| `setup-files`, `data-files-section` | Passo de anexos do setup ([#729](https://github.com/alaorneto/skeleton/issues/729)): o contêiner dos rádios `files_backend` e das seções `data-files-section="local"` e `"s3"`; o CSS (`:has(input:checked)`) mostra só a seção da opção marcada, sem script. Sem CSS ou sem `:has`, as duas aparecem; os campos escondidos continuam no formulário e a validação é do servidor. |

### Tabelas

Toda lista é `<div class="table-scroll"><table>…</table></div>` ([#469](https://github.com/alaorneto/skeleton/issues/469)). A tabela rola dentro do cartão; a página nunca rola na horizontal. As células são alinhadas pelo meio, não preservam o espaço do template e não quebram palavras letra a letra.

| Classe | Onde | Uso |
|---|---|---|
| `col-num` | `th` e `td` | Números, alinhados à direita, sem quebra. |
| `col-date` | `th` e `td` | Datas (com o filtro `moment`), sem quebra. |
| `col-actions` | `th` e `td` | Última coluna, de ações: largura mínima, à direita. |
| `col-text` | `td` | Texto que pode ter várias linhas (até 28rem, quebras preservadas). |
| `cell-primary` | link ou texto | Nome do item, na primeira coluna. |
| `cell-secondary` | `span` | Segunda linha da célula (identificador técnico, detalhe). |
| `cell-truncate` | `span` | Texto cortado com reticências numa linha. |
| `row-actions` | `div` | Grupo de botões da coluna de ações. |

- `{% load skeleton_format %}{{ estado|status_badge }}` mostra o código de estado como badge traduzido e colorido (sucesso, em andamento, atenção, falha, neutro). O argumento é um domínio do mapa (`"flows"`, `"invoice"`, `"jobs"`…) quando o mesmo código muda de sentido, ou um rótulo que substitui o do mapa (`{{ job.state|status_badge:job.get_state_display }}`). Um código desconhecido sai neutro, com o próprio texto. Todo `TextChoices` `*Status`/`*State` do núcleo tem entrada no mapa (teste em `tests/unit/test_format_filters.py`).
- Pessoas pelo nome: a view junta os identificadores da página e chama `skeleton.web.people.people_labels(ids, actor=request.user)` **uma vez** (que usa `IdentityService.people`, só membros ativos da organização atual); o template usa `{{ row.actor_sysid|person:people }}`, que mostra o nome, "Sistema" para vazio e "Pessoa indisponível" com os 8 primeiros caracteres do UUID para quem não é membro ativo. Assim um nome nunca vaza de outra organização. A auditoria global (`/platform/audit/`) continua com o UUID completo ([#475](https://github.com/alaorneto/skeleton/issues/475)).
- Lista vazia: `{% include "skeleton/web/components/empty-row.html" with colspan=4 message=_("Nenhum token.") hint=_("Crie um token para integrar.") icon="key" %}`.
- A tabela genérica de registros (`components/record-table.html`) é a referência: `list_context` devolve cada célula como `{"text", "kind"}` e cada cabeçalho como `{"label", "kind"}`, com `kind` em `text`, `num`, `date` ou `bool` pelo tipo do campo, e corta na lista textos com mais de 160 caracteres (o detalhe mostra tudo). Um template derivado que copiou a versão anterior (com `{{ cell }}` e `{{ heading }}`) passa a usar `{{ cell.text }}` e `{{ heading.label }}`.

Regras:

1. A primeira coluna é o nome do item, como link (`cell-primary`).
2. Identificadores técnicos (UUID, código) vão numa segunda linha (`cell-secondary`), nunca em coluna própria, a não ser que sejam o único identificador.
3. Números à direita (`col-num`); datas com `col-date` e o filtro `moment`.
4. Estado sempre com `status_badge`, nunca o código cru.
5. Ações na última coluna (`col-actions`), dentro de `<div class="row-actions">`, com botões `btn btn-secondary btn-sm` e a ação destrutiva `btn btn-danger-outline btn-sm`; o cabeçalho é `<span class="sr-only">Ações</span>`.
6. Lista vazia com `empty-row.html`.
7. Nunca `<br>` para empilhar: use `cell-secondary`.

### Páginas de erro

Toda recusa mostra uma página em português, com a marca, uma frase do que aconteceu e o caminho de volta ([#473](https://github.com/alaorneto/skeleton/issues/473)).

- **Erros do Django:** `400.html`, `403.html`, `403_csrf.html` ("A página expirou", com "Entrar novamente"), `404.html` e `500.html`, na raiz de `skeleton/web/templates/`. Estendem `skeleton/web/error_base.html`, um layout autônomo: não estende `base.html`, não usa `{% url %}` nem variável de contexto, e por isso renderiza no 500 e em modo setup. A ação padrão é "Ir para o início" (`/`).
- O `404.html` tem um texto só para "não existe" e "sem acesso" e não mostra o caminho pedido: as duas respostas são idênticas byte a byte (teste A/B em `tests/integration/test_error_pages.py`). O 500 não mostra a exceção; o traceback vai só para o log e o relato de erros. A página de CSRF não muda a verificação (não há `CSRF_FAILURE_VIEW`).
- **Erros de operação:** `skeleton.web.errors.error_response(request, detail, *, status, back_url=None, back_label=None)` renderiza `skeleton/web/error.html` com um título pelo status (400 "Não foi possível concluir", 403 "Acesso não permitido", 404 "Página não encontrada", 409 "Os dados mudaram", 413 "Arquivo grande demais", 429 "Muitas tentativas"), o detalhe e "Voltar" ("Voltar e recarregar" no 409) para `back_url` ou para o `Referer` validado com `url_has_allowed_host_and_scheme` contra o host do pedido; sempre há "Ir para o início". Nunca chame `render` com `error.html` direto.
- Recusa de validação que tem uma tela de origem (envio de anexo) volta a ela com `messages.error`, em vez de uma página de erro.

### Datas e horas

Toda data e hora na interface aparece como `15/11/2026 14:30` (só a data: `15/11/2026`; só a hora: `14:30`), no fuso efetivo da pessoa, da organização ou da instância ([#468](https://github.com/alaorneto/skeleton/issues/468)). O formato vem de um ponto só, `FORMAT_MODULE_PATH = ["skeleton.conf.formats"]` (pacote `skeleton/conf/formats/pt_BR/`), que também vale para `presentation._value` nas listas e no detalhe genéricos.

- Use `{% load skeleton_format %}` e `{{ valor|moment }}`, `{{ valor|moment:"date" }}` ou `{{ valor|moment:"time" }}`. O filtro gera `<time datetime="ISO">texto</time>`, devolve "—" para vazio e escapa um texto que não é data.
- Nunca imprima uma data crua (`{{ x.created_at }}`) nem `|default:"—"` numa data: `tests/unit/test_template_dates.py` recusa. As exceções são os corpos de email.
- Os valores "Antes"/"Depois" da auditoria são o dado gravado e não passam pelo filtro.

Ícones: `{% include "skeleton/web/components/icon.html" with name="table" class="size-5" %}` (traço 24 px, estilo Heroicons, decorativos com `aria-hidden`).

Elementos sem classe continuam legíveis: `h1`–`h3`, links, listas, `code`, `pre`, inputs, selects e `button` têm estilo de base. Telas de apps que ainda usam `<h1>`/`<p>` soltos recebem o mesmo ritmo do cabeçalho padrão.

### Linha do tempo do registro

A página de detalhe mostra uma **linha do tempo** que junta a auditoria e os anexos do registro, do mais recente para o mais antigo, com até 50 itens ([#185](https://github.com/alaorneto/skeleton/issues/185)).

- **Auditoria:** passa por `AuditService.history`, que exige o administrador ou a TablePermission `audit` mais a leitura do registro. A linha do tempo mostra só os nomes das colunas alteradas.
- **Anexos:** seguem a autorização do alvo.

Novas fontes entram com `skeleton.web.timeline.register_timeline_source`, e cada uma devolve só o que já mostraria sozinha.

### Título e marca no endereço da organização

O título de toda página termina com `· {{ site_name }}` (`<tela> · {{ site_name }}`), nunca com o literal "Skeleton" ([#484](https://github.com/alaorneto/skeleton/issues/484)). `site_name` vem do processador `organization_shell`: o nome público da organização no host `<slug>.<domínio base>` e, em qualquer outro host, o nome do produto da instância ou "Skeleton" ([#598](https://github.com/alaorneto/skeleton/issues/598)). No host da organização a marca aparece também nas páginas anônimas (login, cadastro, senha, MFA, "sem vínculo"): sem contexto de tenant, `organization_brand` vem do índice `OrganizationSite` (nome, paleta e `/site/logo/`), sem ler `OrganizationSettings`; dentro do tenant vem das configurações, como antes, mas o logo usa a URL pública `/site/logo/?v=…`. O leiaute de entrada (`account/base_entrance.html`) mostra o logo ou o nome no cartão, o rodapé mostra só o nome e `<meta name="theme-color">` leva a cor da marca. Não há template de 404/500 próprio: as respostas de erro do Django continuam em texto puro, sem marca.

### Favicon, ícone da organização e manifesto

O favicon padrão do núcleo (`frontend/src/favicon.svg`, na cor de marca padrão) é copiado pelo build (`frontend/scripts/build.mjs`) para `skeleton/web/static/skeleton/web/favicon.svg`, e `npm run check` recusa uma cópia divergente; nada é editado à mão em `static/`. `base.html` (e por ele o leiaute de entrada e a plataforma) e o leiaute do setup têm `<link rel="icon">`. O administrador envia em `/o/<org>/settings/` um ícone PNG quadrado de exatamente 512 × 512 pixels, até 256 KB, provado pelo conteúdo (assinatura e cabeçalho `IHDR`, sem biblioteca de imagem; [#485](https://github.com/alaorneto/skeleton/issues/485)). `organization_icon_url`, do processador `organization_shell`, aponta para `/site/icon/?v=…` no host da organização e para `/o/<org>/settings/icon/?v=…` (só membros) nas páginas da organização no domínio principal; vazio, vale o favicon padrão. Trocar o ícone muda o `?v=`. No host da organização há ainda `<link rel="manifest">` para `/site/manifest.webmanifest` (nome, nome curto de 12 caracteres, cor e ícone, todos já públicos).

### Texto nos templates

Todo texto visível passa pelo gettext ([#172](https://github.com/alaorneto/skeleton/issues/172)). Isso vale para o conteúdo entre tags, como em `{% translate "Salvar" %}`, e para os atributos visíveis (`alt`, `title`, `placeholder`, `aria-label`). Nos argumentos de tags, use `_("…")`, como em `{% include "…" with label=_("Abrir") %}`. As msgids ficam em pt-BR e o template carrega `{% load i18n %}`. Em frases com variável, prefira `{% blocktranslate trimmed %}` com nomes simples, usando `with` para os atributos. Dados (nomes, emails, valores) nunca entram no gettext.

### Regras que não mudam

- Sem scripts inline e sem CDN: o JS do Flowbite é servido de `static/skeleton/web/vendor/flowbite-4.0.2/`.
- Sem JavaScript tudo funciona: o menu móvel e o menu da conta ficam abertos via `@media (scripting: none)` (variante `noscript:`); as abas do registro voltam a ser seções empilhadas.
- Abas mantêm ARIA e teclado (ArrowLeft/ArrowRight/Home/End) em `site.js`.
- Envio duplo: `site.js` ignora um segundo envio do mesmo formulário POST até a página mudar e marca os botões com `aria-disabled` (nunca `disabled`, que perderia o `name`/`value` do botão clicado); voltar pelo navegador (`pageshow`) reabilita. Um formulário que responde com arquivo na mesma página usa `data-allow-resubmit` (#474).
- Até 600 px o renderer usa uma coluna e não há rolagem horizontal da página; acima disso vale a grade de 12 colunas (`field-span-1..12`), gerada fora do scanner porque vem do manifesto.

## Páginas em blocos

As páginas que o administrador configura (a página inicial pública da plataforma, a página pública e o início interno da organização; epic [#618](https://github.com/alaorneto/skeleton/issues/618)) são um documento JSON versionado de **blocos estruturados de texto simples** ([#619](https://github.com/alaorneto/skeleton/issues/619)). Não há HTML, Markdown, imagem enviada nem estilo por bloco: todo valor sai com autoescape.

```json
{"version": 1, "blocks": [
  {"type": "hero", "title": "Empresa Exemplo", "subtitle": "Atendimento de segunda a sexta", "login_button": true},
  {"type": "text", "heading": "Quem somos", "body": "Parágrafo um.\n\nParágrafo dois."},
  {"type": "links", "heading": "Links", "items": [{"label": "Blog", "url": "https://exemplo.test/blog"}]}
]}
```

`skeleton.common.page_blocks.validate_page(document, *, kind, known_widgets=frozenset())` é a única porta: devolve o documento normalizado (texto com `strip`, campos ausentes preenchidos com o padrão) ou levanta `ValidationError` com uma mensagem por bloco e campo (chaves `blocks.<i>.<campo>` e `blocks.<i>.items.<j>.<campo>`, a partir de 0). O módulo é puro (regra R1) e exporta `PAGE_KINDS`, `BLOCK_TYPES` (por tipo de página), `FIELDS` (os campos de cada tipo, que o editor lê), `EMPTY_PAGE` e `PageState(document, published, revision)`. Com `allow_empty=True`, a validação aceita um documento sem blocos, o de uma página ainda não publicada.

| Tipo | Campos (com `strip`; limites em caracteres) | Vale em |
|---|---|---|
| `hero` | `title` 1–120 obrigatório; `subtitle` 0–300; `login_button` (padrão `true`, leva ao login) | `platform_home`, `organization_public` |
| `text` | `heading` 0–120; `body` 1–4000 (parágrafos separados por linha em branco) | todos |
| `contact` | `email` 0–254 (e-mail válido); `phone` 0–40 (só números, espaço e `+ ( ) - .`); `address` 0–300; `hours` 0–300; ao menos um preenchido | todos |
| `links` | `heading` 0–120; `items` de 1 a 10 `{label 1–60, url}`; `url` é `https://…` (até 500) ou caminho local que começa com `/`, sem `//`, `\`, espaço nem caractere de controle | todos |
| `apps`, `tasks`, `notifications` | `heading` 0–120 | `organization_home` |
| `widget` | `name`, um [quadro registrado por uma app](extending.md#quadros-do-painel-da-organização) | `organization_home` |

Também: `version` igual a 1; de 1 a 20 blocos; até 32 768 bytes UTF-8 serializado; chave ou tipo desconhecido recusado; tipo fora da página recusado; caractere de controle recusado (exceto a quebra de linha em `body`, `address` e `hours`).

**Renderização.** `skeleton/web/templates/skeleton/web/pages/blocks.html` inclui `block-<tipo>.html` para cada bloco. Todo texto passa pelo autoescape (`body|linebreaks`); link externo leva `rel="noopener noreferrer"`; nenhum template de página tem `style`, `script`, `|safe` ou atributo de evento (`tests/unit/test_page_templates.py`). O botão do `hero` leva a `account_login`. `pages/public.html` é o leiaute de quem não entrou: a mesma base visual da entrada, com largura de conteúdo maior.

**Editor.** `skeleton/web/page_editor.py` é o editor SSR compartilhado, sem JavaScript. Uma página concreta monta um `PageEditor` com o seu *store* (`load() -> PageState` e `save(document=, published=, expected_revision=)`, que valida), a autorização (os decoradores da view), o template base e a pré-visualização, e registra as rotas com `editor_urls(prefixo, nome, view)`: a lista dos blocos, `preview/`, `publish/`, `blocks/new/?type=…`, `blocks/<i>/`, `blocks/<i>/move/` e `blocks/<i>/delete/` (confirmação em GET, exclusão em POST). Toda mudança é um POST com CSRF e a revisão lida: revisão antiga responde 409 com "Esta página foi alterada por outra pessoa. Recarregue para ver a versão atual." sem gravar; erro de validação responde 400 com o `error-summary` e os erros no campo; campo de POST desconhecido responde 400. Os links ficam num `textarea`, uma linha `Rótulo | endereço` por item. A pré-visualização usa o mesmo template da página real.

### Início da organização

O início interno (`/o/<org>/`, rota `web:dashboard`; [#624](https://github.com/alaorneto/skeleton/issues/624)) é um painel configurável. O administrador o compõe em **Configuração › Organização › Página inicial** (`/o/<org>/admin/pages/home/`) com o mesmo editor das páginas em blocos, e publica. Sem página publicada vale o padrão `DEFAULT_ORGANIZATION_HOME`: **Aplicativos**, **Minhas tarefas** e **Notificações**. "Seu vínculo" (sair da organização) fica sempre no rodapé. Um início por organização, igual para todos os membros; cada pessoa vê nele só o que já pode ver.

| Bloco | O que mostra |
|---|---|
| `apps` | Os cartões das entradas `navigation` dos apps ativos que a pessoa pode usar (`application_navigation`). |
| `tasks` | Até 5 tarefas abertas atribuídas à pessoa nesta organização, a mesma consulta da caixa de tarefas (`skeleton/flows/inbox.py`), com link para `flows:tasks`. |
| `notifications` | Até 5 notificações não lidas da pessoa nesta organização (`skeleton/notifications/inbox.py`), com link para `notifications:organization`. |
| `widget` | Um [quadro de app](extending.md#quadros-do-painel-da-organização), só se `visible_widgets` o devolve à pessoa; com a app desligada ou sem a porta, o quadro some; com erro, vira o quadro neutro. |
| `text`, `contact`, `links` | Os mesmos blocos das páginas públicas. |

No editor, a escolha de `widget` lista os quadros que o administrador vê; a validação aceita todo nome registrado, então desligar uma app não invalida a página. A pré-visualização (`…/home/preview/`) é o próprio início com o documento em edição. Com o documento padrão, o início de um membro custa um número fixo de consultas (`tests/integration/test_organization_home_page.py`).

## Django admin

Não há tela do Django admin: toda a administração da plataforma usa views próprias em `/platform/`, no mesmo shell, componentes e tokens do site. O Django admin não é usado: `django.contrib.admin` fica instalado só como dependência técnica (modelo `LogEntry` e migrations históricas que concedem privilégios em `django_admin_log`), via `SimpleAdminConfig`, sem autodiscover, site ou rota; `/admin/…` responde 404.

## Build do CSS

```text
frontend/
  package.json, package-lock.json   versões fixas (Tailwind 4.3.5, Flowbite 4.0.2, IBM Plex Sans 5.3.0)
  src/tokens.css                    tokens compartilhados
  src/site.css                      interface SSR (base, componentes, grade do renderer)
  scripts/build.mjs                 build e verificação
skeleton/web/static/skeleton/web/
  site.css                          saída compilada e minificada (versionada)
  vendor/flowbite-4.0.2/            flowbite.min.js + LICENSE.md
  vendor/ibm-plex-sans-5.3.0/       woff2 (normal e itálico) + LICENSE
  vendor/VENDOR.json                versões, licenças e SHA-256 dos arquivos vendorizados
```

```sh
cd frontend
npm ci --ignore-scripts      # instala exatamente o lockfile, sem scripts de instalação
npm run build                # gera site.css e copia os assets vendorizados
npm run check                # falha se static/ estiver desatualizado em relação às fontes
```

Rode `npm run build` sempre que alterar `frontend/src/` **ou templates**: o Tailwind só gera as classes que encontra nos templates de todos os pacotes do núcleo (`skeleton/**/templates`, conferido por `tests/unit/test_css_sources.py`), em `skeleton/contrib`, `skeleton/web/static/skeleton/web/*.js`, `skeleton/system/app_template/*.html-tpl` e `examples/notes/templates`. Versione o resultado junto com a mudança.

Templates de apps derivadas em `applications/` não são varridos, porque o CSS faz parte do wheel do núcleo. Nesses templates use as classes de componente da tabela acima e utilitários que o núcleo já usa; um utilitário Tailwind inédito não existirá no `site.css`. Estilo próprio de uma app vai num arquivo estático da própria app, carregado pelo bloco `extrahead`, usando as variáveis `--color-*` dos tokens.

- O mapa de fonte do Flowbite não é distribuído; o build remove a referência `sourceMappingURL` para que `ManifestStaticFilesStorage` não falhe no `collectstatic`.
- `overrides` em `package.json` impedem que dependências transitivas do Flowbite tragam outra versão do Tailwind.
- Para atualizar Flowbite, Tailwind ou a IBM Plex Sans: altere a versão em `package.json`, rode `npm install --ignore-scripts`, ajuste o caminho versionado em `skeleton/web/templates/skeleton/web/base.html`, `skeleton/web/templates/skeleton/portal/base.html` e `tokens.css` (o build recusa referências a outra versão), rode `npm run build` e confira as telas.

O job `assets` do CI executa `npm ci --ignore-scripts` e `npm run check`; ver [testes](testing.md#integração-contínua).
