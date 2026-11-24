// Markdown ⇄ documento do modo visual (#825), com o markdown-it do dialeto.
//
// O serializador escreve a forma canônica do dialeto: `**negrito**`, `*itálico*`, `++sub++`,
// `~~tachado~~`, `- item`, `1. item`, e uma quebra de linha simples para cada quebra (o dialeto
// usa `breaks`). O texto digitado é escapado para não virar marcação ao ser lido de novo.
import { MarkdownParser, MarkdownSerializer, defaultMarkdownSerializer } from "prosemirror-markdown";
import { Slice } from "prosemirror-model";

import { createMarkdownIt } from "./dialect.js";

function listIsTight(tokens, index) {
  while (++index < tokens.length) if (tokens[index].type !== "list_item_open") return tokens[index].hidden;
  return false;
}

export function createParser(schema, features) {
  const tokens = {
    paragraph: { block: "paragraph" },
    hardbreak: { node: "hard_break" },
    softbreak: { node: "hard_break" },
  };
  const { nodes, marks } = schema;
  if (nodes.heading) tokens.heading = { block: "heading", getAttrs: token => ({ level: Number(token.tag.slice(1)) }) };
  if (nodes.blockquote) tokens.blockquote = { block: "blockquote" };
  if (nodes.list_item) {
    tokens.list_item = { block: "list_item" };
    tokens.bullet_list = { block: "bullet_list", getAttrs: (_, all, index) => ({ tight: listIsTight(all, index) }) };
    tokens.ordered_list = {
      block: "ordered_list",
      getAttrs: (token, all, index) => ({ order: Number(token.attrGet("start")) || 1, tight: listIsTight(all, index) }),
    };
  }
  if (marks.em) {
    tokens.em = { mark: "em" };
    tokens.strong = { mark: "strong" };
  }
  if (marks.underline) tokens.u = { mark: "underline" };
  if (marks.strike) tokens.s = { mark: "strike" };
  if (marks.code) tokens.code_inline = { mark: "code", noCloseToken: true };
  if (marks.link) {
    tokens.link = { mark: "link", getAttrs: token => ({ href: token.attrGet("href"), title: token.attrGet("title") || null }) };
  }
  return new MarkdownParser(schema, createMarkdownIt(features), tokens);
}

/** O texto colado (sem HTML) é lido como Markdown do dialeto. */
export function textToSlice(parser, text) {
  const doc = parser.parse(text);
  return Slice.maxOpen(doc.content);
}

function sameTypeRun(node, parent, index) {
  let run = 0;
  for (let i = index - 1; i >= 0 && parent.child(i).type === node.type; i--) run += 1;
  return run;
}

function linkDestination(href) {
  return /[\s<>]/.test(href) ? `<${href.replace(/[<>]/g, encodeURIComponent)}>` : href.replace(/[()]/g, "\\$&");
}

export function createSerializer(schema) {
  const base = defaultMarkdownSerializer.nodes;
  const nodes = {
    paragraph: base.paragraph,
    text: base.text,
    hard_break(state, node, parent, index) {
      for (let i = index + 1; i < parent.childCount; i++) {
        if (parent.child(i).type !== node.type) {
          // Com `breaks`, uma quebra é só a troca de linha; duas seguidas precisam do `\`, senão
          // a linha em branco viraria outro parágrafo.
          const afterBreak = index > 0 && parent.child(index - 1).type === node.type;
          state.write(afterBreak ? "\\\n" : "\n");
          return;
        }
      }
    },
  };
  if (schema.nodes.heading) nodes.heading = base.heading;
  if (schema.nodes.blockquote) nodes.blockquote = base.blockquote;
  if (schema.nodes.list_item) {
    nodes.list_item = base.list_item;
    // Duas listas seguidas do mesmo tipo trocam de marcador, senão seriam lidas como uma só.
    nodes.bullet_list = (state, node, parent, index) => {
      const bullet = sameTypeRun(node, parent, index) % 2 ? "* " : "- ";
      state.renderList(node, "  ", () => bullet);
    };
    nodes.ordered_list = (state, node, parent, index) => {
      const delimiter = sameTypeRun(node, parent, index) % 2 ? ")" : ".";
      const start = node.attrs.order || 1;
      const width = String(start + node.childCount - 1).length;
      state.renderList(node, state.repeat(" ", width + 2), i => {
        const number = String(start + i);
        return state.repeat(" ", width - number.length) + number + delimiter + " ";
      });
    };
  }
  const marks = {};
  if (schema.marks.link) {
    marks.link = {
      open: "[",
      close: (_, mark) => `](${linkDestination(mark.attrs.href)}${mark.attrs.title ? ` ${JSON.stringify(mark.attrs.title)}` : ""})`,
    };
  }
  if (schema.marks.em) {
    marks.em = { open: "*", close: "*", mixable: true, expelEnclosingWhitespace: true };
    marks.strong = { open: "**", close: "**", mixable: true, expelEnclosingWhitespace: true };
  }
  if (schema.marks.underline) marks.underline = { open: "++", close: "++", mixable: true, expelEnclosingWhitespace: true };
  if (schema.marks.strike) marks.strike = { open: "~~", close: "~~", mixable: true, expelEnclosingWhitespace: true };
  if (schema.marks.code) marks.code = defaultMarkdownSerializer.marks.code;
  // Além do que o serializador já escapa: entidades (`&amp;` digitado continua `&amp;`), `++`
  // (sublinhado) e `1)` no início da linha (lista numerada).
  const extra = ["&(?=#?[0-9A-Za-z]+;)"];
  if (schema.marks.underline) extra.push("\\+(?=\\+)");
  if (schema.nodes.list_item) extra.push("(?<=^\\s*\\d+)\\)(?=\\s|$)");
  return new MarkdownSerializer(nodes, marks, {
    escapeExtraCharacters: new RegExp(extra.join("|"), "g"),
    tightLists: true,
  });
}
