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

### Modo escuro

Segue `prefers-color-scheme`, sem script e sem flash, compatível com CSP. O admin Django pode forçar claro/escuro com `html[data-theme]`; a variante `dark` do Tailwind respeita o mesmo atributo, então admin e site nunca ficam em temas diferentes.

### Tipografia

- **Inter Variable** (OFL 1.1), auto-hospedada em `static/skeleton/web/vendor/inter-5.2.8/` (subsets latin e latin-ext), `font-display: swap`, recursos `cv05`/`cv08` para distinguir l/I.
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
| Organização (`/o/<org>/…`) | seletor de organização no cabeçalho | menu lateral: Início · Aplicativos (apps ativas) · Dados (Tabelas, Auditoria) · Administração, só para `organization_admin` (Pessoas, Grupos, Papéis e permissões, Aplicativos) |
| Administração da plataforma | menu "Plataforma" no cabeçalho, só para superusuários | Usuários e organizações, Catálogo de tabelas, Auditoria global, Aplicativos instalados, Versões e atualizações. Ainda servidas pelo admin Django; serão migradas para views próprias em `/platform/`. |
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
| `btn` + `btn-primary` / `btn-secondary` / `btn-danger` / `btn-ghost`, `btn-sm` | Botões e links com aparência de botão. `.button` continua como alias do primário. |
| `card`, `link-card` (+ `link-card-icon`, `link-card-title`, `link-card-text`) | Superfícies e atalhos navegáveis. |
| `table-scroll` | Tabela responsiva (rola dentro do cartão, nunca a página). `empty-state` para listas vazias. |
| `pagination` | Componente `skeleton/web/pagination.html` (resumo "Mostrando X–Y de N" e Anterior/Próxima). |
| `breadcrumbs`, `toolbar`, `filter-panel`, `record-tabs` | Navegação, ações, filtros e abas. |
| `message`/`alert` (+ `success`, `warning`, `error`), `error-summary`, `confirmation` | Retorno ao usuário. |
| `badge` (+ `badge-brand`, `badge-success`, `badge-warning`, `badge-dot`), `uuid`, `meta-list` | Estados e metadados. |
| `form-stack`, `form-field`, `helptext`, `errorlist`, `form-actions` | Formulários simples; `components/form-fields.html` substitui `form.as_p` mantendo ids, ajuda, erros e `aria-describedby`. |

Ícones: `{% include "skeleton/web/components/icon.html" with name="table" class="size-5" %}` (traço 24 px, estilo Heroicons, decorativos com `aria-hidden`).

Elementos sem classe continuam legíveis: `h1`–`h3`, links, listas, `code`, `pre`, inputs, selects e `button` têm estilo de base. Telas de apps que ainda usam `<h1>`/`<p>` soltos recebem o mesmo ritmo do cabeçalho padrão.

### Regras que não mudam

- Sem scripts inline e sem CDN: o JS do Flowbite é servido de `static/skeleton/web/vendor/flowbite-4.0.2/`.
- Sem JavaScript tudo funciona: o menu móvel e o menu da conta ficam abertos via `@media (scripting: none)` (variante `noscript:`); as abas do registro voltam a ser seções empilhadas.
- Abas mantêm ARIA e teclado (ArrowLeft/ArrowRight/Home/End) em `site.js`.
- Até 600 px o renderer usa uma coluna e não há rolagem horizontal da página; acima disso vale a grade de 12 colunas (`field-span-1..12`), gerada fora do scanner porque vem do manifesto.

## Admin Django e Registros globais

O admin não foi reescrito em Tailwind: ele é a área de configuração, tem templates próprios que mudam a cada versão do Django e já oferece claro/escuro. `admin.css`, gerado pelo mesmo build e carregado depois do CSS do admin:

1. mapeia as variáveis do admin (`--primary`, `--body-bg`, `--link-fg`, `--button-bg`, fontes etc.) para os tokens;
2. ajusta cabeçalho (marca, navegação principal igual à do site, conta), breadcrumbs, módulos, tabelas, botões, campos, mensagens e changelists;
3. nos **Registros globais** (changelists do admin dentro do shell do site), devolve o shell e os blocos `.sk-scope` às camadas do site com `all: revert-layer` e deixa `#content` só com o CSS do admin (`all: revert` de especificidade zero). Isso é necessário porque o CSS do admin não está em camada e venceria o Tailwind.

As telas próprias do skeleton no admin (versões, operações, documentação) não usam mais `<style>` nem `style=` embutidos.

## Build do CSS

```text
frontend/
  package.json, package-lock.json   versões fixas (Tailwind 4.3.3, Flowbite 4.0.2, Inter 5.2.8)
  src/tokens.css                    tokens compartilhados
  src/site.css                      interface SSR (base, componentes, grade do renderer)
  src/admin.css                     tema do admin Django
  scripts/build.mjs                 build e verificação
skeleton/web/static/skeleton/web/
  site.css, admin.css               saídas compiladas e minificadas (versionadas)
  vendor/flowbite-4.0.2/            flowbite.min.js + LICENSE.md
  vendor/inter-5.2.8/               woff2 + LICENSE
  vendor/VENDOR.json                versões, licenças e SHA-256 dos arquivos vendorizados
```

```sh
cd frontend
npm ci --ignore-scripts      # instala exatamente o lockfile, sem scripts de instalação
npm run build                # gera site.css/admin.css e copia os assets vendorizados
npm run check                # falha se static/ estiver desatualizado em relação às fontes
```

Rode `npm run build` sempre que alterar `frontend/src/` **ou templates**: o Tailwind só gera as classes que encontra em `skeleton/web/templates`, `skeleton/web/static/skeleton/web/*.js`, `skeleton/system/app_template/*.html-tpl` e `examples/notes/templates`. Versione o resultado junto com a mudança.

Templates de apps derivadas em `applications/` não são varridos, porque o CSS faz parte do wheel do núcleo. Nesses templates use as classes de componente da tabela acima e utilitários que o núcleo já usa; um utilitário Tailwind inédito não existirá no `site.css`. Estilo próprio de uma app vai num arquivo estático da própria app, carregado pelo bloco `extrahead`, usando as variáveis `--color-*` dos tokens.

- O mapa de fonte do Flowbite não é distribuído; o build remove a referência `sourceMappingURL` para que `ManifestStaticFilesStorage` não falhe no `collectstatic`.
- `overrides` em `package.json` impedem que dependências transitivas do Flowbite tragam outra versão do Tailwind.
- Para atualizar Flowbite, Tailwind ou Inter: altere a versão em `package.json`, rode `npm install --ignore-scripts`, ajuste o caminho versionado em `skeleton/web/templates/skeleton/web/base.html`, `components/admin-theme.html` e `tokens.css` (o build recusa referências a outra versão), rode `npm run build` e confira as telas.

O job `assets` do CI executa `npm ci --ignore-scripts` e `npm run check`; ver [testes](testing.md#integração-contínua).
