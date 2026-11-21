# Marca e temas da instância

O administrador da plataforma define a marca da instância inteira (nome do produto, rodapé, logo, logo para o modo escuro e ícone) e escolhe ou cria o tema ativo (cor, cantos, fonte e CSS personalizado), em `/platform/appearance/`. A mudança vale para todas as páginas sem reinício nem build do CSS. A marca de uma organização ([#168](https://github.com/alaorneto/skeleton/issues/168), [endereços de organização](organization-sites.md)) continua valendo por cima nas telas e nos e-mails dela. O desenho é o do epic [#591](https://github.com/alaorneto/skeleton/issues/591).

## Precedência

| O quê | Organização (telas e e-mails dela) | Instância | Núcleo |
|---|---|---|---|
| Nome no título, no cabeçalho e no rodapé | nome de exibição da organização; no endereço próprio, o nome publicado | `product_name` | "Skeleton" |
| Cor | `brand_color` da organização (`<style nonce>`, depois do tema) | cor do tema ativo | escala "Cobalto e Ardósia" |
| Logo do cabeçalho | logo da organização | logo da instância (com a variante escura) | ícone do núcleo |
| Favicon | ícone da organização | ícone da instância | `favicon.svg` |
| Cantos, fonte e CSS personalizado | — (organizações não escolhem tema) | tema ativo | tokens do núcleo |

A folha de estilo do tema entra depois do `site.css` e antes do `<style>` da organização, então a organização vence pela ordem. Uma organização sem marca própria herda nome, logo e tema da instância. Fora do escopo: página de manutenção e setup (renderizadas antes do banco, continuam com o tema do núcleo), forçar modo claro ou escuro, fontes enviadas, logo em SVG, tema por organização e manifesto PWA do domínio principal.

## Modelo

O pacote `skeleton/appearance` (label `sk_appearance`) tem só dados, regras puras e serviços; as páginas ficam em `skeleton/web`. As duas tabelas são `SystemRecord` globais, com revisão/CAS, auditoria por trigger e infraestrutura congelada em `sk_appearance.0001_initial`:

- `sk_appearance.instancetheme`: um tema do catálogo (`name` único entre os vivos, `brand_color` vazio ou `#rrggbb`, `radius` em `sharp|default|round`, `font` em `inter|system|serif`, `custom_css` até 32 KiB). O CSS personalizado **não** é sensível: a auditoria mostra quem mudou o quê.
- `sk_appearance.instancebrand`: a linha única (`slot = "instance"`) com `product_name` (vazio mostra "Skeleton"), `footer_text`, as chaves `logo_key`, `logo_dark_key` e `icon_key` (sensíveis, redigidas na auditoria) e o tema ativo: `builtin_theme` (um tema pronto) **ou** `active_theme` (um tema do catálogo), nunca os dois; os dois vazios são o tema do núcleo.

Marca e tema não vão para o SQLite da instância: aquilo é configuração lida antes do Django e só vale depois do reinício (E025); estes são dados editados pela interface.

## Quem altera

Só o administrador da plataforma, pelos serviços de `skeleton.appearance.services` e `skeleton.appearance.images` (todos keyword-only, com `actor`, dentro de `transaction.atomic()`): `update_brand`, `create_theme`, `update_theme`, `delete_theme`, `activate_theme`, `replace_image` e `remove_image`. Toda alteração leva `expected_revision` (a revisão da linha; 0 quando a linha da marca ainda não existe) e uma revisão diferente levanta `ConcurrentUpdate`; dados iguais não gravam nada. Campos fora da lista fechada são recusados ("Campo não permitido."). Qualquer outro usuário recebe `PermissionDenied`. `actor=None` é só do console (`manage.py appearance`), que abre o próprio `system_context` com motivo.

## Leitura a cada requisição e cache

`skeleton.appearance.snapshot.current_appearance()` devolve um `Appearance` imutável (nome, rodapé, versões das imagens, CSS do tema com e sem o CSS livre, as versões desses CSS e a cor). Ele lê o banco no máximo a cada 5 s por processo; se a consulta falhar, mantém o último valor bom por 5 min e depois cai na aparência do núcleo. Toda escrita chama `reset_appearance_cache()` depois do commit, então o processo que grava vê a mudança na hora e os outros em até 5 s. `cached_appearance()` nunca consulta o banco: é o que as páginas de erro usam (a 500 renderiza com o banco fora). Nos testes, um fixture `autouse` fixa a aparência padrão (`pin_appearance`), para que os orçamentos de consultas não mudem; os testes deste recurso pedem `live_appearance`.

## Temas

`skeleton/appearance/themes.py` é Python puro. A cor escolhida é o passo 600 da marca e precisa de contraste AA (4,5:1) com o branco, a mesma regra da cor da organização. Dela sai a escala `--color-cobalt-50…950` inteira, por mistura com branco e preto em sRGB: o `site.css` compilado lê a marca dessa escala (`--color-brand: var(--color-cobalt-600)`), então trocar a escala troca botões, links, foco, fundos suaves e o gradiente do login de uma vez. No modo escuro os fundos da marca e os textos (`--color-fg-brand`, `--color-fg-brand-strong`, `--color-focus`) são emitidos explicitamente, clareados até AA sobre o fundo escuro. Os cantos `sharp` e `round` trocam `--radius-base` e `--radius-card`; as fontes `system` e `serif` trocam `--font-sans` por pilhas do sistema (nenhuma fonte externa). A parte gerada só tem valores `#rrggbb` e esses literais fixos; o nome do tema nunca entra no CSS.

Temas prontos, em código e sem CSS livre: `cobalto` (o do núcleo, não emite nada), `esmeralda` (`#047857`), `grafite` (`#374151`), `vinho` (`#9f1239`) e `violeta` (`#6d28d9`). O catálogo de temas personalizados tem no máximo 50 temas vivos. Excluir é exclusão lógica (o nome fica livre) e o tema ativo não pode ser excluído. Um tema ativo excluído por fora, ou um slug desconhecido, vale como o tema do núcleo, nunca como erro.

## CSS personalizado

`skeleton/appearance/css_validation.py` é a barreira do texto livre, em defesa em profundidade: a CSP (`default-src 'self'`, `img-src 'self' data:`, `font-src 'self'`), que não muda, já impede carregar qualquer recurso externo, e o CSS nunca é embutido no HTML (sai só como `text/css` de `/brand/theme.css`). A validação recusa, nesta ordem: o que não é texto; mais de 32 768 bytes em UTF-8; `<` e caracteres nulos (nada de `</style>` nem marcação); strings e comentários sem fim; chaves e parênteses sem par fora de strings; e, numa forma normalizada só para a checagem (comentários removidos pelo mesmo leitor que conhece as strings, escapes CSS decodificados, minúsculas), `@import`, `@charset`, `@namespace`, `url(`, `image-set(`, `image(`, `src(`, `expression(`, `javascript:`, `behavior` e `-moz-binding`. Assim `u\72l(`, `\75 rl(` e `@im/**/port` também são recusados. O texto gravado é o original.

A mensagem de erro diz a linha ("Linha 3: o CSS não pode usar url(."). Duas regras costumam pegar autores: `<` é recusado **também dentro de comentários** (`/* <style> */` não passa), e `url(` é recusado em qualquer lugar, porque fontes e imagens de um tema vêm do [pacote de tema](#pacote-de-tema), nunca do CSS. A checagem em si é `css_problem(texto)`, que não importa Django e devolve `CssProblem(code, line, item)` (códigos `not_text`, `too_large`, `markup`, `open_comment`, `open_string`, `unbalanced`, `forbidden`); `validate_custom_css` é o invólucro com `ValidationError` usado pelos serviços e pelas telas (#631). A linha é contada na forma normalizada: um escape hexadecimal seguido de quebra de linha consome a quebra e, nesse caso raro, a linha sai uma a menos.

O contrato estável para o CSS livre são os tokens `--color-*`, `--radius-*`, `--font-*` e as classes de componente listadas em [UI](ui-design.md#marca-e-tema-da-instância).

## Imagens

`skeleton/appearance/images.py` guarda o logo e o logo para o modo escuro (PNG, JPEG ou WebP até 512 KB) e o ícone (PNG quadrado de 512 × 512 até 256 KB), com os mesmos verificadores das imagens de organização (`checked_logo` e `checked_icon` de `skeleton.tenancy.branding`): o conteúdo decide o tipo, nunca o nome. SVG e GIF são recusados. A chave é `platform/branding/<uuid4>`, gerada pelo servidor, no armazenamento privado ([arquivos](files.md)). Os bytes são gravados antes da linha; falhar a linha apaga o objeto na hora, o objeto substituído sai depois do commit, e um rollback do chamador deixa um órfão que o job global `appearance.orphans` remove depois da carência ([jobs](jobs.md)). A variante escura só aparece junto com o logo.

### Logos em SVG

Só dentro de um [pacote de tema](#pacote-de-tema) o logo e o logo para o modo escuro podem ser SVG; o envio avulso desta tela continua PNG, JPEG ou WebP, e o ícone é sempre PNG. `skeleton/appearance/svg_validation.py` (`svg_problem(conteúdo)`, Python puro, #632) aceita um subconjunto estático e recusa o resto, sem reescrever o arquivo:

- até 256 KiB, UTF-8 (com ou sem BOM), raiz `svg` no namespace SVG com `viewBox` ou `width` e `height`, até 32 níveis e 5 000 elementos;
- elementos permitidos: `svg`, `g`, `defs`, `title`, `desc`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `text`, `tspan`, `linearGradient`, `radialGradient`, `stop`, `clipPath`, `mask`, `pattern`, `symbol` e `use`. Qualquer outro é recusado: `script`, `style`, `foreignObject`, `image`, `a`, animações (`animate*`, `set`), filtros e elementos de editores (`sodipodi:*`, `inkscape:*`);
- recusados antes do parse: `<!DOCTYPE`, `<!ENTITY`, `<![CDATA[` e instruções de processamento além da declaração `<?xml …?>` inicial (sem DOCTYPE não há entidade nem bomba de expansão);
- atributos `on*` recusados; `href`/`xlink:href` só em `use`, gradientes e `pattern`, e só `#id`; nenhum valor com `javascript:`, `data:`, `vbscript:` ou `url(` que não seja `url(#id)`; `style` sem `@`, `expression`, `\` nem `<`; atributos só sem namespace, `xlink:href`, `xml:space` e `xml:lang`;
- texto só em `title`, `desc`, `text` e `tspan`.

O problema (`SvgProblem(code, item)`) cita o elemento ou o atributo, nunca o valor. Exporte o logo do editor como "SVG simples" ou "SVG otimizado". O SVG só aparece em `<img>` e é servido com `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` e `nosniff`.

## Rotas públicas

| Rota | Resposta |
|---|---|
| `GET /brand/theme.css` | CSS do tema ativo, `text/css; charset=utf-8`, `nosniff`, `ETag` com a versão servida; `Cache-Control: public, max-age=300`, ou `public, max-age=31536000, immutable` quando `?v=` é a versão atual; `If-None-Match` igual dá 304; sem tema, 200 com corpo vazio |
| `GET /brand/theme.css?safe=1` | Só os tokens, sem o CSS personalizado (as páginas de aparência usam esta) |
| `GET /brand/logo/`, `/brand/logo-dark/`, `/brand/icon/` | Bytes da chave da linha única, tipo conferido de novo pelo conteúdo, `nosniff`, CSP `sandbox`, `public, max-age=300`; 404 quando não existe |

Sem autenticação, só GET/HEAD, sem parâmetro que escolha arquivo. `/brand/` é liberado nos endereços próprios das organizações (eles também carregam o tema da instância) e é raiz reservada: uma app não pode declarar rota em `brand/`, como em `platform/` e `admin/`. Com o cache quente, a folha de estilo não consulta o banco.

## Tela de administração

`/platform/appearance/` ("Aparência", grupo Operação do menu da plataforma, só para o administrador completo; papéis limitados recebem 403):

1. **Marca:** nome do produto (até 60) e texto do rodapé (até 160, uma linha).
2. **Imagens:** enviar ou remover o logo, o logo para o modo escuro e o ícone.
3. **Temas:** o do núcleo, os prontos e os personalizados, com o ativo marcado; para cada um, "Pré-visualizar", "Ativar", "Editar" e "Excluir" (este com confirmação; o ativo não pode ser excluído). "Novo tema" pede nome, cor (`#RRGGBB`), cantos, fonte e CSS personalizado.
4. **Pré-visualização:** `…/themes/<id>/preview/` e `…/builtin/<slug>/preview/` mostram uma galeria de componentes (botões, selos, mensagens, tabela, formulário, abas e item de menu ativo) com o CSS completo do tema candidato, servido por `…/preview.css` só para o administrador e com `Cache-Control: private, no-store`. Nada é ativado.

Toda escrita exige CSRF e autenticação recente, recusa campo POST desconhecido (400), responde 409 a uma revisão antiga e registra um `security_event` sem valores (`appearance.brand_changed`, `appearance.image_changed`, `appearance.theme_changed`, `appearance.theme_activated`). A tela funciona sem JavaScript.

### Saída de emergência

As páginas de `/platform/appearance/` (fora a pré-visualização) carregam o tema em modo seguro (`safe=1`): um CSS personalizado que esconda a interface não impede o administrador de editar ou trocar o tema. Se a pré-visualização esconder a página, volte pela URL da lista. Sem acesso à tela, no servidor:

```sh
python manage.py appearance show         # nome, rodapé, tema ativo e quais imagens existem (nunca as chaves)
python manage.py appearance reset-theme  # volta ao tema do núcleo, auditado com o motivo appearance.reset_theme
```

## Página inicial pública

O administrador da plataforma publica em `/platform/appearance/home/` ([#622](https://github.com/alaorneto/skeleton/issues/622)) uma página inicial em [blocos estruturados](ui-design.md#páginas-em-blocos) (`hero`, `text`, `contact`, `links`): texto simples, sempre escapado, sem HTML nem Markdown. Quem abre `/` do host da plataforma sem login a vê, com a marca e o tema da instância e, no `hero`, o botão "Entrar"; sem página publicada (ou despublicada), `/` continua levando ao login (`redirect_to_login`, `next=/`), como antes. Quem entrou continua vendo "Minhas organizações". `/` responde com `never_cache`. No endereço de uma organização, `/` é a rota da organização e nunca mostra esta página.

- **Modelo.** `sk_appearance.instancepage` (`InstancePage`, `SystemRecord` global): `slot` único com check `slot = 'home'`, `document` (`JSONField`) e `published`. Infraestrutura congelada em `sk_appearance.0002_instance_page`; sem gateway nem `write_fields`.
- **Serviço.** `skeleton/appearance/pages.py`: `home_page()` é o que `/` lê, com o mesmo cache por processo do snapshot (5 s; o último valor bom por 5 min quando o banco falha; modo setup e falha sem valor bom respondem "sem página"); `save_home_page(*, actor, document, published, expected_revision)` é a única escrita: só o administrador completo da plataforma, CAS na revisão da linha (0 antes de existir), `validate_page(kind="platform_home")` (uma página publicada precisa de ao menos um bloco), dados iguais não gravam nada, o cache é zerado depois do commit e o `security_event` `appearance.home_page_changed` leva só a situação e o número de blocos, nunca o conteúdo. `current_home_page()` lê a linha sem cache (o editor precisa da revisão atual). Nos testes, um fixture `autouse` fixa "sem página" (`pin_home_page`); os testes do recurso pedem `live_home_page`.
- **Editor.** O [editor SSR compartilhado](ui-design.md#páginas-em-blocos), com as mesmas regras das demais telas de aparência: só o administrador completo (`platform_page`), autenticação recente nas escritas, CSRF e revisão (409 com revisão antiga), 400 com o `error-summary` para erro de validação. A pré-visualização (`…/home/preview/`) usa o mesmo template da página real. A seção "Página inicial pública" de `/platform/appearance/` mostra a situação e leva ao editor.

A página é pública por definição: não lê nada de organização nem de pessoa. Para desfazer, despublique: `/` volta ao login.

## E-mails

Sem organização no contexto, os e-mails HTML (`skeleton/email/base.html`) levam o nome do produto, a cor do tema ativo e o logo da instância como parte inline (`cid:organization-logo`, nunca URL). Com organização, valem a marca dela; se ela não tem cor, vale a do tema da instância, e o logo da instância nunca entra num e-mail de organização. Os e-mails de conta do allauth (texto) saudam com o nome do produto no domínio principal ([notificações](notifications.md#marca-nos-e-mails)).
