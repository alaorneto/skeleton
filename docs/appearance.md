# Marca e temas da instância

O administrador da plataforma configura em `/platform/appearance/` a marca global da instância — nome do produto, rodapé, logo, variante escura do logo e ícone — e escolhe ou instala o tema ativo (cor, cantos, tipografia e CSS personalizado). Todas as alterações passam a valer sem reinício do processo nem recompilação de CSS. A marca de cada organização ([#168](https://github.com/alaorneto/skeleton/issues/168), [endereços de organização](organization-sites.md)) continua prevalecendo nas telas e nos e-mails dela. O desenho geral pertence ao epic [#591](https://github.com/alaorneto/skeleton/issues/591).

Esta página é a referência técnica do subsistema. Para o passo a passo de autoria e publicação a partir de um sistema de design, consulte [criar e publicar um tema](themes.md).

## Precedência

| O quê | Organização (telas e e-mails dela) | Marca da instância | Tema ativo (pacote) | Núcleo |
|---|---|---|---|---|
| Nome no título, no cabeçalho e no rodapé | nome de exibição da organização; no endereço próprio, o nome publicado | `product_name` | `brand.product_name` do pacote | "Skeleton" |
| Rodapé | — | `footer_text` | `brand.footer_text` do pacote | frase do núcleo |
| Cor | `brand_color` da organização (`<style nonce>`, depois do tema) | — | cor do tema ativo | escala "Cobalto e Ardósia" |
| Logo do cabeçalho | logo da organização | logo enviado (com a variante escura enviada) | logo do pacote (com a variante escura do pacote) | ícone do núcleo |
| Favicon | ícone da organização | ícone enviado | ícone do pacote | `favicon.svg` |
| Cantos, fonte, fontes e CSS personalizado | — (organizações não escolhem tema) | — | tema ativo | tokens do núcleo |

Para cada campo, vale o primeiro valor não vazio da esquerda para a direita a partir da coluna da instância (a coluna da organização aplica-se somente às telas e e-mails dela). O logo escuro acompanha sempre a mesma origem do logo principal: se a instância tem logo enviado sem variante escura própria, ela não herda o logo escuro do pacote, evitando misturar duas marcas diferentes.

A folha de estilo do tema é carregada depois de `site.css` e antes do `<style>` da organização, garantindo a precedência pela ordem do CSS. Uma organização sem marca própria herda nome, logo e tema da instância. Ficam fora desse mecanismo a página de manutenção e o setup (renderizados antes do banco, mantendo o tema do núcleo), além de temas por organização. A escolha de modo claro ou escuro da pessoa prevalece sobre tudo sem alterar a marca ([modo escuro](ui-design.md#modo-escuro)).

## Modelo

O pacote `skeleton/appearance` (label `sk_appearance`) concentra apenas dados, regras puras e serviços; as páginas e rotas públicas ficam em `skeleton/web`. As duas tabelas são `SystemRecord` globais, com revisão/CAS, auditoria por trigger e infraestrutura congelada em `sk_appearance.0001_initial`:

- `sk_appearance.instancetheme`: registra cada tema do catálogo (`name` único entre vivos, `brand_color` vazio ou `#rrggbb`, `radius` em `sharp|default|round`, `font` em `default|system|serif` — temas gravados em versões anteriores podem conter `inter`, interpretado como `default` — e `custom_css` de até 32 KiB). O CSS personalizado **não** é campo sensível, para que a auditoria registre quem mudou o quê. A origem fica em `source`: `form` (criado na interface) ou `package` (instalado de um [pacote de tema](#pacote-de-tema)). Temas de pacote armazenam ainda `package_id` (único entre vivos), `package_version`, `package_digest` (o `content_digest`), `package_manifest` (o `theme.json` validado), `font_family`, `product_name`, `footer_text` e `package_files` (mapa das chaves de armazenamento de imagens e fontes, campo **sensível** redigido na auditoria). Temas `form` não podem preencher esses campos (garantido por constraint), e a infraestrutura atualizada consta em `sk_appearance.0003_theme_packages`.
- `sk_appearance.instancebrand`: linha única (`slot = "instance"`) com `product_name` (vazio exibe "Skeleton"), `footer_text`, as chaves sensíveis `logo_key`, `logo_dark_key` e `icon_key` (redigidas na auditoria) e o tema ativo: `builtin_theme` (tema pronto), `active_theme` (tema do catálogo) **ou** `app_theme` (`<app_label>:<slug>`, tema de app; coluna e constraint em `sk_appearance.0004_app_theme`), nunca mais de um simultaneamente. Quando todos ficam vazios, vale o tema do núcleo.

Marca e tema não vão para o SQLite da instância: esse arquivo guarda apenas configurações lidas antes do Django e aplicadas após reinício (E025), enquanto aparência é dado operacional editado pela interface.

## Quem altera

Somente o administrador da plataforma pode alterar marca e temas, usando os serviços de `skeleton.appearance.services` e `skeleton.appearance.images` (todos keyword-only, recebendo `actor`, dentro de `transaction.atomic()`): `update_brand`, `create_theme`, `update_theme`, `delete_theme`, `activate_theme`, `replace_image` e `remove_image`.

Toda escrita exige `expected_revision` (revisão atual da linha, ou `0` antes da criação da linha da marca) e levanta `ConcurrentUpdate` em caso de divergência. Envios sem alteração efetiva não gravam nada; campos fora da lista fechada são recusados ("Campo não permitido.") e qualquer outro usuário recebe `PermissionDenied`. `actor=None` é aceito apenas no console (`manage.py appearance`), que abre seu próprio `system_context` com motivo explícito.

**Pacotes** (`skeleton.appearance.packages.install_theme_package(actor=..., data=bytes)`): o leitor valida o `.zip` inteiro em memória antes de qualquer escrita, seguido pela checagem de contraste AA da cor e da faixa do núcleo. Cada imagem e fonte é gravada no armazenamento privado sob chave `platform/branding/<uuid4>`, e o tema é salvo com `source="package"`. O resultado (`InstallResult`) informa `installed`, `updated` (com a versão anterior) ou `unchanged`:

| Situação | Resultado |
|---|---|
| `id` novo | instala (até 50 temas vivos no catálogo; nome único entre vivos) |
| mesmo `id`, mesmo `content_digest` | `unchanged`, nada grava |
| mesmo `id`, versão maior | `updated`: atualiza a mesma linha (mesmo `sysid`, revisão + 1); se estiver ativo, permanece ativo |
| mesmo `id`, mesma versão, outro conteúdo | recusado: `same_version_changed` |
| mesmo `id`, versão menor | recusado: `downgrade` |

Instalar um pacote nunca o ativa automaticamente. Temas de pacote são somente leitura no formulário (`update_theme` responde `package_read_only`), mas podem ser removidos com `delete_theme` da mesma forma que temas de formulário (exclusão lógica, proibida enquanto ativo). Instalação e ativação se serializam pela linha da marca; duas instalações paralelas de um mesmo pacote novo têm apenas um vencedor, e a outra recebe `ConcurrentUpdate`.

## Leitura a cada requisição e cache

`skeleton.appearance.snapshot.current_appearance()` devolve um snapshot `Appearance` imutável (nome, rodapé, versões das imagens, CSS do tema com e sem o CSS livre, versões desses CSS e cor). Ele consulta o banco no máximo a cada 5 segundos por processo; se a consulta falhar, mantém o último valor bom por 5 minutos antes de voltar à aparência do núcleo.

Toda escrita chama `reset_appearance_cache()` após o commit, fazendo o processo que gravou refletir a mudança na hora e os demais em até 5 segundos. `cached_appearance()` nunca toca no banco e é o caminho usado pelas páginas de erro (permitindo renderizar um 500 mesmo com o banco indisponível). Na suíte de testes, um fixture `autouse` fixa a aparência padrão (`pin_appearance`) para não alterar orçamentos de consultas; testes que exercitam este subsistema solicitam `live_appearance`.

## Temas

`skeleton/appearance/themes.py` é Python puro. A cor escolhida corresponde ao passo 600 da marca e precisa atingir contraste WCAG AA (4,5:1) com o branco — a mesma regra da cor da organização. A partir dela é gerada toda a escala `--color-cobalt-50…950` por mistura em sRGB com branco e preto. Como o `site.css` compilado referencia essa escala (`--color-brand: var(--color-cobalt-600)`), redefini-la atualiza botões, links, foco, fundos suaves e o gradiente de login de uma só vez.

No modo escuro, os fundos da marca e os tokens de texto (`--color-fg-brand`, `--color-fg-brand-strong`, `--color-focus`) são emitidos explicitamente, clareados até alcançar contraste AA sobre o fundo escuro. Os cantos `sharp` e `round` ajustam `--radius-base` e `--radius-card`; as fontes `system` e `serif` trocam `--font-sans` por pilhas do sistema, sem fontes externas. O CSS gerado contém apenas valores `#rrggbb` e literais fixos — o nome do tema nunca é interpolado no CSS.

O núcleo oferece cinco temas prontos em código, sem CSS livre: `cobalto` (padrão do núcleo, sem emissão extra), `esmeralda` (`#047857`), `grafite` (`#374151`), `vinho` (`#9f1239`) e `violeta` (`#6d28d9`). O catálogo personalizado aceita até 50 temas vivos. A exclusão é lógica (liberando o nome) e é proibida para o tema ativo. Se um tema ativo for excluído externamente ou apontar para um slug desconhecido, a instância volta silenciosamente ao tema do núcleo, sem erro.

## CSS personalizado

`skeleton/appearance/css_validation.py` atua como defesa em profundidade para o texto livre: a CSP (`default-src 'self'`, `img-src 'self' data:`, `font-src 'self'`) já impede carregar recursos externos, e o CSS nunca é embutido no HTML, sendo servido apenas como `text/css` em `/brand/theme.css`.

A validação rejeita, nesta ordem:

1. valores que não sejam texto;
2. conteúdo acima de 32 768 bytes em UTF-8;
3. `<` e caracteres nulos (impedindo `</style>` ou qualquer marcação);
4. strings ou comentários não encerrados;
5. chaves ou parênteses não balanceados fora de strings;
6. na forma normalizada usada apenas para análise (comentários removidos pelo mesmo lexer ciente de strings, escapes CSS decodificados e texto em minúsculas): `@import`, `@charset`, `@namespace`, `url(`, `image-set(`, `image(`, `src(`, `expression(`, `javascript:`, `behavior` e `-moz-binding`. Com isso, variantes ofuscadas como `u\72l(`, `\75 rl(` e `@im/**/port` também são barradas.

O texto gravado no banco é sempre o original. A mensagem de erro indica a linha ("Linha 3: o CSS não pode usar url(."). Dois pontos costumam surpreender autores: `<` é proibido **inclusive dentro de comentários** (`/* <style> */` é recusado) e `url(` é bloqueado em qualquer lugar, pois fontes e imagens devem vir do [pacote de tema](#pacote-de-tema).

A validação pura está em `css_problem(texto)`, que não importa Django e retorna `CssProblem(code, line, item)` (códigos `not_text`, `too_large`, `markup`, `open_comment`, `open_string`, `unbalanced`, `forbidden`); `validate_custom_css` é o invólucro com `ValidationError` usado pelos serviços e telas. Como a contagem de linhas ocorre na forma normalizada, o caso raro de um escape hexadecimal seguido de quebra de linha pode deslocar a linha em uma unidade.

O contrato estável para o CSS livre compreende os tokens `--color-*`, `--radius-*`, `--font-*` e as classes de componente documentadas em [UI](ui-design.md#marca-e-tema-da-instância).

## Temas trazidos por apps

Uma app pode embarcar temas da instância no próprio pacote, declarados em `runtime.instance_themes` no manifesto ([extensão](extending.md#manifest-ativação-e-superfícies-próprias), epic [#630](https://github.com/alaorneto/skeleton/issues/630)), usando o mesmo formato do [pacote de tema](#pacote-de-tema) em um diretório interno da app. `package build` e `package doctor` validam cada tema com o leitor; na carga da aplicação, um tema inválido gera `skeleton.E047` e fica fora do catálogo. O guia de autoria está em [publicar junto de uma app](themes.md#publicar-junto-de-uma-app).

Um tema de app **não é copiado para o banco**: ele existe enquanto o código da app estiver instalado, assim como os temas prontos existem com o núcleo. `skeleton.appearance.app_themes.app_themes()` monta, uma vez por processo e sem consultar o banco, o registro dos temas válidos das apps carregadas, mantendo apenas metadados (CSS, nomes e SHA-256 dos arquivos, nunca os bytes nem caminhos fora do registro).

A ativação grava apenas a chave `InstanceBrand.app_theme = "<app_label>:<slug>"` (`activate_theme(..., app_theme=...)`, garantindo exclusividade entre os tipos de tema também por constraint). Nas páginas, o tema de app se comporta como um pacote: fornece tokens, fontes, CSS, imagens, nome e rodapé (com precedência da marca da instância onde configurada). Os arquivos são servidos pelas mesmas rotas `/brand/`, lidos apenas entre os arquivos declarados no diretório da app, com caminho resolvido confinado a ele e SHA-256 conferido.

Ao desinstalar a app (o agente troca o código e reinicia os processos), o tema desaparece do catálogo sem sincronização extra. Uma chave `app_theme` que aponte para uma app ausente passa a valer como o tema do núcleo, sem erro (`manage.py appearance show` informa "da app X (não instalada; vale o tema do núcleo)"); reinstalar a app restaura o tema. Enviar um `.zip` com o `id` de um tema de app — ou com o prefixo `<app_id>.theme.` de uma app instalada que fornece temas — é recusado com `id_belongs_to_app`, pois temas de app são atualizados junto com a própria app.

## Imagens

`skeleton/appearance/images.py` gerencia o logo principal e o logo para modo escuro (PNG, JPEG ou WebP de até 512 KB) e o ícone (PNG quadrado de 512 × 512 até 256 KB), aplicando os mesmos validadores das imagens de organização (`checked_logo` e `checked_icon` em `skeleton.tenancy.branding`). O tipo é sempre verificado pelo conteúdo real, nunca pela extensão; SVG avulso e GIF são recusados.

As imagens são armazenadas no storage privado ([arquivos](files.md)) sob chaves `platform/branding/<uuid4>` geradas pelo servidor. Os bytes são gravados antes do banco: se a gravação da linha falhar, o objeto é apagado imediatamente; objetos substituídos são removidos após o commit; e eventuais órfãos deixados por rollback são recolhidos pelo job global `appearance.orphans` após o período de carência ([jobs](jobs.md)). A variante escura só aparece quando há logo principal. Arquivos de pacotes de tema usam o mesmo prefixo de chave; como o job considera referenciadas as chaves de `package_files` dos temas vivos, arquivos de versões antigas ou pacotes removidos são limpos apenas pelo job, fora da transação.

### Logos em SVG

Apenas dentro de um [pacote de tema](#pacote-de-tema) o logo e o logo escuro podem ser SVG; envios avulsos pela tela continuam restritos a PNG, JPEG e WebP, e o ícone é sempre PNG. `skeleton/appearance/svg_validation.py` (`svg_problem(conteúdo)`, Python puro) aceita um subconjunto estático seguro e recusa o restante sem reescrever o arquivo:

- até 256 KiB em UTF-8 (com ou sem BOM), elemento raiz `svg` no namespace SVG com `viewBox` ou `width` e `height`, até 32 níveis de profundidade e até 5 000 elementos;
- elementos permitidos: `svg`, `g`, `defs`, `title`, `desc`, `path`, `rect`, `circle`, `ellipse`, `line`, `polyline`, `polygon`, `text`, `tspan`, `linearGradient`, `radialGradient`, `stop`, `clipPath`, `mask`, `pattern`, `symbol` e `use`. Qualquer outro é recusado, incluindo `script`, `style`, `foreignObject`, `image`, `a`, animações (`animate*`, `set`), filtros e elementos de editores (`sodipodi:*`, `inkscape:*`);
- bloqueados antes do parse: `<!DOCTYPE`, `<!ENTITY`, `<![CDATA[` e instruções de processamento além da declaração `<?xml …?>` inicial (eliminando entidades e bombas de expansão);
- atributos `on*` recusados; `href`/`xlink:href` permitidos apenas em `use`, gradientes e `pattern`, e exclusivamente com `#id`; proibidos valores com `javascript:`, `data:`, `vbscript:` ou `url(` diferente de `url(#id)`; `style` sem `@`, `expression`, `\` ou `<`; apenas atributos sem namespace ou `xlink:href`, `xml:space` e `xml:lang`;
- conteúdo textual permitido apenas em `title`, `desc`, `text` e `tspan`.

Erros (`SvgProblem(code, item)`) indicam o elemento ou atributo problemático, nunca o valor.

## Pacote de tema

Para o roteiro de autoria, veja [criar e publicar um tema](themes.md). Um tema pode ser distribuído como **pacote** — um `.zip` ou um diretório interno de app — em formato padronizado, validado e instalado pela instância (epic [#629](https://github.com/alaorneto/skeleton/issues/629)). O leitor fica em `skeleton/appearance/package_format.py`: `read_theme_archive(bytes)` e `read_theme_directory(caminho)` devolvem um `ThemePackage` imutável ou levantam `ThemePackageError(code, path, line, item)`. Ele não importa Django (rodando também no build de apps) e não toca no disco: todos os bytes são lidos em memória e nenhum nome de arquivo vira caminho local.

Os arquivos ficam **na raiz** do `.zip`, sem pasta de topo:

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

Os nomes dentro de `images/` e `fonts/` são livres (os exemplos acima são convenção): vale o que estiver declarado em `theme.json`. Qualquer arquivo não declarado gera `unexpected_file`, evitando que sobras entrem despercebidas no `.zip`. Todos os caminhos aceitam apenas segmentos `[a-z0-9][a-z0-9._-]*`, com exceção de caixa para `README.md` e `LICENSES/`.

`theme.json` segue esquema fechado (chaves desconhecidas em qualquer nível geram `unknown_field`):

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
| `id` | obrigatório; `[a-z][a-z0-9._-]{1,127}` (mesma regra dos IDs de componente) | `id` |
| `version` | obrigatório; versão semântica | `version` |
| `name` | obrigatório; 1–80 caracteres, uma linha | `name` |
| `description` | até 300 caracteres, uma linha | `text_field` |
| `publisher` | mesma regra de `id` | `publisher` |
| `compatibility.skeleton` | obrigatório; faixa de versões que precisa conter a versão instalada do núcleo | `compatibility`, `incompatible_core` |
| `tokens.brand_color` | `""` ou `#rrggbb`; na instalação, exige contraste AA com branco | `brand_color` |
| `tokens.radius` | `sharp`, `default` (padrão) ou `round` | `radius` |
| `tokens.font` | `default` (padrão: IBM Plex Sans do núcleo), `system` ou `serif`. `default` vale a partir do núcleo 0.5.0; para núcleos anteriores, omita o campo ou use o nome legado `inter`, que continua aceito e lido como `default` | `font` |
| `tokens.font_family` | `""` ou o `family` de uma entrada em `fonts` (prevalece sobre `font`) | `font_family` |
| `brand.product_name`, `brand.footer_text` | até 60 e 160 caracteres, uma linha | `text_field` |
| `images.logo`, `images.logo_dark` | em `images/`; PNG, JPEG ou WebP (provado pelo conteúdo) até 512 KB, ou [SVG validado](#logos-em-svg) até 256 KiB; o logo escuro exige o logo principal | `image_path`, `image_type`, `image_size`, `svg`, `logo_dark_without_logo` |
| `images.icon` | em `images/`; PNG 512 × 512 até 256 KiB | `image_path`, `image_size`, `icon` |
| `fonts` | até 8 entradas: `family` (`[A-Za-z][A-Za-z0-9 -]{0,62}[A-Za-z0-9]`, nunca um nome genérico do CSS; `IBM Plex Sans Variable` é aceita e substitui a do núcleo no tema), `file` (`fonts/*.woff2`, assinatura `wOF2`, até 512 KiB), `weight` (`"400"`, padrão, ou faixa `"100 900"`) e `style` (`normal` ou `italic`) | `too_many_fonts`, `font_family_name`, `font_file`, `font_weight`, `font_style` |
| `css` | padrão `theme.css`, na raiz; se declarado, precisa existir; segue as regras do [CSS personalizado](#css-personalizado) | `css_missing`, `css` (com a linha) |

Limites do arquivo `.zip`: até 4 MiB comprimido, 6 MiB descomprimido, 40 membros, razão máxima de expansão de 100 por membro e `theme.json` de até 16 KiB. Links, arquivos especiais, criptografia, nomes duplicados (inclusive diferenciando apenas caixa) e caminhos inseguros geram `archive`. Outros códigos: `too_large`, `too_many_files`, `missing_manifest` (inclusive um `.zip` com pasta de topo como `aurora/theme.json`), `manifest_json` (JSON inválido ou chave repetida) e `manifest_field` (campo obrigatório ausente ou com tipo errado). A lista completa está em `ERROR_CODES`.

O `content_digest` do pacote é o SHA-256 do manifesto canônico seguido por `caminho\0sha256\n` de cada arquivo usado, em ordem: o mesmo conteúdo compactado por ferramentas diferentes produz o mesmo digest, e `README.md` e `LICENSES/` não entram no cálculo.

### Conferir e montar um pacote

Dois comandos de autoria permitem validar e empacotar sem abrir o banco:

```sh
python manage.py appearance check-package caminho/do/tema        # diretório ou .zip
python manage.py appearance build-package caminho/do/tema --output aurora-1.0.0.zip
```

`check-package` aplica as mesmas validações da instalação (formato, contraste AA e compatibilidade com o núcleo instalado). Quando válido, exibe o resumo e sai com código 0:

```text
Pacote: org.example.theme.aurora 1.0.0
Nome: Aurora
Cor: #1d4ed8 · cantos: default · fonte: Aurora Sans
Imagens: logo, logo escuro, ícone · fontes: 1 · CSS: 812 bytes
Nome do produto: Aurora
Digest: 3f6c…
Pacote válido.
```

Em caso de erro, encerra com código 1 informando código, arquivo e linha: `ERRO css: theme.css: Linha 3: o CSS não pode conter < nem caracteres nulos, nem em comentários.`. `build-package` valida o diretório da mesma forma e gera um `.zip` determinístico (incluindo apenas arquivos usados, `README.md` e `LICENSES/`, em ordem, com data e permissões fixas): o mesmo diretório gera sempre os mesmos bytes, e o arquivo resultante é relido para conferência do digest antes da gravação atômica (`--force` sobrescreve arquivo existente).

As mensagens em português de cada código residem em `skeleton/appearance/package_messages.py` e são reutilizadas pela interface. Ao criar um logo vetorial, exporte-o como "SVG simples" ou "SVG otimizado". Na entrega HTTP, SVGs só são usados em `<img>` e saem com `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` e `nosniff`.

### Exportar um tema

Qualquer tema do catálogo (núcleo, pronto, personalizado ou pacote) pode ser exportado no formato de pacote aceito pela instalação, facilitando replicá-lo em outra instância ou versioná-lo no repositório do projeto.

Na interface, **Exportar** abre a página com a opção **Incluir a marca da instância** e o botão **Baixar pacote** (POST com CSRF e autenticação recente, resposta `application/zip` com `Cache-Control: private, no-store`). No console:

```sh
python manage.py appearance export-theme --builtin esmeralda --output esmeralda.zip
python manage.py appearance export-theme --package-id org.example.theme.aurora --output aurora.zip --include-brand
```

Temas de pacote são exportados com o `theme.json` e os arquivos originais (mantendo o mesmo `content_digest` quando a marca não é incluída). Temas prontos ou de formulário recebem o identificador local `local.theme.<slug>`, versão `1.0.0` e compatibilidade com a linha atual do núcleo — troque o `id` antes de publicar. Quando a opção de incluir a marca está marcada, nome, rodapé e imagens configurados pelo administrador entram no pacote e prevalecem sobre os do tema (o logo escuro acompanha a origem do logo). Todo `.zip` exportado é relido antes da entrega, garantindo que nenhum tema incompatível com as regras atuais seja gerado.

## Rotas públicas

| Rota | Resposta |
|---|---|
| `GET /brand/theme.css` | CSS do tema ativo, `text/css; charset=utf-8`, `nosniff`, `ETag` com a versão servida; `Cache-Control: public, max-age=300`, ou `public, max-age=31536000, immutable` quando `?v=` coincide com a versão atual; `If-None-Match` igual responde 304; sem tema ativo, 200 com corpo vazio |
| `GET /brand/theme.css?safe=1` | Somente os tokens, sem o CSS personalizado (usado pelas páginas de aparência) |
| `GET /brand/theme.css?…&scheme=light\|dark` | O mesmo CSS (com ou sem `safe=1`) com cada `(prefers-color-scheme: …)` nos prelúdios de `@media` substituído por literal fixo: `(min-width:0)` para o modo pedido e `(not (min-width:0))` para o oposto (`skeleton/appearance/color_scheme.py`). Comentários e strings ficam intactos, e a saída continua passando na [barreira do CSS](#css-personalizado). `ETag` leva o sufixo `-light` ou `-dark`, com o mesmo cache por `?v=`; valores inválidos de `scheme` respondem 400. Usado quando a pessoa força um modo |
| `GET /brand/logo/`, `/brand/logo-dark/`, `/brand/icon/` | Bytes da imagem enviada pela instância (chave da linha única, tipo conferido novamente pelo conteúdo) ou, na ausência dela, do tema ativo de pacote; `nosniff`, CSP `sandbox`, `public, max-age=300`; 404 se ausente. Logos SVG saem com `image/svg+xml` e `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'; sandbox` |
| `GET /brand/manifest.webmanifest` | Manifesto PWA do domínio principal: nome do produto (ou "Skeleton"), `short_name` de até 12 caracteres, cor do tema ativo em `theme_color` e `background_color` e o ícone da instância (`/brand/icon/?v=…`, 512 × 512) ou o `favicon.svg` do núcleo; `application/manifest+json`, `public, max-age=300`. Em host de organização responde 404 (ali vale `/site/manifest.webmanifest`). Só é referenciado no HTML do domínio principal quando a instância define nome, ícone ou cor |
| `GET /brand/fonts/<arquivo>.woff2` | Somente fontes declaradas no tema ativo (pelo nome do arquivo em `fonts/`); `font/woff2`, `nosniff`, `ETag`; `immutable` quando `?v=` bate com os 12 primeiros dígitos do SHA-256 do conteúdo; qualquer outro nome responde 404 |

Essas rotas são públicas (sem autenticação), aceitam apenas GET/HEAD e nunca recebem parâmetros que escolham arquivos arbitrariamente: nomes de fontes são validados contra a lista do tema ativo e chaves de armazenamento nunca aparecem na URL. O CSS gerado declara cada fonte do pacote em `@font-face` apontando para `/brand/fonts/...` (inclusive no modo seguro, pois fontes não são CSS livre), e `tokens.font_family` posiciona a família no início de `--font-sans`; como `font-src 'self'` já cobre a origem, a CSP das páginas não muda.

A raiz `/brand/` também é liberada nos endereços próprios das organizações (que carregam o tema da instância) e é reservada ao núcleo — apps não podem declarar rotas em `brand/`, assim como em `platform/` e `admin/`. Com o cache quente, servir a folha de estilo não consulta o banco.

## Tela de administração

`/platform/appearance/` (**Aparência**, no grupo Operação do menu da plataforma) é restrita ao administrador completo da plataforma; papéis limitados recebem 403. A tela organiza a gestão em quatro blocos:

1. **Marca:** nome do produto (até 60 caracteres) e texto do rodapé (até 160 caracteres, em uma linha).
2. **Imagens:** envio e remoção de logo, logo para modo escuro e ícone.
3. **Temas:** lista o tema do núcleo, os temas prontos, os personalizados e os pacotes instalados, indicando o ativo e sua origem ("Núcleo", "Pronto", "Personalizado", "Pacote · `<id>` · v`<versão>`"). Ações por origem:

   | Origem | Pré-visualizar | Ativar | Editar | Excluir / Remover |
   |---|---|---|---|---|
   | Núcleo e prontos | sim | sim | — | — |
   | Personalizado | sim | sim | sim | "Excluir" |
   | Pacote | sim | sim | — (edição retorna 404; envie uma nova versão) | "Remover" |

   O tema ativo não exibe **Excluir** nem **Remover**. **Temas de apps** aparecem em tabela própria com "Da app `<nome>` · v`<versão>`" e o aviso "Sai do catálogo quando a app for desinstalada.", oferecendo apenas **Pré-visualizar** (`…/apps/<app_label>/<slug>/preview/`, com fontes e logo do tema, restrito ao administrador e sem cache público) e **Ativar** (campo `app_theme` de `…/activate/`), sem edição, remoção nem exportação. Se o tema ativo pertencia a uma app desinstalada, a página avisa ("O tema ativo vinha da app X, que não está mais instalada. Vale o tema do núcleo até você ativar outro tema."), e a página da app em `/platform/apps/` lista seus temas marcando o ativo.

   **Novo tema** solicita nome, cor (`#RRGGBB`), cantos, fonte e CSS personalizado. **Instalar pacote de tema** aceita um `.zip` de até 4 MiB e informa o resultado: "Pacote Aurora 1.0.0 instalado. Pré-visualize e ative quando quiser." (instalar não ativa), "Pacote Aurora atualizado de 1.0.0 para 1.1.0." (acompanhado de "É o tema ativo: a mudança já vale para todas as páginas." quando aplicável) ou "Este pacote já está instalado; nada mudou.". Pacotes rejeitados retornam 400 com a mensagem do código (incluindo arquivo e linha), nunca expondo o nome do arquivo enviado. **Remover** exige confirmação com `id` e versão; os arquivos são limpos após a carência.

4. **Pré-visualização:** `…/themes/<id>/preview/` e `…/builtin/<slug>/preview/` exibem uma galeria de componentes (botões, selos, mensagens, tabela, formulário, abas e item de menu ativo) usando o CSS completo do candidato, servido em `…/preview.css` apenas para o administrador com `Cache-Control: private, no-store`, sem ativá-lo. Em temas de pacote, mostra ainda a seção "Sobre este pacote" (id, versão, publicador, descrição, compatibilidade, conteúdo e digest) e seu logo; fontes e logos do candidato saem por `…/themes/<id>/preview/fonts/<arquivo>` e `…/preview/images/<logo|logo_dark>/`, igualmente restritos e sem cache público.

Toda escrita exige CSRF e autenticação recente, rejeita campos POST desconhecidos (400), responde 409 em revisão desatualizada e registra um `security_event` sem valores (`appearance.brand_changed`, `appearance.image_changed`, `appearance.theme_changed`, `appearance.theme_activated`, `appearance.package_installed`, `appearance.package_updated`, `appearance.package_removed` e, apenas com o código de erro, `appearance.package_rejected`; envios sem alteração não geram evento). Toda a interface funciona sem JavaScript.

### Saída de emergência

Todas as páginas sob `/platform/appearance/` (exceto a pré-visualização) carregam o tema em modo seguro (`safe=1`), de forma que um CSS personalizado defeituoso nunca impede o administrador de corrigir ou trocar o tema. Caso a própria pré-visualização oculte o conteúdo, basta retornar pela URL da lista. Sem acesso à interface web, o servidor oferece recuperação via CLI:

```sh
python manage.py appearance show         # nome, rodapé, tema ativo e quais imagens existem (nunca as chaves)
python manage.py appearance reset-theme  # volta ao tema do núcleo, auditado com o motivo appearance.reset_theme
```

Os pacotes também dispõem de comandos de console idempotentes para automação de deploy, executados em `system_context` com seus motivos (`appearance.install_package`, `appearance.remove_package`):

```sh
python manage.py appearance install-package aurora-1.0.0.zip   # instala, atualiza ou diz "nada mudou"; nunca ativa
python manage.py appearance list-themes                        # origem, nome, id e versão de cada tema, e o ativo
python manage.py appearance remove-package org.example.theme.aurora  # recusa o tema ativo
```

## Página inicial pública

Em `/platform/appearance/home/` ([#622](https://github.com/alaorneto/skeleton/issues/622)), o administrador da plataforma pode publicar uma página inicial em [blocos estruturados](ui-design.md#páginas-em-blocos) (`hero`, `text`, `contact`, `links`) — sempre texto simples escapado, sem HTML nem Markdown.

Visitantes sem login que acessam `/` no host da plataforma veem essa página com a marca e o tema da instância e o botão **Entrar** no bloco `hero`. Sem página publicada (ou após despublicação), `/` continua redirecionando para o login (`redirect_to_login`, `next=/`). Usuários já autenticados seguem vendo "Minhas organizações". A rota `/` responde com `never_cache`; no endereço próprio de uma organização, `/` pertence à organização e nunca exibe esta página.

- **Modelo:** `sk_appearance.instancepage` (`InstancePage`, `SystemRecord` global) com `slot` único restrito a `slot = 'home'`, `document` (`JSONField`) e `published`. Infraestrutura congelada em `sk_appearance.0002_instance_page`, sem gateway nem `write_fields`.
- **Serviço:** `skeleton/appearance/pages.py`. `home_page()` serve a leitura de `/` com o mesmo cache por processo do snapshot (5 s; mantém o último valor bom por 5 min em falha do banco; modo setup e falha sem cache retornam "sem página"). `save_home_page(*, actor, document, published, expected_revision)` é a única operação de escrita: restrita ao administrador completo da plataforma, com CAS na revisão da linha (`0` antes de existir) e validação via `validate_page(kind="platform_home")` (página publicada exige ao menos um bloco). Dados idênticos não gravam nada; após o commit o cache é limpo e registra-se o `security_event` `appearance.home_page_changed` apenas com situação e contagem de blocos, nunca o conteúdo. `current_home_page()` lê sem cache para fornecer a revisão atual ao editor. Nos testes, `pin_home_page` (`autouse`) fixa "sem página"; testes específicos pedem `live_home_page`.
- **Editor:** utiliza o [editor SSR compartilhado](ui-design.md#páginas-em-blocos), seguindo as mesmas garantias das demais telas de aparência: restrito ao administrador completo (`platform_page`), autenticação recente nas escritas, CSRF, CAS (409 em revisão antiga) e 400 com `error-summary` em erro de validação. A pré-visualização (`…/home/preview/`) renderiza o mesmo template da página real, e a seção **Página inicial pública** em `/platform/appearance/` informa o estado atual e leva ao editor.

A página é pública por definição e não lê dados de organizações nem de pessoas. Para reverter, basta despublicá-la e `/` volta a abrir o login.

## E-mails

Fora do contexto de uma organização, os e-mails HTML (`skeleton/email/base.html`) incluem o nome do produto, a cor do tema ativo e o logo da instância embutido como parte inline (`cid:organization-logo`, nunca URL externa). Logos vindos de pacotes de tema só entram no e-mail se forem PNG, JPEG ou WebP — logos SVG não são embutidos (por baixa compatibilidade em clientes de e-mail), e o cabeçalho exibe apenas o nome.

Em e-mails de uma organização, prevalece a marca dela; caso não tenha cor própria, herda a cor do tema da instância, mas o logo da instância nunca entra em e-mails organizacionais. Os e-mails de conta do allauth em texto puro saúdam com o nome do produto no domínio principal ([notificações](notifications.md#marca-nos-e-mails)).
