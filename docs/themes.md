# Criar e publicar um tema da instância

Este guia mostra como transformar um sistema de design (cores, tipografia, raios e logos) em um **pacote de tema** validado, instalado e ativo numa instância, sem alterar os templates do núcleo. Para a especificação completa do formato, das rotas e da precedência, consulte a referência em [aparência da instância](appearance.md). Agentes seguem o skill `instance-theme` (`.claude/skills/instance-theme/`), que resume este fluxo.

## Antes de começar

Um tema altera a **aparência**, não a estrutura da aplicação. Ele define tokens de cor, cantos e fonte, fontes próprias, CSS adicional sobre as classes de componente estáveis, logos (claro e escuro), ícone (favicon), nome do produto e rodapé. Ele não altera layout, menus, componentes nem textos das telas — se o sistema de design exigir esse tipo de mudança, o caminho é alterar o núcleo ou uma app, não criar um tema.

A precedência da marca é aplicada campo a campo nesta ordem:

1. **Organização** (nas telas e e-mails dela);
2. **Marca da instância** (o que o administrador definiu em `/platform/appearance/`);
3. **Tema ativo** (o que o pacote trouxe);
4. **Núcleo**.

Assim, um pacote preenche apenas os campos que a marca da instância deixou vazios e nunca sobrescreve o que o administrador configurou.

## Estrutura do pacote

O pacote é preparado em um diretório no repositório do projeto e compactado em um `.zip` com os arquivos diretamente na raiz:

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

Exemplo completo de `theme.json`:

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

- `id` é o identificador estável do pacote (letras minúsculas, números, `.`, `-` e `_`); `version` deve subir a cada publicação, pois a instância só aceita atualizações com versão maior para o mesmo `id`.
- `compatibility.skeleton` informa a faixa de versões do núcleo em que o tema foi validado.
- `tokens` define os parâmetros que o núcleo converte em CSS; `brand`, `images` e `fonts` compõem o restante da identidade.

Todas as regras de validação e limites de cada campo constam na tabela de [pacote de tema](appearance.md#pacote-de-tema).

## Do sistema de design para os tokens

A interface do núcleo é construída sobre tokens CSS: basta sobrescrevê-los no tema para que os componentes (`.btn`, `.card`, `.badge`…) adotem a nova identidade.

| No sistema de design | No pacote |
|---|---|
| Cor primária da marca | `tokens.brand_color`: tom que atinge contraste AA (4,5:1) com texto branco. Ele vira o passo 600 da escala `--color-cobalt-*` inteira (botões, links, foco e fundos suaves), e o núcleo gera as variantes do modo escuro |
| Escala de cinzas | `--color-ink-*` no `theme.css` |
| Cores de estado (sucesso, alerta, erro) | `--color-success*`, `--color-warning*`, `--color-danger*` no `theme.css` |
| Cor secundária ou de destaque | tokens próprios com prefixo do projeto (por exemplo `--acme-accent`), aplicados sobre as classes estáveis |
| Raio de controles e cartões | `tokens.radius` (`sharp`, `default`, `round`) ou `--radius-base` e `--radius-card` |
| Tipografia do corpo | uma entrada em `fonts` e `tokens.font_family` (passa a ser a primeira família de `--font-sans`) |
| Tipografia dos títulos | outra entrada em `fonts` e uma regra como `h1, h2, .page-header h1 { font-family: "Aurora Display", var(--font-sans); }` |
| Sombras | `--shadow-card` e `--shadow-pop` |
| Modo escuro | `@media (prefers-color-scheme: dark) { :root { … } }` no `theme.css`; nunca use `data-theme`, `[data-color-scheme]` nem script. Quando a pessoa força o modo claro ou escuro, o núcleo serve o mesmo `theme.css` com a media query resolvida para a escolha dela: o tema não precisa conhecer a preferência |
| Logo horizontal | `images.logo` (cabeçalho e e-mails) |
| Logo para fundo escuro | `images.logo_dark` |
| Símbolo / favicon | `images.icon` (PNG 512 × 512) |

O contrato estável para o CSS do tema compreende os tokens `--color-*`, `--radius-*`, `--font-*` e as classes de componente listadas em [UI](ui-design.md#marca-e-tema-da-instância). Classes utilitárias do Tailwind e a estrutura interna dos templates **não** fazem parte do contrato e podem mudar entre versões.

Exemplo mínimo de `theme.css`:

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

Todo CSS personalizado passa pela [barreira do CSS personalizado](appearance.md#css-personalizado). Os pontos que costumam surpreender autores são:

- `<` é proibido **inclusive dentro de comentários**: `/* carregado depois do <style> */` é recusado. Escreva "depois do style da organização".
- `url(` é proibido em qualquer posição: fontes devem ser declaradas em `fonts` e imagens em `images`, nunca no CSS. Regras `@font-face` com `src: local("Nome")` passam na validação, mas só surtem efeito se a fonte estiver instalada no dispositivo.
- `@import`, `@charset`, `@namespace`, `expression(`, `javascript:`, `behavior` e `-moz-binding` são recusados, mesmo ofuscados com escapes CSS.
- O limite é de 32 KiB. Em caso de erro, a mensagem aponta a linha exata: `theme.css: Linha 3: o CSS não pode usar url(.`

## Fontes

O pacote aceita apenas arquivos `.woff2`, até 8 por tema, cada um com até 512 KiB, posicionados diretamente em `fonts/`. Converta arquivos `.ttf` ou `.otf` para `.woff2` com a ferramenta de sua preferência e confirme que a licença permite auto-hospedagem (armazene os arquivos de licença em `LICENSES/`).

Fontes variáveis declaram a faixa de pesos (`"weight": "100 900"`); fontes estáticas declaram um peso por arquivo (`"400"`, `"600"`). O núcleo gera automaticamente o `@font-face` (com `font-display: swap`) apontando para `/brand/fonts/<arquivo>`, mantendo a IBM Plex Sans do núcleo como fallback na pilha tipográfica.

## Logos e ícone

- **Logo:** SVG "simples" ou "otimizado" (sem `<script>`, `<style>`, imagens embutidas, referências externas ou metadados de editor como `sodipodi:*`), ou PNG/JPEG/WebP de até 512 KB. Veja os elementos aceitos em [logos em SVG](appearance.md#logos-em-svg).
- **Logo escuro:** variante da mesma marca para fundo escuro; só é aceita quando `images.logo` também está presente.
- **Ícone:** PNG quadrado de exatamente 512 × 512 pixels e até 256 KB, usado como favicon e ícone do manifesto PWA.
- **E-mails:** e-mails fora do contexto de organização exibem o logo da instância apenas se for PNG, JPEG ou WebP; quando o logo é SVG, o e-mail mostra apenas o nome do produto.

## Conferir e montar

```sh
python manage.py appearance check-package temas/aurora
python manage.py appearance build-package temas/aurora --output dist/aurora-1.0.0.zip
```

`check-package` aplica exatamente as mesmas regras da instalação (formato, contraste e compatibilidade com o núcleo instalado) sem abrir conexão com o banco. A leitura do `.zip` faz streaming em blocos com limites reais por arquivo e acumulados (6 MiB), verifica a razão máxima de expansão sobre os bytes reais e aceita apenas `ZIP_STORED` e `ZIP_DEFLATED`. Quando o pacote está válido, a saída é:

```text
Pacote: org.example.theme.aurora 1.0.0
Nome: Aurora
Cor: #1d4ed8 · cantos: default · fonte: Aurora Sans
Imagens: logo, logo escuro, ícone · fontes: 1 · CSS: 240 bytes
Nome do produto: Aurora
Digest: 9c1d…
Pacote válido.
```

Se houver problema, o comando encerra com código 1 e indica a correção: `ERRO unexpected_file: O arquivo images/rascunho.png não é declarado no theme.json; tire-o do pacote ou declare-o.`. O comando `build-package` só grava o `.zip` se a validação passar e produz uma saída determinística (o mesmo diretório gera sempre os mesmos bytes).

## Publicar

O mesmo formato de pacote pode ser publicado por três caminhos:

1. **Envio manual:** em `/platform/appearance/`, clique em **Instalar pacote de tema**, selecione o `.zip` e envie. Instalar não ativa o tema automaticamente: use **Pré-visualizar** (já com as fontes e o logo do pacote) e, quando estiver pronto, clique em **Ativar**.
2. **Console (implantação automatizada):** execute `python manage.py appearance install-package dist/aurora-1.0.0.zip`. O comando é idempotente: reenviar o mesmo pacote não grava nada; uma versão maior atualiza o tema instalado (e, se ele estiver ativo, as páginas refletem a mudança em até 5 segundos). A ativação inicial é feita pela tela.
3. **Junto de uma app:** a app inclui o tema no próprio pacote e ele entra no catálogo assim que a app é instalada; veja [publicar junto de uma app](#publicar-junto-de-uma-app).

Para atualizar um tema, incremente `version` em `theme.json` e publique novamente. Para remover um pacote enviado manualmente, ative outro tema primeiro e use **Remover** (ou `manage.py appearance remove-package <id>`). Caso um CSS personalizado comprometa a usabilidade da interface, as telas de aparência continuam acessíveis porque carregam o tema em modo seguro (sem o CSS livre); no servidor, `python manage.py appearance reset-theme` restaura o tema do núcleo.

## Publicar junto de uma app

Quando o tema representa a identidade de um produto distribuído como app (ou conjunto de apps), empacote-o **junto com a app**. Ele chega à instância com a instalação da app, aparece em `/platform/appearance/` marcado como "Da app …" e sai do catálogo quando a app é desinstalada. Para temas específicos de uma única instância, prefira o pacote `.zip` avulso.

1. Coloque o diretório do tema dentro da app (por exemplo, `themes/aurora/`), seguindo a mesma estrutura de um pacote.
2. Em `theme.json`, defina `id` como `<app_id>.theme.<slug>` (`org.example.app.theme.aurora`) e uma faixa `compatibility.skeleton` que inclua a menor versão do núcleo aceita pela app.
3. Declare o tema no manifesto da app (formato 2), exigindo o piso `INSTANCE_THEMES_CORE_MINIMUM` em `compatibility.skeleton`:

   ```json
   "runtime": {"instance_themes": [{"slug": "aurora", "path": "themes/aurora"}]}
   ```

4. O gerador já cria essa estrutura pronta: `python manage.py skeleton_startapp acme_vendas --app-id org.example.vendas --publisher org.example --with-theme aurora` gera `themes/aurora/theme.json`, `theme.css` e a entrada correspondente no manifesto.
5. Na suíte de testes da app, valide o tema com `skeleton.testing.appearance.assert_app_themes_valid("acme_vendas")`, que executa as mesmas verificações do check `skeleton.E047` (formato, contraste, faixa do núcleo e regra do `id`) e lista todos os problemas.
6. Tanto `package doctor` quanto `package build` validam os temas declarados e impedem que um tema inválido chegue a um release.

Após a instalação da app, o administrador pré-visualiza e ativa o tema manualmente (instalar a app **nunca** ativa o tema). Atualizar a app atualiza seus temas; desinstalá-la remove o tema do catálogo (e, se ele estiver ativo, a instância volta ao tema do núcleo, com aviso prévio na prévia de desinstalação). Um `.zip` avulso com o mesmo `id` de um tema de app é recusado, pois esse tema deve evoluir junto com a própria app. O exemplo `examples/themes/caderno/` ilustra os dois modos: pode ser empacotado como `.zip` ou copiado para `themes/caderno/` de uma app com o `id` ajustado.

## Versionar o tema no repositório do projeto

Versione sempre o **diretório** de origem do tema no repositório (por exemplo, `temas/aurora/`), nunca o `.zip` gerado. No CI, execute `check-package` a cada alteração e `build-package` na publicação, anexando o `.zip` ao release do projeto.

Também é possível trazer um tema criado pela interface para o repositório: use **Exportar** (ou `manage.py appearance export-theme`), descompacte o `.zip` no diretório desejado, substitua o `id` local (`local.theme.…`) pelo identificador do projeto e versione os arquivos.

## Erros comuns

| Mensagem (código) | Causa | Correção |
|---|---|---|
| `missing_manifest` | o zip tem uma pasta de topo (`aurora/theme.json`) | compacte o conteúdo da pasta, não a pasta; ou use `build-package` |
| `unexpected_file` | arquivo no pacote que o `theme.json` não declara (rascunho, `.DS_Store`) | remova o arquivo ou declare-o |
| `css` com "não pode conter <" | `<` num comentário do CSS | reescreva o comentário sem `<` |
| `css` com "não pode usar url(" | `@font-face` ou `background` com `url()` | declare a fonte em `fonts`; imagens entram apenas em `images` |
| `svg` | o SVG tem script, estilo, imagem embutida ou elementos de editor | exporte como SVG simples ou otimizado |
| `brand_color` | a cor não atinge contraste AA com texto branco | use um tom mais escuro da marca em `brand_color` e deixe o tom claro como token próprio |
| `icon` | o ícone não é PNG 512 × 512 | exporte o símbolo em PNG 512 × 512 |
| `incompatible_core` | a faixa `compatibility.skeleton` não inclui o núcleo instalado | valide o tema na versão nova e ajuste a faixa |
| `same_version_changed` | mesma versão com conteúdo diferente | incremente `version` |

## Não faça

- Não sobrescreva `skeleton/web/base.html` nem os demais templates do shell do núcleo para aplicar a marca: cópias locais perdem evoluções do núcleo e `manage.py check` alerta com `skeleton.W046`. Apenas as páginas de erro podem ser sobrescritas.
- Não fixe cor, logo ou nome de produto diretamente em templates: use `instance_brand` e `site_name` ([extensão](extending.md)).
- Não carregue fontes, CSS ou imagens de CDN: a CSP da instância bloqueia recursos de outra origem.
- Não edite `skeleton/web/static/` nem `frontend/` do núcleo para trocar a marca.
- Não armazene o tema no SQLite da instância: marca e tema são dados do PostgreSQL ([aparência](appearance.md#modelo)).
