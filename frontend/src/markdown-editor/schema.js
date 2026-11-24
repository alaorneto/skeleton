// O schema do modo visual (ProseMirror), montado só com os recursos do campo (#825).
//
// Um nó ou marca que o campo não aceita não existe: colar HTML ou digitar um atalho não o
// cria. As regras parseDOM decidem o que sobrevive a uma colagem de HTML; o resto vira texto.
// Formatação por `style` chega aqui já convertida em tags (paste.js).
import { Schema } from "prosemirror-model";

import { LINK_REL, MAX_HEADING_LEVEL, isAllowedLink } from "./dialect.js";

export function buildSchema(features) {
  const on = new Set(features);
  const nodes = {
    doc: { content: "block+" },
    paragraph: { content: "inline*", group: "block", parseDOM: [{ tag: "p" }], toDOM: () => ["p", 0] },
    text: { group: "inline" },
    hard_break: {
      inline: true,
      group: "inline",
      selectable: false,
      parseDOM: [{ tag: "br" }],
      toDOM: () => ["br"],
    },
  };
  if (on.has("heading")) {
    nodes.heading = {
      attrs: { level: { default: 2 } },
      content: "inline*",
      group: "block",
      defining: true,
      parseDOM: [1, 2, 3, 4, 5, 6].map(level => ({ tag: `h${level}`, attrs: { level: Math.min(level, MAX_HEADING_LEVEL) } })),
      toDOM: node => [`h${node.attrs.level}`, 0],
    };
  }
  if (on.has("quote")) {
    nodes.blockquote = {
      content: "block+",
      group: "block",
      defining: true,
      parseDOM: [{ tag: "blockquote" }],
      toDOM: () => ["blockquote", 0],
    };
  }
  if (on.has("lists")) {
    nodes.bullet_list = {
      content: "list_item+",
      group: "block",
      attrs: { tight: { default: true } },
      parseDOM: [{ tag: "ul" }],
      toDOM: () => ["ul", 0],
    };
    nodes.ordered_list = {
      content: "list_item+",
      group: "block",
      attrs: { order: { default: 1 }, tight: { default: true } },
      parseDOM: [
        {
          tag: "ol",
          getAttrs: dom => ({ order: dom.hasAttribute("start") ? Number(dom.getAttribute("start")) || 1 : 1 }),
        },
      ],
      toDOM: node => ["ol", node.attrs.order === 1 ? {} : { start: node.attrs.order }, 0],
    };
    nodes.list_item = {
      content: "paragraph block*",
      defining: true,
      parseDOM: [{ tag: "li" }],
      toDOM: () => ["li", 0],
    };
  }

  const marks = {};
  if (on.has("link")) {
    marks.link = {
      attrs: { href: {}, title: { default: null } },
      inclusive: false,
      parseDOM: [
        {
          tag: "a[href]",
          getAttrs: dom => {
            const href = dom.getAttribute("href");
            return isAllowedLink(href) ? { href, title: dom.getAttribute("title") || null } : false;
          },
        },
      ],
      toDOM: mark => ["a", { href: mark.attrs.href, title: mark.attrs.title, rel: LINK_REL }, 0],
    };
  }
  if (on.has("emphasis")) {
    marks.em = {
      parseDOM: [{ tag: "i" }, { tag: "em" }],
      toDOM: () => ["em", 0],
    };
    marks.strong = {
      parseDOM: [{ tag: "strong" }, { tag: "b" }],
      toDOM: () => ["strong", 0],
    };
  }
  if (on.has("underline")) {
    marks.underline = {
      parseDOM: [{ tag: "u" }, { tag: "ins" }],
      toDOM: () => ["u", 0],
    };
  }
  if (on.has("strike")) {
    marks.strike = {
      parseDOM: [{ tag: "s" }, { tag: "del" }, { tag: "strike" }],
      toDOM: () => ["s", 0],
    };
  }
  if (on.has("code")) {
    marks.code = { code: true, parseDOM: [{ tag: "code" }], toDOM: () => ["code", 0] };
  }
  return new Schema({ nodes, marks });
}
