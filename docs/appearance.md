# Marca e temas da instância

O administrador da plataforma define a marca da instância inteira (nome do produto, rodapé, logo, logo para o modo escuro e ícone) e escolhe ou cria o tema ativo (cor, cantos, fonte e CSS personalizado), em `/platform/appearance/`. A mudança vale para todas as páginas sem reinício nem build do CSS. A marca de uma organização ([#168](https://github.com/alaorneto/skeleton/issues/168), [endereços de organização](organization-sites.md)) continua valendo por cima nas telas e nos e-mails dela. O desenho é o do epic [#591](https://github.com/alaorneto/skeleton/issues/591). Para **criar e publicar um tema** a partir de um sistema de design, siga o guia [criar e publicar um tema](themes.md); esta página é a referência.

## Precedência

| O quê | Organização (telas e e-mails dela) | Marca da instância | Tema ativo (pacote) | Núcleo |
|---|---|---|---|---|
| Nome no título, no cabeçalho e no rodapé | nome de exibição da organização; no endereço próprio, o nome publicado | `product_name` | `brand.product_name` do pacote | "Skeleton" |
| Rodapé | — | `footer_text` | `brand.footer_text` do pacote | a frase do núcleo |
| Cor | `brand_color` da organização (`<style nonce>`, depois do tema) | — | cor do tema ativo | escala "Cobalto e Ardósia" |
| Logo do cabeçalho | logo da organização | logo enviado (com a variante escura enviada) | logo do pacote (com a variante escura do pacote) | ícone do núcleo |
| Favicon | ícone da organização | ícone enviado | ícone do pacote | `favicon.svg` |
| Cantos, fonte, fontes e CSS personalizado | — (organizações não escolhem tema) | — | tema ativo | tokens do núcleo |

Por campo, vale o primeiro valor não vazio da esquerda para a direita, a partir da coluna da instância (a da organização só vale nas telas e nos e-mails dela). O logo escuro vem sempre da mesma origem do logo: com um logo enviado e sem variante escura enviada, não há variante escura, nunca a do pacote (dois logos diferentes não se misturam). A folha de estilo do tema entra depois do `site.css` e antes do `<style>` da organização, então a organização vence pela ordem. Uma organização sem marca própria herda nome, logo e tema da instância. Fora do escopo: página de manutenção e setup (renderizadas antes do banco, continuam com o tema do núcleo) e tema por organização. O modo claro ou escuro escolhido pela pessoa vale por cima de tudo isso, sem mudar a marca ([modo escuro](ui-design.md#modo-escuro), #665).

## Modelo

O pacote `skeleton/appearance` (label `sk_appearance`) tem só dados, regras puras e serviços; as páginas ficam em `skeleton/web`. As duas tabelas são `SystemRecord` globais, com revisão/CAS, auditoria por trigger e infraestrutura congelada em `sk_appearance.0001_initial`:

- `sk_appearance.instancetheme`: um tema do catálogo (`name` único entre os vivos, `brand_color` vazio ou `#rrggbb`, `radius` em `sharp|default|round`, `font` em `default|system|serif` (temas gravados antes do #690 podem guardar `inter`, lido como `default`), `custom_css` até 32 KiB). O CSS personalizado **não** é sensível: a auditoria mostra quem mudou o quê. A origem fica em `source`: `form` (criado na tela) ou `package` (instalado de um [pacote de tema](#pacote-de-tema), #635). Um tema de pacote guarda também `package_id` (vivo uma vez só), `package_version`, `package_digest` (o `content_digest`), `package_manifest` (o `theme.json` validado), `font_family`, `product_name`, `footer_text` e `package_files`, o mapa das chaves de armazenamento das imagens e fontes (**sensível**, redigido na auditoria). Um tema `form` não tem nenhum desses campos (constraint), e a infraestrutura foi reinstalada com eles em `sk_appearance.0003_theme_packages`.
- `sk_appearance.instancebrand`: a linha única (`slot = "instance"`) com `product_name` (vazio mostra "Skeleton"), `footer_text`, as chaves `logo_key`, `logo_dark_key` e `icon_key` (sensíveis, redigidas na auditoria) e o tema ativo: `builtin_theme` (um tema pronto), `active_theme` (um tema do catálogo) **ou** `app_theme` (`<app_label>:<slug>`, um tema de app, #643; coluna e constraint em `sk_appearance.0004_app_theme`), nunca dois; todos vazios são o tema do núcleo.

Marca e tema não vão para o SQLite da instância: aquilo é configuração lida antes do Django e só vale depois do reinício (E025); estes são dados editados pela interface.

## Quem altera

Só o administrador da plataforma, pelos serviços de `skeleton.appearance.services` e `skeleton.appearance.images` (todos keyword-only, com `actor`, dentro de `transaction.atomic()`): `update_brand`, `create_theme`, `update_theme`, `delete_theme`, `activate_theme`, `replace_image` e `remove_image`. Toda alteração leva `expected_revision` (a revisão da linha; 0 quando a linha da marca ainda não existe) e uma revisão diferente levanta `ConcurrentUpdate`; dados iguais não gravam nada. Campos fora da lista fechada são recusados ("Campo não permitido."). Qualquer outro usuário recebe `PermissionDenied`. `actor=None` é só do console (`manage.py appearance`), que abre o próprio `system_context` com motivo.

**Pacotes** (`skeleton.appearance.packages.install_theme_package(actor=..., data=bytes)`, #635): o zip é lido inteiro pelo leitor antes de qualquer gravação; depois vêm o contraste AA da cor e a faixa do núcleo. Cada imagem e cada fonte vai para o armazenamento privado com chave própria `platform/branding/<uuid4>` e o tema é gravado com `source="package"`. O resultado (`InstallResult`) diz `installed`, `updated` (com a versão anterior) ou `unchanged`:

| Situação | Resultado |
|---|---|
| `id` novo | instala (até 50 temas no catálogo; nome único entre os vivos) |
| mesmo `id`, mesmo `content_digest` | `unchanged`, nada grava |
| mesmo `id`, versão maior | `updated`: a mesma linha (mesmo `sysid`, revisão + 1); se é o tema ativo, continua ativo |
| mesmo `id`, mesma versão, outro conteúdo | recusado: `same_version_changed` |
| mesmo `id`, versão menor | recusado: `downgrade` |

Instalar nunca ativa. Um tema de pacote é somente leitura para o formulário (`update_theme` recusa com `package_read_only`); remover é `delete_theme`, igual ao tema de formulário (exclusão lógica, nunca o ativo). As linhas de instalação e ativação se serializam pela linha da marca, e duas instalações paralelas do mesmo pacote novo têm um vencedor (a outra recebe `ConcurrentUpdate`).

## Leitura a cada requisição e cache

`skeleton.appearance.snapshot.current_appearance()` devolve um `Appearance` imutável (nome, rodapé, versões das imagens, CSS do tema com e sem o CSS livre, as versões desses CSS e a cor). Ele lê o banco no máximo a cada 5 s por processo; se a consulta falhar, mantém o último valor bom por 5 min e depois cai na aparência do núcleo. Toda escrita chama `reset_appearance_cache()` depois do commit, então o processo que grava vê a mudança na hora e os outros em até 5 s. `cached_appearance()` nunca consulta o banco: é o que as páginas de erro usam (a 500 renderiza com o banco fora). Nos testes, um fixture `autouse` fixa a aparência padrão (`pin_appearance`), para que os orçamentos de consultas não mudem; os testes deste recurso pedem `live_appearance`.

## Temas

`skeleton/appearance/themes.py` é Python puro. A cor escolhida é o passo 600 da marca e precisa de contraste AA (4,5:1) com o branco, a mesma regra da cor da organização. Dela sai a escala `--color-cobalt-50…950` inteira, por mistura com branco e preto em sRGB: o `site.css` compilado lê a marca dessa escala (`--color-brand: var(--color-cobalt-600)`), então trocar a escala troca botões, links, foco, fundos suaves e o gradiente do login de uma vez. No modo escuro os fundos da marca e os textos (`--color-fg-brand`, `--color-fg-brand-strong`, `--color-focus`) são emitidos explicitamente, clareados até AA sobre o fundo escuro. Os cantos `sharp` e `round` trocam `--radius-base` e `--radius-card`; as fontes `system` e `serif` trocam `--font-sans` por pilhas do sistema (nenhuma fonte externa). A parte gerada só tem valores `#rrggbb` e esses literais fixos; o nome do tema nunca entra no CSS.

Temas prontos, em código e sem CSS livre: `cobalto` (o do núcleo, não emite nada), `esmeralda` (`#047857`), `grafite` (`#374151`), `vinho` (`#9f1239`) e `violeta` (`#6d28d9`). O catálogo de temas personalizados tem no máximo 50 temas vivos. Excluir é exclusão lógica (o nome fica livre) e o tema ativo não pode ser excluído. Um tema ativo excluído por fora, ou um slug desconhecido, vale como o tema do núcleo, nunca como erro.

## CSS personalizado

`skeleton/appearance/css_validation.py` é a barreira do texto livre, em defesa em profundidade: a CSP (`default-src 'self'`, `img-src 'self' data:`, `font-src 'self'`), que não muda, já impede carregar qualquer recurso externo, e o CSS nunca é embutido no HTML (sai só como `text/css` de `/brand/theme.css`). A validação recusa, nesta ordem: o que não é texto; mais de 32 768 bytes em UTF-8; `<` e caracteres nulos (nada de `</style>` nem marcação); strings e comentários sem fim; chaves e parênteses sem par fora de strings; e, numa forma normalizada só para a checagem (comentários removidos pelo mesmo leitor que conhece as strings, escapes CSS decodificados, minúsculas), `@import`, `@charset`, `@namespace`, `url(`, `image-set(`, `image(`, `src(`, `expression(`, `javascript:`, `behavior` e `-moz-binding`. Assim `u\72l(`, `\75 rl(` e `@im/**/port` também são recusados. O texto gravado é o original.

A mensagem de erro diz a linha ("Linha 3: o CSS não pode usar url(."). Duas regras costumam pegar autores: `<` é recusado **também dentro de comentários** (`/* <style> */` não passa), e `url(` é recusado em qualquer lugar, porque fontes e imagens de um tema vêm do [pacote de tema](#pacote-de-tema), nunca do CSS. A checagem em si é `css_problem(texto)`, que não importa Django e devolve `CssProblem(code, line, item)` (códigos `not_text`, `too_large`, `markup`, `open_comment`, `open_string`, `unbalanced`, `forbidden`); `validate_custom_css` é o invólucro com `ValidationError` usado pelos serviços e pelas telas (#631). A linha é contada na forma normalizada: um escape hexadecimal seguido de quebra de linha consome a quebra e, nesse caso raro, a linha sai uma a menos.

O contrato estável para o CSS livre são os tokens `--color-*`, `--radius-*`, `--font-*` e as classes de componente listadas em [UI](ui-design.md#marca-e-tema-da-instância).

## Temas trazidos por apps

Uma app pode trazer temas da instância no próprio pacote, declarados em `runtime.instance_themes` do manifesto ([extensão](extending.md#manifest-ativação-e-superfícies-próprias), epic [#630](https://github.com/alaorneto/skeleton/issues/630)), no mesmo formato do [pacote de tema](#pacote-de-tema), como diretório dentro da app. O build e o doctor da app validam cada um com o leitor; na carga, um tema inválido é `skeleton.E047` e fica fora do catálogo. O passo a passo de autor está em [publicar junto de uma app](themes.md#publicar-junto-de-uma-app).

Um tema de app **não é copiado para o banco** (#643): ele existe enquanto o código da app está instalado, como os temas prontos existem enquanto o código do núcleo está. `skeleton.appearance.app_themes.app_themes()` monta, uma vez por processo e sem consultar o banco, o registro dos temas válidos das apps carregadas, guardando só os metadados (o CSS, os nomes e os SHA-256 dos arquivos, nunca os bytes nem o caminho fora do registro). Ativar grava só a chave `InstanceBrand.app_theme = "<app_label>:<slug>"` (`activate_theme(..., app_theme=...)`, um tipo de tema por vez, também por constraint). As páginas usam o tema de app como um pacote: tokens, fontes, CSS, imagens, nome e rodapé, com a marca da instância vencendo onde estiver definida; os arquivos saem pelas mesmas rotas de `/brand/`, lidos só entre os arquivos declarados do diretório da app, com o caminho resolvido preso a ele e o SHA-256 conferido.

Desinstalar a app (o agente troca o código e reinicia os processos) tira o tema do catálogo sem nenhuma sincronização. Uma chave `app_theme` de uma app que não está mais instalada vale como o tema do núcleo, nunca como erro (`manage.py appearance show` diz "da app X (não instalada; vale o tema do núcleo)"); reinstalar a app devolve o tema. Um zip enviado com o `id` de um tema de app, ou com o prefixo `<app_id>.theme.` de uma app instalada que traz temas, é recusado (`id_belongs_to_app`): um tema de app é atualizado com a própria app.

## Imagens

`skeleton/appearance/images.py` guarda o logo e o logo para o modo escuro (PNG, JPEG ou WebP até 512 KB) e o ícone (PNG quadrado de 512 × 512 até 256 KB), com os mesmos verificadores das imagens de organização (`checked_logo` e `checked_icon` de `skeleton.tenancy.branding`): o conteúdo decide o tipo, nunca o nome. SVG e GIF são recusados. A chave é `platform/branding/<uuid4>`, gerada pelo servidor, no armazenamento privado ([arquivos](files.md)). Os bytes são gravados antes da linha; falhar a linha apaga o objeto na hora, o objeto substituído sai depois do commit, e um rollback do chamador deixa um órfão que o job global `appearance.orphans` remove depois da carência ([jobs](jobs.md)). A variante escura só aparece junto com o logo. Os arquivos de um pacote de tema usam as mesmas chaves; o job considera referenciadas as de `package_files` dos temas vivos, então os arquivos de uma versão substituída ou de um pacote removido saem pelo job, depois da carência, e nunca dentro da transação.

### Logos em SVG

Só dentro de um [pacote de tema](#pacote-de-tema) o logo e o logo para o modo escuro podem ser SVG; o envio avulso desta tela continua PNG, JPEG ou WebP, e o ícone é sempre PNG. `skeleton/appearance/svg_validation.py` (`svg_problem(conteúdo)`, Python puro, #632) aceita um subconjunto estático e recusa o resto, sem reescrever o arquivo:

- até 256 KiB, UTF-8 (com ou sem BOM), raiz `svg` no namespace SVG com `viewBox` ou `width` e `height`, até 32 níveis e 5 000 elementos;
- elementos permitidos: `svg`, `g`, `defs`, `title`, `desc`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `text`, `tspan`, `linearGradient`, `radialGradient`, `stop`, `clipPath`, `mask`, `pattern`, `symbol` e `use`. Qualquer outro é recusado: `script`, `style`, `foreignObject`, `image`, `a`, animações (`animate*`, `set`), filtros e elementos de editores (`sodipodi:*`, `inkscape:*`);
- recusados antes do parse: `<!DOCTYPE`, `<!ENTITY`, `<![CDATA[` e instruções de processamento além da declaração `<?xml …?>` inicial (sem DOCTYPE não há entidade nem bomba de expansão);
- atributos `on*` recusados; `href`/`xlink:href` só em `use`, gradientes e `pattern`, e só `#id`; nenhum valor com `javascript:`, `data:`, `vbscript:` ou `url(` que não seja `url(#id)`; `style` sem `@`, `expression`, `\` nem `<`; atributos só sem namespace, `xlink:href`, `xml:space` e `xml:lang`;
- texto só em `title`, `desc`, `text` e `tspan`.

O problema (`SvgProblem(code, item)`) cita o elemento ou o atributo, nunca o valor.

## Pacote de tema

O passo a passo de autor está em [criar e publicar um tema](themes.md). Um tema pode ser entregue como **pacote**: um `.zip` (ou um diretório, dentro de uma app) num formato padronizado, que a instância valida e instala (epic [#629](https://github.com/alaorneto/skeleton/issues/629)). O leitor é `skeleton/appearance/package_format.py` (#633): `read_theme_archive(bytes)` e `read_theme_directory(caminho)` devolvem um `ThemePackage` imutável ou levantam `ThemePackageError(code, path, line, item)`. Ele não importa Django (roda também no build de apps) e nada grava: os bytes ficam em memória e nenhum nome de arquivo vira caminho de disco.

Arquivos **na raiz** do zip, sem pasta de topo:

```text
theme.json                                obrigatório: o manifesto
theme.css                                 opcional: CSS personalizado (o nome vem de "css")
images/logo.(png|jpg|jpeg|webp|svg)       opcional
images/logo-dark.(png|jpg|jpeg|webp|svg)  opcional, só junto com o logo
images/icon.png                           opcional, PNG 512 × 512
fonts/<nome>.woff2                        opcional, até 8
README.md                                 opcional, ignorado
LICENSES/<nome>.txt                       opcional, ignorado (licenças das fontes)
```

Os nomes dentro de `images/` e `fonts/` são livres (os de cima são a convenção): o que manda é o `theme.json`. Qualquer outro arquivo é erro `unexpected_file`, para um arquivo esquecido no zip não passar despercebido. Caminhos só com segmentos `[a-z0-9][a-z0-9._-]*` (as exceções de caixa são `README.md` e `LICENSES/`).

`theme.json` é um esquema fechado (chave desconhecida em qualquer nível é `unknown_field`):

```json
{
  "format": 1,
  "id": "org.example.theme.aurora",
  "version": "1.0.0",
  "name": "Aurora",
  "description": "Tema da marca Aurora.",
  "publisher": "org.example",
  "compatibility": {"skeleton": ">=0.3.0,<0.4.0"},
  "tokens": {"brand_color": "#1d4ed8", "radius": "default", "font_family": "Aurora Sans"},
  "brand": {"product_name": "Aurora", "footer_text": "Aurora · atendimento ao cliente"},
  "images": {"logo": "images/logo.svg", "logo_dark": "images/logo-dark.svg", "icon": "images/icon.png"},
  "fonts": [{"family": "Aurora Sans", "file": "fonts/aurora-sans.woff2", "weight": "100 900", "style": "normal"}],
  "css": "theme.css"
}
```

| Campo | Regra | Código |
|---|---|---|
| `format` | obrigatório, inteiro `1` | `format` |
| `id` | obrigatório; `[a-z][a-z0-9._-]{1,127}` (a regra dos ids de componente) | `id` |
| `version` | obrigatório; versão semântica | `version` |
| `name` | obrigatório; 1–80, uma linha | `name` |
| `description` | até 300, uma linha | `text_field` |
| `publisher` | mesma regra de `id` | `publisher` |
| `compatibility.skeleton` | obrigatório; faixa de versões; na instalação, precisa conter a versão do núcleo instalado | `compatibility`, `incompatible_core` |
| `tokens.brand_color` | `""` ou `#rrggbb`; na instalação, contraste AA com o branco | `brand_color` |
| `tokens.radius` | `sharp`, `default` (padrão) ou `round` | `radius` |
| `tokens.font` | `default` (padrão: a fonte do núcleo, IBM Plex Sans), `system` ou `serif`. `default` vale a partir do núcleo 0.5.0; para núcleos anteriores, omita o campo ou use o nome antigo `inter`, que continua aceito e é lido como `default` | `font` |
| `tokens.font_family` | `""` ou o `family` de uma entrada de `fonts` (vence `font`) | `font_family` |
| `brand.product_name`, `brand.footer_text` | até 60 e 160, uma linha | `text_field` |
| `images.logo`, `images.logo_dark` | em `images/`; PNG, JPEG ou WebP (provado pelo conteúdo) até 512 KB, ou [SVG validado](#logos-em-svg) até 256 KiB; o escuro exige o logo | `image_path`, `image_type`, `image_size`, `svg`, `logo_dark_without_logo` |
| `images.icon` | em `images/`; PNG 512 × 512 até 256 KiB | `image_path`, `image_size`, `icon` |
| `fonts` | até 8: `family` (`[A-Za-z][A-Za-z0-9 -]{0,62}[A-Za-z0-9]`, nunca um nome genérico do CSS; a família do núcleo, `IBM Plex Sans Variable`, é aceita e substitui a do núcleo no tema), `file` (`fonts/*.woff2`, começa com `wOF2`, até 512 KiB), `weight` (`"400"`, padrão, ou faixa `"100 900"`), `style` (`normal` ou `italic`) | `too_many_fonts`, `font_family_name`, `font_file`, `font_weight`, `font_style` |
| `css` | padrão `theme.css`, na raiz; declarado e ausente é erro; mesmas regras do [CSS personalizado](#css-personalizado) | `css_missing`, `css` (com a linha) |

Limites do zip: até 4 MiB, 6 MiB descomprimido, 40 membros, razão de expansão até 100 por membro, `theme.json` até 16 KiB; links, arquivos especiais, criptografia, nomes duplicados (inclusive por caixa) e caminhos inseguros são `archive`. Outros códigos: `too_large`, `too_many_files`, `missing_manifest` (inclusive um zip da pasta, com `aurora/theme.json`), `manifest_json` (JSON inválido, chave repetida) e `manifest_field` (campo obrigatório ausente ou de tipo errado). A lista completa é `ERROR_CODES`.

O `content_digest` do pacote é o SHA-256 do manifesto canônico seguido de `caminho\0sha256\n` de cada arquivo usado, em ordem: o mesmo conteúdo zipado por outra ferramenta dá o mesmo digest, e `README.md` e `LICENSES/` não entram.

### Conferir e montar um pacote

Dois comandos de autor (#634), que nunca abrem o banco:

```sh
python manage.py appearance check-package caminho/do/tema        # diretório ou .zip
python manage.py appearance build-package caminho/do/tema --output aurora-1.0.0.zip
```

`check-package` aplica as mesmas regras da instalação: o formato acima, o contraste AA da cor e a faixa `compatibility.skeleton` contra o núcleo instalado. Com o pacote certo, mostra um resumo e sai com 0:

```text
Pacote: org.example.theme.aurora 1.0.0
Nome: Aurora
Cor: #1d4ed8 · cantos: default · fonte: Aurora Sans
Imagens: logo, logo escuro, ícone · fontes: 1 · CSS: 812 bytes
Nome do produto: Aurora
Digest: 3f6c…
Pacote válido.
```

Com erro, sai com 1 e diz o código, o arquivo e a linha: `ERRO css: theme.css: Linha 3: o CSS não pode conter < nem caracteres nulos, nem em comentários.`. `build-package` confere o diretório do mesmo jeito e grava um zip determinístico (só os arquivos usados, `README.md` e `LICENSES/`, em ordem, com data e modo fixos): o mesmo diretório gera sempre os mesmos bytes, e o zip é relido para conferir o digest antes de ser gravado (de forma atômica; `--force` substitui um arquivo existente). As mensagens em português de cada código ficam em `skeleton/appearance/package_messages.py`, usadas também pela tela. Exporte o logo do editor como "SVG simples" ou "SVG otimizado". O SVG só aparece em `<img>` e é servido com `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` e `nosniff`.

### Exportar um tema

Qualquer tema do catálogo (núcleo, pronto, personalizado ou pacote) pode ser baixado como pacote, no mesmo formato que a instalação aceita (#638): para levar a outra instância ou guardar no repositório do projeto. Na tela, "Exportar" abre uma página com a caixa "Incluir a marca da instância" e o botão "Baixar pacote" (POST com CSRF e autenticação recente, resposta `application/zip` com `Cache-Control: private, no-store`); no console:

```sh
python manage.py appearance export-theme --builtin esmeralda --output esmeralda.zip
python manage.py appearance export-theme --package-id org.example.theme.aurora --output aurora.zip --include-brand
```

Um tema de pacote volta com o `theme.json` e os arquivos instalados (sem a marca, com o mesmo `content_digest`). Um tema de formulário ou pronto recebe o id local `local.theme.<slug>`, a versão `1.0.0` e a linha atual do núcleo como compatibilidade: troque o `id` antes de publicar. Com a marca, o nome, o rodapé e as imagens enviadas pelo administrador entram no pacote e vencem as do tema (o logo escuro acompanha a origem do logo). O zip é relido antes de ser entregue: um tema que a instalação recusaria hoje não é exportado.

## Rotas públicas

| Rota | Resposta |
|---|---|
| `GET /brand/theme.css` | CSS do tema ativo, `text/css; charset=utf-8`, `nosniff`, `ETag` com a versão servida; `Cache-Control: public, max-age=300`, ou `public, max-age=31536000, immutable` quando `?v=` é a versão atual; `If-None-Match` igual dá 304; sem tema, 200 com corpo vazio |
| `GET /brand/theme.css?safe=1` | Só os tokens, sem o CSS personalizado (as páginas de aparência usam esta) |
| `GET /brand/theme.css?…&scheme=light\|dark` | O mesmo CSS (com ou sem `safe=1`) com cada `(prefers-color-scheme: …)` dos prelúdios de `@media` trocado por um literal fixo: `(min-width:0)` quando é o modo pedido, `(not (min-width:0))` quando não é (`skeleton/appearance/color_scheme.py`, #665). Comentários e strings ficam intactos, e a saída continua passando na [barreira do CSS](#css-personalizado). `ETag` com o sufixo `-light` ou `-dark`, o mesmo cache por `?v=`; outro valor de `scheme` dá 400. As páginas pedem esta forma quando a pessoa forçou um modo |
| `GET /brand/logo/`, `/brand/logo-dark/`, `/brand/icon/` | Bytes da imagem enviada (chave da linha única, tipo conferido de novo pelo conteúdo) ou, sem ela, do tema ativo de pacote; `nosniff`, CSP `sandbox`, `public, max-age=300`; 404 quando não existe. Um logo SVG sai com `image/svg+xml` e `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` |
| `GET /brand/manifest.webmanifest` | Manifesto PWA do domínio principal (#639): nome do produto (ou "Skeleton"), `short_name` com até 12 caracteres, cor do tema ativo em `theme_color`/`background_color` e o ícone da instância (`/brand/icon/?v=…`, 512 × 512) ou o `favicon.svg` do núcleo; `application/manifest+json`, `public, max-age=300`. Num host de organização dá 404 (lá vale `/site/manifest.webmanifest`). As páginas do domínio principal só o referenciam quando a instância tem nome, ícone ou cor, para que, sem marca, o HTML continue igual |
| `GET /brand/fonts/<arquivo>.woff2` | Só uma fonte declarada no tema ativo (o nome é o do arquivo em `fonts/`); `font/woff2`, `nosniff`, `ETag`; `immutable` quando `?v=` é a versão atual (12 primeiros dígitos do SHA-256 do conteúdo); qualquer outro nome dá 404 |

Sem autenticação, só GET/HEAD, sem parâmetro que escolha arquivo (o nome de uma fonte é conferido contra a lista do tema ativo; a chave de armazenamento nunca aparece na URL). O CSS gerado declara cada fonte do pacote num `@font-face` que aponta para esta rota (também no modo seguro, porque as fontes não são CSS livre), e `tokens.font_family` põe a família na frente da pilha de `--font-sans`; `font-src 'self'` já permite, e a CSP das páginas não muda. `/brand/` é liberado nos endereços próprios das organizações (eles também carregam o tema da instância) e é raiz reservada: uma app não pode declarar rota em `brand/`, como em `platform/` e `admin/`. Com o cache quente, a folha de estilo não consulta o banco.

## Tela de administração

`/platform/appearance/` ("Aparência", grupo Operação do menu da plataforma, só para o administrador completo; papéis limitados recebem 403):

1. **Marca:** nome do produto (até 60) e texto do rodapé (até 160, uma linha).
2. **Imagens:** enviar ou remover o logo, o logo para o modo escuro e o ícone.
3. **Temas:** o do núcleo, os prontos, os personalizados e os pacotes instalados, com o ativo marcado e a origem na coluna "Origem" ("Núcleo", "Pronto", "Personalizado", "Pacote · `<id>` · v`<versão>`"). Ações por origem:

   | Origem | Pré-visualizar | Ativar | Editar | Excluir / Remover |
   |---|---|---|---|---|
   | Núcleo e prontos | sim | sim | — | — |
   | Personalizado | sim | sim | sim | "Excluir" |
   | Pacote | sim | sim | — (a edição dá 404; envie uma versão nova) | "Remover" |

   O ativo não mostra "Excluir" nem "Remover". Os **temas de apps** (#644) ficam numa tabela própria, "Temas de apps", com "Da app `<nome>` · v`<versão>`" e a frase "Sai do catálogo quando a app for desinstalada."; só "Pré-visualizar" (`…/apps/<app_label>/<slug>/preview/`, com as fontes e o logo do tema, só para o administrador e sem cache público) e "Ativar" (o campo `app_theme` de `…/activate/`). Sem editar, remover nem exportar. Se o tema ativo vinha de uma app que não está mais instalada, a página avisa "O tema ativo vinha da app X, que não está mais instalada. Vale o tema do núcleo até você ativar outro tema."; a página da app em `/platform/apps/` lista os temas dela e marca o ativo. "Novo tema" pede nome, cor (`#RRGGBB`), cantos, fonte e CSS personalizado. **"Instalar pacote de tema"** recebe um `.zip` de até 4 MiB (#637) e diz o resultado: "Pacote Aurora 1.0.0 instalado. Pré-visualize e ative quando quiser." (instalar não ativa), "Pacote Aurora atualizado de 1.0.0 para 1.1.0." (com "É o tema ativo: a mudança já vale para todas as páginas." quando for o caso) ou "Este pacote já está instalado; nada mudou.". Um pacote recusado volta com 400 e a mensagem do código (com arquivo e linha), nunca com o nome do arquivo enviado. "Remover" pede confirmação mostrando o `id` e a versão; os arquivos saem depois da carência.
4. **Pré-visualização:** `…/themes/<id>/preview/` e `…/builtin/<slug>/preview/` mostram uma galeria de componentes (botões, selos, mensagens, tabela, formulário, abas e item de menu ativo) com o CSS completo do tema candidato, servido por `…/preview.css` só para o administrador e com `Cache-Control: private, no-store`. Nada é ativado. De um tema de pacote, mostra também "Sobre este pacote" (id, versão, publicador, descrição, compatibilidade, o que traz e o digest) e o logo dele; as fontes e o logo do candidato vêm de `…/themes/<id>/preview/fonts/<arquivo>` e `…/preview/images/<logo|logo_dark>/`, também só para o administrador e sem cache público.

Toda escrita exige CSRF e autenticação recente, recusa campo POST desconhecido (400), responde 409 a uma revisão antiga e registra um `security_event` sem valores (`appearance.brand_changed`, `appearance.image_changed`, `appearance.theme_changed`, `appearance.theme_activated`, `appearance.package_installed`, `appearance.package_updated`, `appearance.package_removed` e, com só o código do erro, `appearance.package_rejected`; um envio sem mudança não registra nada). A tela funciona sem JavaScript.

### Saída de emergência

As páginas de `/platform/appearance/` (fora a pré-visualização) carregam o tema em modo seguro (`safe=1`): um CSS personalizado que esconda a interface não impede o administrador de editar ou trocar o tema. Se a pré-visualização esconder a página, volte pela URL da lista. Sem acesso à tela, no servidor:

```sh
python manage.py appearance show         # nome, rodapé, tema ativo e quais imagens existem (nunca as chaves)
python manage.py appearance reset-theme  # volta ao tema do núcleo, auditado com o motivo appearance.reset_theme
```

Os pacotes também têm console, idempotente, para implantação automatizada (#637), cada comando num `system_context` com o seu motivo (`appearance.install_package`, `appearance.remove_package`):

```sh
python manage.py appearance install-package aurora-1.0.0.zip   # instala, atualiza ou diz "nada mudou"; nunca ativa
python manage.py appearance list-themes                        # origem, nome, id e versão de cada tema, e o ativo
python manage.py appearance remove-package org.example.theme.aurora  # recusa o tema ativo
```

## Página inicial pública

O administrador da plataforma publica em `/platform/appearance/home/` ([#622](https://github.com/alaorneto/skeleton/issues/622)) uma página inicial em [blocos estruturados](ui-design.md#páginas-em-blocos) (`hero`, `text`, `contact`, `links`): texto simples, sempre escapado, sem HTML nem Markdown. Quem abre `/` do host da plataforma sem login a vê, com a marca e o tema da instância e, no `hero`, o botão "Entrar"; sem página publicada (ou despublicada), `/` continua levando ao login (`redirect_to_login`, `next=/`), como antes. Quem entrou continua vendo "Minhas organizações". `/` responde com `never_cache`. No endereço de uma organização, `/` é a rota da organização e nunca mostra esta página.

- **Modelo.** `sk_appearance.instancepage` (`InstancePage`, `SystemRecord` global): `slot` único com check `slot = 'home'`, `document` (`JSONField`) e `published`. Infraestrutura congelada em `sk_appearance.0002_instance_page`; sem gateway nem `write_fields`.
- **Serviço.** `skeleton/appearance/pages.py`: `home_page()` é o que `/` lê, com o mesmo cache por processo do snapshot (5 s; o último valor bom por 5 min quando o banco falha; modo setup e falha sem valor bom respondem "sem página"); `save_home_page(*, actor, document, published, expected_revision)` é a única escrita: só o administrador completo da plataforma, CAS na revisão da linha (0 antes de existir), `validate_page(kind="platform_home")` (uma página publicada precisa de ao menos um bloco), dados iguais não gravam nada, o cache é zerado depois do commit e o `security_event` `appearance.home_page_changed` leva só a situação e o número de blocos, nunca o conteúdo. `current_home_page()` lê a linha sem cache (o editor precisa da revisão atual). Nos testes, um fixture `autouse` fixa "sem página" (`pin_home_page`); os testes do recurso pedem `live_home_page`.
- **Editor.** O [editor SSR compartilhado](ui-design.md#páginas-em-blocos), com as mesmas regras das demais telas de aparência: só o administrador completo (`platform_page`), autenticação recente nas escritas, CSRF e revisão (409 com revisão antiga), 400 com o `error-summary` para erro de validação. A pré-visualização (`…/home/preview/`) usa o mesmo template da página real. A seção "Página inicial pública" de `/platform/appearance/` mostra a situação e leva ao editor.

A página é pública por definição: não lê nada de organização nem de pessoa. Para desfazer, despublique: `/` volta ao login.

## E-mails

Sem organização no contexto, os e-mails HTML (`skeleton/email/base.html`) levam o nome do produto, a cor do tema ativo e o logo da instância como parte inline (`cid:organization-logo`, nunca URL). O logo de um tema de pacote entra só se for PNG, JPEG ou WebP: um logo SVG não vira parte inline (muitos clientes de e-mail não o mostram), e o e-mail mostra só o nome. Com organização, valem a marca dela; se ela não tem cor, vale a do tema da instância, e o logo da instância nunca entra num e-mail de organização. Os e-mails de conta do allauth (texto) saudam com o nome do produto no domínio principal ([notificações](notifications.md#marca-nos-e-mails)).
