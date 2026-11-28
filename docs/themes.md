# Criar e publicar um tema da instância

Este guia leva de um sistema de design (cores, tipografia, raios, logos) a um **pacote de tema** validado, instalado e ativo numa instância, sem tocar nos templates do núcleo. A referência do formato, das rotas e da precedência é a [aparência da instância](appearance.md); aqui está o procedimento. Agentes seguem o skill `instance-theme` (`.claude/skills/instance-theme/`), que resume estes passos.

## Antes de começar

Um tema muda a **aparência**, não a estrutura: tokens de cor, cantos e fonte, fontes próprias, CSS por cima das classes de componente estáveis, logo, logo para o modo escuro, ícone (favicon), nome do produto e rodapé. Ele não muda leiaute, menus, componentes nem textos das telas. Se o sistema de design pede isso, é uma mudança no núcleo ou numa app, não um tema.

A marca vale nesta ordem, campo a campo: **organização** (nas telas e e-mails dela) → **marca da instância** (o que o administrador definiu em `/platform/appearance/`) → **tema ativo** (o que o pacote trouxe) → **núcleo**. Um pacote preenche o que a marca da instância deixou vazio; ele nunca passa por cima do que o administrador definiu.

## Estrutura do pacote

Um diretório (no repositório do projeto) que vira um `.zip` com os arquivos na raiz:

```text
aurora/
├── theme.json            o manifesto (obrigatório)
├── theme.css             CSS personalizado (opcional)
├── images/
│   ├── logo.svg          logo para fundo claro (PNG, JPEG, WebP ou SVG)
│   ├── logo-dark.svg     logo para fundo escuro (opcional, só com o logo)
│   └── icon.png          ícone/favicon: PNG 512 × 512
├── fonts/
│   └── aurora-sans.woff2 até 8 fontes .woff2
├── LICENSES/
│   └── OFL.txt           licenças das fontes (recomendado)
└── README.md             opcional
```

O `theme.json` de exemplo, campo a campo:

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

- `id` é a identidade estável do pacote (letras minúsculas, números, `.`, `-`, `_`); `version` sobe a cada publicação (a instância só aceita versão maior do mesmo `id`).
- `compatibility.skeleton` é a faixa de versões do núcleo em que o tema foi conferido.
- `tokens` são os parâmetros que o núcleo transforma em CSS; `brand`, `images` e `fonts` são o resto da identidade.

As regras de cada campo e os limites estão na tabela de [pacote de tema](appearance.md#pacote-de-tema).

## Do sistema de design para os tokens

O núcleo é construído sobre tokens CSS. O tema troca os tokens; os componentes (`.btn`, `.card`, `.badge`…) já os usam.

| No sistema de design | No pacote |
|---|---|
| Cor primária da marca | `tokens.brand_color`: o tom que dá contraste AA (4,5:1) com texto branco. Ele vira o passo 600 da escala `--color-cobalt-*` inteira (botões, links, foco, fundos suaves) e o núcleo gera as variantes do modo escuro |
| Escala de cinzas | `--color-ink-*` no `theme.css` |
| Cores de estado (sucesso, alerta, erro) | `--color-success*`, `--color-warning*`, `--color-danger*` no `theme.css` |
| Cor secundária ou de destaque | tokens próprios com o prefixo do projeto (por exemplo `--acme-accent`) usados em regras sobre as classes estáveis |
| Raio de controles e cartões | `tokens.radius` (`sharp`, `default`, `round`) ou `--radius-base` e `--radius-card` |
| Tipografia do corpo | uma entrada em `fonts` e `tokens.font_family` (vira a primeira família de `--font-sans`) |
| Tipografia dos títulos | outra entrada em `fonts` e uma regra como `h1, h2, .page-header h1 { font-family: "Aurora Display", var(--font-sans); }` |
| Sombras | `--shadow-card` e `--shadow-pop` |
| Modo escuro | `@media (prefers-color-scheme: dark) { :root { … } }` no `theme.css`; nunca `data-theme`, `[data-color-scheme]` nem script. Quando a pessoa força o modo claro ou escuro, o núcleo serve o mesmo `theme.css` com a consulta resolvida para a escolha dela (#665): o tema não precisa saber da preferência |
| Logo horizontal | `images.logo` (cabeçalho e e-mails) |
| Logo para fundo escuro | `images.logo_dark` |
| Símbolo / favicon | `images.icon` (PNG 512 × 512) |

O contrato estável para o CSS do tema são os tokens `--color-*`, `--radius-*`, `--font-*` e as classes de componente listadas em [UI](ui-design.md#marca-e-tema-da-instância). Classes utilitárias do Tailwind e a estrutura interna dos templates **não** são contrato: podem mudar entre versões.

Um `theme.css` mínimo:

```css
:root {
  --acme-accent: #2bb673;
  --color-ink-900: #0b1220;
}
.page-header h1 { font-family: "Aurora Sans", var(--font-sans); letter-spacing: -0.01em; }
.btn-primary { border-radius: 9999px; }
@media (prefers-color-scheme: dark) {
  :root { --acme-accent: #5fd39a; }
}
```

## Regras do CSS que pegam autores

O CSS do tema passa pela [barreira do CSS personalizado](appearance.md#css-personalizado). As que mais aparecem:

- `<` é proibido **até dentro de comentários**: `/* carregado depois do <style> */` é recusado. Escreva "depois do style da organização".
- `url(` é proibido em qualquer lugar: fontes vêm de `fonts` do manifesto e imagens de `images`, nunca do CSS. `@font-face` com `src: local("Nome")` passa, mas só funciona onde a fonte estiver instalada.
- `@import`, `@charset`, `@namespace`, `expression(`, `javascript:`, `behavior` e `-moz-binding` são recusados, também disfarçados com escapes.
- Até 32 KiB. A mensagem diz a linha: `theme.css: Linha 3: o CSS não pode usar url(.`

## Fontes

Só `.woff2`, até 8 por pacote, cada uma até 512 KiB, todas direto em `fonts/`. Converta `.ttf`/`.otf` para `.woff2` com uma ferramenta de sua escolha e confira que a licença permite auto-hospedagem (guarde-a em `LICENSES/`). Uma fonte variável declara a faixa de pesos (`"weight": "100 900"`); fontes estáticas declaram um peso cada (`"400"`, `"600"`). O núcleo gera o `@font-face` (com `font-display: swap`) apontando para `/brand/fonts/<arquivo>`; a IBM Plex Sans do núcleo continua como reserva na pilha.

## Logos e ícone

- Logo: SVG "simples" ou "otimizado" (sem `<script>`, sem `<style>`, sem imagens embutidas, sem referências externas, sem elementos de editor como `sodipodi:*`), ou PNG/WebP até 512 KB. A lista do que o SVG pode ter está em [logos em SVG](appearance.md#logos-em-svg).
- Logo escuro: a mesma marca para fundo escuro; só vale junto com o logo.
- Ícone: PNG quadrado de 512 × 512 até 256 KB. Ele vira o favicon e o ícone do manifesto PWA.
- E-mails sem organização usam o logo da instância só se for PNG, JPEG ou WebP: com logo SVG, o e-mail mostra o nome.

## Conferir e montar

```sh
python manage.py appearance check-package temas/aurora
python manage.py appearance build-package temas/aurora --output dist/aurora-1.0.0.zip
```

`check-package` aplica as mesmas regras da instalação (formato, contraste e compatibilidade com o núcleo instalado) e não abre o banco. A leitura do `.zip` faz streaming em blocos com teto real por arquivo e acumulado (6 MiB), taxa máxima de expansão sobre bytes reais e aceita apenas `ZIP_STORED` e `ZIP_DEFLATED` (#891). Com sucesso:

```text
Pacote: org.example.theme.aurora 1.0.0
Nome: Aurora
Cor: #1d4ed8 · cantos: default · fonte: Aurora Sans
Imagens: logo, logo escuro, ícone · fontes: 1 · CSS: 240 bytes
Nome do produto: Aurora
Digest: 9c1d…
Pacote válido.
```

Com erro, sai com 1 e diz o que corrigir: `ERRO unexpected_file: O arquivo images/rascunho.png não é declarado no theme.json; tire-o do pacote ou declare-o.`. `build-package` só grava o zip se a checagem passar, e o mesmo diretório gera sempre o mesmo zip.

## Publicar

Três caminhos, o mesmo formato:

1. **Envio manual**: em `/platform/appearance/`, "Instalar pacote de tema", escolha o `.zip` e envie. Instalar não ativa: pré-visualize ("Pré-visualizar", com as fontes e o logo do pacote) e, se estiver certo, "Ativar".
2. **Console**, para implantação automatizada: `python manage.py appearance install-package dist/aurora-1.0.0.zip`. É idempotente: o mesmo pacote não grava nada, uma versão maior atualiza o tema (e, se ele está ativo, as páginas mudam em até 5 segundos). Ative uma vez pela tela.
3. **Junto de uma app**: a app traz o tema no próprio pacote e ele aparece no catálogo quando a app é instalada; veja [publicar junto de uma app](#publicar-junto-de-uma-app).

Para atualizar, aumente `version` no `theme.json` e publique de novo. Para remover um pacote enviado à mão, ative outro tema e use "Remover" (ou `manage.py appearance remove-package <id>`). Se um CSS esconder a interface, as páginas de aparência continuam usáveis (carregam o tema sem o CSS livre) e, no servidor, `python manage.py appearance reset-theme` volta ao tema do núcleo.

## Publicar junto de uma app

Quando o tema é a identidade de um produto que é uma app (ou um conjunto de apps), entregue-o **com a app**: ele chega à instância junto com ela, aparece no catálogo de `/platform/appearance/` como "Da app …" e sai quando a app é desinstalada. Quando o tema é de uma instância específica, prefira o zip.

1. Ponha o diretório do tema dentro da app, por exemplo `themes/aurora/`, com a mesma estrutura do pacote.
2. No `theme.json`, use `id` igual a `<app_id>.theme.<slug>` (`org.example.app.theme.aurora`) e uma faixa `compatibility.skeleton` que contenha o menor núcleo que a app aceita.
3. Declare-o no manifesto da app (formato 2), com o piso `INSTANCE_THEMES_CORE_MINIMUM` em `compatibility.skeleton`:

   ```json
   "runtime": {"instance_themes": [{"slug": "aurora", "path": "themes/aurora"}]}
   ```

4. Uma app nova já sai com isso: `python manage.py skeleton_startapp acme_vendas --app-id org.example.vendas --publisher org.example --with-theme aurora` cria `themes/aurora/theme.json` e `theme.css` e a entrada no manifesto.
5. Na suíte da app, prove que o tema passa: `skeleton.testing.appearance.assert_app_themes_valid("acme_vendas")` roda as mesmas checagens do `skeleton.E047` (formato, contraste, faixa do núcleo e a regra do `id`) e lista todos os problemas.
6. `package doctor` e `package build` validam o tema; um tema inválido não entra num release.

Depois da instalação, o administrador pré-visualiza e ativa o tema (instalar a app **nunca** ativa o tema). Atualizar a app atualiza o tema. Desinstalar a app tira o tema do catálogo; se ele era o ativo, a instância volta ao tema do núcleo, e a prévia de desinstalação avisa antes. Um zip com o `id` de um tema de app é recusado: o tema de app é atualizado com a própria app. O exemplo `examples/themes/caderno/` serve para os dois caminhos: como zip, ou copiado para `themes/caderno/` de uma app com o `id` trocado.

## Versionar o tema no repositório do projeto

Guarde o **diretório** do pacote no repositório (por exemplo `temas/aurora/`), não o zip. No CI, rode `check-package` em cada mudança e `build-package` na publicação, anexando o zip ao release do projeto. Um tema montado na tela também pode ser trazido para o repositório: "Exportar" (ou `manage.py appearance export-theme`) gera o zip; descompacte-o no diretório, troque o `id` local (`local.theme.…`) pelo do projeto e versione.

## Erros comuns

| Mensagem (código) | Causa | Correção |
|---|---|---|
| `missing_manifest` | o zip tem uma pasta de topo (`aurora/theme.json`) | compacte o conteúdo da pasta, não a pasta; ou use `build-package` |
| `unexpected_file` | arquivo no pacote que o `theme.json` não declara (rascunho, `.DS_Store`) | tire o arquivo ou declare-o |
| `css` com "não pode conter <" | `<` num comentário do CSS | reescreva o comentário sem `<` |
| `css` com "não pode usar url(" | `@font-face` ou `background` com `url()` | declare a fonte em `fonts`; imagens só em `images` |
| `svg` | o SVG tem script, estilo, imagem embutida ou elementos de editor | exporte como SVG simples/otimizado |
| `brand_color` | a cor não dá AA com texto branco | use um tom mais escuro da marca para `brand_color` e o tom claro como token próprio |
| `icon` | o ícone não é PNG 512 × 512 | exporte o símbolo em 512 × 512 |
| `incompatible_core` | a faixa `compatibility.skeleton` não inclui o núcleo instalado | confira o tema na versão nova e ajuste a faixa |
| `same_version_changed` | mesma versão com outro conteúdo | aumente `version` |

## Não faça

- Não sobrescreva `skeleton/web/base.html` nem os outros templates do shell do núcleo para aplicar a marca: a cópia perde o que o núcleo acrescenta a cada versão, e `manage.py check` avisa com `skeleton.W046`. Só as páginas de erro podem ser sobrescritas.
- Não escreva cor, logo ou nome de produto fixos num template: use `instance_brand` e `site_name` ([extensão](extending.md)).
- Não carregue fonte, CSS ou imagem de CDN: a CSP da instância bloqueia qualquer recurso de outra origem.
- Não edite `skeleton/web/static/` nem `frontend/` do núcleo para trocar a marca.
- Não guarde o tema no SQLite da instância: marca e tema são dados do PostgreSQL ([aparência](appearance.md#modelo)).
