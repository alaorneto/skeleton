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

Os valores ficam em `frontend/src/tokens.css`: escalas primitivas (`cobalt-*`, `ink-*`) e o mapeamento semântico do Flowbite 4. Para trocar a marca de um projeto derivado, altere só a escala `--color-cobalt-*` e recompile.

### Marca por organização

O administrador escolhe, em `/o/<org>/settings/`, um nome de exibição, uma cor primária e um logo ([#168](https://github.com/alaorneto/skeleton/issues/168)). Nas telas da organização, o nome e o logo substituem "Skeleton" no cabeçalho. A cor substitui só os tokens `--color-brand`, `--color-brand-strong`, `--color-fg-brand` e `--color-fg-brand-strong`, por um `<style>` no `base.html`.

- **Contraste:**
  - A cor precisa de contraste WCAG AA (4,5:1) com o branco, porque os botões levam texto branco sobre ela e os links a usam sobre o fundo claro.
  - No modo escuro, os textos usam uma variante clareada até atingir 4,5:1 sobre o fundo escuro.
  - O cálculo fica em `skeleton/tenancy/branding.py`.
- **CSS:** sai só de valores `#rrggbb` validados (há também um check no banco), nunca de texto livre. Quando houver CSP global, esse `<style>` vai precisar de nonce.
- **Logo:**
  - PNG, JPEG ou WebP de até 512 KB, conferido pelo conteúdo; SVG é recusado.
  - É servido inline só para membros, em `/o/<org>/settings/logo/`, com `Cache-Control: private`, `nosniff` e ETag.
  - Fora das telas da organização (`/me`, login, `/platform/`), o tema do núcleo continua.

### Modo escuro

Segue `prefers-color-scheme`, sem script e sem flash, compatível com CSP.

### Tipografia

- **Inter Variable** (OFL 1.1), auto-hospedada em `static/skeleton/web/vendor/inter-5.3.0/` (subsets latin e latin-ext), `font-display: swap`, recursos `cv05`/`cv08` para distinguir l/I.
- Monoespaçada do sistema (`ui-monospace`, Cascadia Mono, SF Mono…) para UUID, digest e código.
- Escala: H1 28/36 semibold (24/32 no celular), H2 18/28, H3 16/24, corpo 16/24, tabelas e metadados 14/20, rótulos de seção 12–14 em maiúsculas com tracking. Números tabulares nas tabelas.
- Campos usam 16 px no celular (evita zoom do iOS) e 14 px a partir de 640 px.

### Espaçamento, forma e profundidade

- Grade de 4 px. Contêiner de até 1280 px com gutter de 16/24/32 px; telas de detalhe e formulário limitam-se a 1024 px.
- Cabeçalho de página padrão (`.page-header`): breadcrumb ou eyebrow → `h1` → descrição → 32 px (24 px no celular) até o conteúdo; ações à direita no desktop e abaixo no celular.
- Raios: controles 8 px (`rounded-base`), cartões 12 px (`rounded-card`), avatares e pílulas redondos.
- Profundidade por borda e sombra mínima (`shadow-card`); sombra maior (`shadow-pop`) apenas em menus suspensos.

## Navegação

A interface tem três áreas, sempre identificáveis pelo cabeçalho e pelo menu lateral:

| Área | Entrada | Conteúdo |
|---|---|---|
| Organização (`/o/<org>/…`) | seletor de organização no cabeçalho | menu lateral: Início · Aplicativos (apps ativas) · Dados (Tabelas, Auditoria) · Administração, com as seções da capacidade do ator (Pessoas para `members.view`; Grupos e Papéis e permissões para `access.manage`; Aplicativos para `organization_admin`) |
| Administração da plataforma (`/platform/…`) | link "Plataforma" no cabeçalho, só para staff+superuser | menu lateral: Visão geral · Identidade (Usuários, Organizações) · Aplicativos (Aplicativos instalados, Versões e atualizações) · Dados (Catálogo de tabelas, Auditoria global). Views próprias em `skeleton/web/platform`; Versões e atualizações (`/platform/updates/`) segue em etapas: instaladas, novas versões, enviar pacote, preparar plano, operações. |
| Minha conta | menu da conta | Perfil, Email, Senha, Autenticação em dois fatores |

- O seletor lista os vínculos ativos do usuário (`memberships_for_user`); ele só cria links, e cada tela revalida a autorização.
- A administração de acesso é agrupada por tarefa em `skeleton/web/navigation.py` (`ACCESS_SECTIONS`): **Pessoas** (membros, convites, papéis de membros), **Grupos** (grupos, membros de grupos, papéis de grupos) e **Papéis e permissões** (papéis, permissões de tabelas, concessões de registros). Cada seção é um item do menu lateral e seus recursos viram abas na lista.
- Telas de parâmetros de apps (`parameter_panel`) não mostram atalhos para rotas de negócio, conforme o [contrato de administração](application-admin-ui-plan.md).
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
| `breadcrumbs`, `toolbar`, `filter-panel`, `record-tabs` | Navegação, ações, filtros e abas. |
| `link-tabs`, `segmented` | `link-tabs`: abas entre telas irmãs (links com `aria-current="page"`). `segmented` (`components/segmented.html`, com `options` = `[{label, url, current}]` e `label`): alternância de um filtro na mesma tela, uma opção com `aria-current`. Nunca um `badge` como opção ativa ao lado de botões do mesmo grupo (#472). |
| `message`/`alert` (+ `success`, `warning`, `error`), `error-summary`, `confirmation` | Retorno ao usuário. |
| `badge` (+ `badge-brand`, `badge-success`, `badge-warning`, `badge-danger`, `badge-neutral`, `badge-dot`), `uuid`, `meta-list` | Estados e metadados; estado de um registro sempre pelo filtro `status_badge`. |
| `form-stack`, `form-field`, `helptext`, `errorlist`, `form-actions` | Formulários simples; `components/form-fields.html` substitui `form.as_p` mantendo ids, ajuda, erros e `aria-describedby`. Todo campo obrigatório (Django `required`, `marked_required` ou o atributo `data-required` no widget) leva `*` e os demais "(opcional)"; checkbox não leva marca; acima dos campos aparece uma vez "Campos com * são obrigatórios.". `hide_marks=True` no `include` desliga as marcas (login, LDAP, token do setup). O renderer de registros (`skeleton_crud/field.html`) segue a mesma regra, sem "(opcional)" nos campos com a caixa "alterar este campo" (#474). |
| `required`, `optional` | Marcas do rótulo: `*` nos obrigatórios (`show_required`) e "(opcional)" nos demais (`show_optional`), passadas ao `include` de `form-fields.html`. |
| `setup-steps`, `setup-step-number` | Lista numerada das etapas do setup do primeiro acesso (vertical no desktop, linha rolável no celular); `setup-step-done` e `setup-step-later` marcam o estado. |

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
### Texto nos templates

Todo texto visível passa pelo gettext ([#172](https://github.com/alaorneto/skeleton/issues/172)). Isso vale para o conteúdo entre tags, como em `{% translate "Salvar" %}`, e para os atributos visíveis (`alt`, `title`, `placeholder`, `aria-label`). Nos argumentos de tags, use `_("…")`, como em `{% include "…" with label=_("Abrir") %}`. As msgids ficam em pt-BR e o template carrega `{% load i18n %}`. Em frases com variável, prefira `{% blocktranslate trimmed %}` com nomes simples, usando `with` para os atributos. Dados (nomes, emails, valores) nunca entram no gettext.

### Regras que não mudam

- Sem scripts inline e sem CDN: o JS do Flowbite é servido de `static/skeleton/web/vendor/flowbite-4.0.2/`.
- Sem JavaScript tudo funciona: o menu móvel e o menu da conta ficam abertos via `@media (scripting: none)` (variante `noscript:`); as abas do registro voltam a ser seções empilhadas.
- Abas mantêm ARIA e teclado (ArrowLeft/ArrowRight/Home/End) em `site.js`.
- Envio duplo: `site.js` ignora um segundo envio do mesmo formulário POST até a página mudar e marca os botões com `aria-disabled` (nunca `disabled`, que perderia o `name`/`value` do botão clicado); voltar pelo navegador (`pageshow`) reabilita. Um formulário que responde com arquivo na mesma página usa `data-allow-resubmit` (#474).
- Até 600 px o renderer usa uma coluna e não há rolagem horizontal da página; acima disso vale a grade de 12 colunas (`field-span-1..12`), gerada fora do scanner porque vem do manifesto.

## Django admin

Não há tela do Django admin: toda a administração da plataforma usa views próprias em `/platform/`, no mesmo shell, componentes e tokens do site. O Django admin não é usado: `django.contrib.admin` fica instalado só como dependência técnica (modelo `LogEntry` e migrations históricas que concedem privilégios em `django_admin_log`), via `SimpleAdminConfig`, sem autodiscover, site ou rota; `/admin/…` responde 404.

## Build do CSS

```text
frontend/
  package.json, package-lock.json   versões fixas (Tailwind 4.3.3, Flowbite 4.0.2, Inter 5.2.8)
  src/tokens.css                    tokens compartilhados
  src/site.css                      interface SSR (base, componentes, grade do renderer)
  scripts/build.mjs                 build e verificação
skeleton/web/static/skeleton/web/
  site.css                          saída compilada e minificada (versionada)
  vendor/flowbite-4.0.2/            flowbite.min.js + LICENSE.md
  vendor/inter-5.3.0/               woff2 + LICENSE
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
- Para atualizar Flowbite, Tailwind ou Inter: altere a versão em `package.json`, rode `npm install --ignore-scripts`, ajuste o caminho versionado em `skeleton/web/templates/skeleton/web/base.html`, `components/admin-theme.html` e `tokens.css` (o build recusa referências a outra versão), rode `npm run build` e confira as telas.

O job `assets` do CI executa `npm ci --ignore-scripts` e `npm run check`; ver [testes](testing.md#integração-contínua).
