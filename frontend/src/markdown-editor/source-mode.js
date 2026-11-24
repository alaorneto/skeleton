// O modo Markdown (#825): CodeMirror 6 com os marcadores à vista, já formatados.
//
// O realce usa classes (`tok-*`, estilizadas em frontend/src/site.css), nunca estilo injetado:
// em `**texto**` os marcadores e o texto recebem `tok-strong`, e os marcadores também `tok-mark`
// (esmaecidos). O analisador do realce só reconhece os recursos do campo, como o do servidor.
import { history, historyKeymap, defaultKeymap } from "@codemirror/commands";
import { Language, defineLanguageFacet, syntaxHighlighting } from "@codemirror/language";
import { EditorSelection, EditorState } from "@codemirror/state";
import { EditorView, keymap } from "@codemirror/view";
import { Tag, tagHighlighter, tags } from "@lezer/highlight";
import { Strikethrough, parser as commonmark } from "@lezer/markdown";

// Um só import() dinâmico traz o editor e os comandos do modo Markdown.
export * from "./source-commands.js";

export const underlineTag = Tag.define();
const PUNCTUATION = /[\p{S}\p{P}]/u;
const PLUS = 43;
const UnderlineDelimiter = { resolve: "Underline", mark: "UnderlineMark" };
const Underline = {
  defineNodes: [
    { name: "Underline", style: { "Underline/...": underlineTag } },
    { name: "UnderlineMark", style: tags.processingInstruction },
  ],
  parseInline: [
    {
      name: "Underline",
      parse(cx, next, pos) {
        if (next !== PLUS || cx.char(pos + 1) !== PLUS) return -1;
        const before = cx.slice(pos - 1, pos);
        const after = cx.slice(pos + 2, pos + 3);
        const spaceBefore = /\s|^$/.test(before);
        const spaceAfter = /\s|^$/.test(after);
        const punctuationBefore = PUNCTUATION.test(before);
        const punctuationAfter = PUNCTUATION.test(after);
        return cx.addDelimiter(
          UnderlineDelimiter,
          pos,
          pos + 2,
          !spaceAfter && (!punctuationAfter || spaceBefore || punctuationBefore),
          !spaceBefore && (!punctuationBefore || spaceAfter || punctuationAfter),
        );
      },
      after: "Emphasis",
    },
  ],
};

// O que o dialeto nunca tem, e o que só existe com o recurso.
const ALWAYS_REMOVED = ["LinkReference", "IndentedCode", "FencedCode", "HorizontalRule", "HTMLBlock", "SetextHeading", "HTMLTag", "Image"];
const FEATURE_PARSERS = {
  emphasis: ["Emphasis"],
  code: ["InlineCode"],
  heading: ["ATXHeading"],
  lists: ["BulletList", "OrderedList"],
  quote: ["Blockquote"],
  link: ["Link", "LinkEnd"],
};

export function sourceLanguage(features) {
  const on = new Set(features);
  const remove = [...ALWAYS_REMOVED];
  for (const [feature, names] of Object.entries(FEATURE_PARSERS)) if (!on.has(feature)) remove.push(...names);
  const extensions = [{ remove }];
  if (on.has("strike")) extensions.push(Strikethrough);
  if (on.has("underline")) extensions.push(Underline);
  return new Language(languageData, commonmark.configure(extensions), [], "markdown");
}

const languageData = defineLanguageFacet({});
const CONTINUED = /^(\s*)(?:([-+*])|(\d+)([.)])|(>))(\s+)/;

/** Enter numa linha de lista ou citação continua o prefixo; numa linha só com o prefixo, o tira. */
export function continueMarkup(state) {
  let handled = false;
  const spec = state.changeByRange(range => {
    const line = state.doc.lineAt(range.head);
    const match = range.empty ? CONTINUED.exec(line.text) : null;
    if (!match || range.head < line.from + match[0].length) return { range };
    handled = true;
    if (line.text.trim() === match[0].trim()) {
      return { changes: { from: line.from, to: line.to }, range: EditorSelection.cursor(line.from) };
    }
    const [, indent, bullet, number, delimiter, quote, space] = match;
    const prefix = indent + (bullet || (number ? `${Number(number) + 1}${delimiter}` : quote)) + space;
    const insert = state.lineBreak + prefix;
    return { changes: { from: range.head, insert }, range: EditorSelection.cursor(range.head + insert.length) };
  });
  return handled ? spec : null;
}

export const highlighter = tagHighlighter([
  { tag: tags.strong, class: "tok-strong" },
  { tag: tags.emphasis, class: "tok-em" },
  { tag: underlineTag, class: "tok-u" },
  { tag: tags.strikethrough, class: "tok-s" },
  { tag: tags.monospace, class: "tok-code" },
  { tag: tags.heading1, class: "tok-h1" },
  { tag: tags.heading2, class: "tok-h2" },
  { tag: [tags.heading3, tags.heading4, tags.heading5, tags.heading6], class: "tok-h3" },
  { tag: tags.quote, class: "tok-quote" },
  { tag: tags.link, class: "tok-link" },
  { tag: tags.url, class: "tok-url" },
  { tag: tags.processingInstruction, class: "tok-mark" },
  { tag: tags.escape, class: "tok-mark" },
]);

// O tema do editor usa os tokens do tema da instância (variáveis CSS), no claro e no escuro.
// O CodeMirror o instala numa folha adotada (CSSOM), que a CSP permite (frontend/scripts/build.mjs).
const theme = EditorView.theme({
  "&": { backgroundColor: "transparent", color: "var(--color-heading)" },
  "&.cm-focused": { outline: "none" },
  ".cm-scroller": { fontFamily: "var(--font-mono)", lineHeight: "1.5rem" },
  ".cm-content": {
    padding: "0.75rem",
    fontSize: "0.875rem",
    minHeight: "var(--markdown-min-height)",
    caretColor: "var(--color-heading)",
  },
  ".cm-line": { padding: "0" },
  ".cm-cursor, .cm-dropCursor": { borderLeftColor: "var(--color-heading)" },
  "&.cm-focused > .cm-scroller > .cm-selectionLayer .cm-selectionBackground, .cm-selectionBackground, ::selection": {
    backgroundColor: "var(--color-brand-soft)",
  },
});

export function createSourceEditor({ parent, doc, features, keys, contentAttributes, onChange, onUpdate }) {
  const state = EditorState.create({
    doc,
    extensions: [
      history(),
      EditorView.lineWrapping,
      theme,
      sourceLanguage(features),
      syntaxHighlighting(highlighter),
      keymap.of([
        ...keys,
        { key: "Enter", run: view => runSource(view, continueMarkup) },
        ...defaultKeymap,
        ...historyKeymap,
      ]),
      EditorView.contentAttributes.of(contentAttributes),
      EditorView.updateListener.of(update => {
        if (update.docChanged) onChange(update.state.doc.toString());
        if (update.docChanged || update.selectionSet || update.focusChanged) onUpdate();
      }),
    ],
  });
  return new EditorView({ state, parent });
}

/** Executa um comando do modo Markdown (função do estado que devolve a transação). */
export function runSource(view, command) {
  const spec = command(view.state);
  if (!spec) return false;
  view.dispatch(view.state.update(spec, { scrollIntoView: true, userEvent: "input.format" }));
  return true;
}
