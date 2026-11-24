// HTML colado no modo visual (#825), antes de o schema decidir o que fica.
//
// Processadores de texto marcam negrito e itálico com `style` (o Google Docs usa
// <span style="font-weight:700">). A CSP da página (style-src sem 'unsafe-inline') bloqueia esses
// atributos ao ler o HTML, então a formatação se perderia e cada colagem registraria violações.
// Aqui `style` (e os `on*`, que também seriam relatados) é renomeado antes de qualquer leitura; o
// estilo vira <strong>, <em>, <u> ou <s>, e todo atributo fora de href, title e start sai. O schema continua sendo quem decide o que
// sobrevive: link só https:// ou caminho local, e nada de imagem, tabela, script ou estilo.
const INLINE_ATTRIBUTE = /(\s)(style|on[a-z]+)\s*=\s*("[^"]*"|'[^']*'|[^\s>]+)/gi;
const KEPT = new Set(["href", "title", "start"]);
const WRAPPERS = [
  ["strong", /font-weight\s*:\s*(bold|bolder|[6-9]00)\b/],
  ["em", /font-style\s*:\s*(italic|oblique)\b/],
  ["u", /text-decoration(-line)?\s*:[^;]*\bunderline\b/],
  ["s", /text-decoration(-line)?\s*:[^;]*\bline-through\b/],
];
const NOT_BOLD = /font-weight\s*:\s*(normal|lighter|[1-4]00)\b/;

export function normalizePastedHTML(html) {
  const doc = new DOMParser().parseFromString(html.replace(INLINE_ATTRIBUTE, "$1data-pasted-$2=$3"), "text/html");
  for (const element of [...doc.body.querySelectorAll("*")]) {
    const style = (element.getAttribute("data-pasted-style") || "").toLowerCase();
    for (const name of element.getAttributeNames()) if (!KEPT.has(name)) element.removeAttribute(name);
    // O Google Docs envolve tudo num <b style="font-weight:normal">, que não é negrito.
    if (element.tagName === "B" && NOT_BOLD.test(style)) {
      element.replaceWith(...element.childNodes);
      continue;
    }
    for (const [tag, pattern] of WRAPPERS) {
      if (!pattern.test(style)) continue;
      const wrapper = doc.createElement(tag);
      wrapper.append(...element.childNodes);
      element.append(wrapper);
    }
  }
  return doc.body.innerHTML;
}
