// Os comandos do modo Markdown (#825): funções do estado do CodeMirror que devolvem a transação.
//
// Ficam fora de commands.js para o CodeMirror só ser carregado quando a pessoa abre o modo
// Markdown (source-mode.js, carregado por import() dinâmico). O registro em commands.js aponta
// para estas funções pelo nome em SOURCE_COMMANDS.
import { syntaxTree } from "@codemirror/language";
import { EditorSelection } from "@codemirror/state";

const INLINE_NODES = {
  bold: { node: "StrongEmphasis", marker: "**" },
  italic: { node: "Emphasis", marker: "*" },
  underline: { node: "Underline", marker: "++" },
  strike: { node: "Strikethrough", marker: "~~" },
  code: { node: "InlineCode", marker: "`" },
};

function ancestors(state, pos, side = -1) {
  const names = [];
  for (let node = syntaxTree(state).resolveInner(pos, side); node; node = node.parent) names.push(node);
  return names;
}

function findAncestor(state, range, name) {
  for (const side of [1, -1]) {
    for (const node of ancestors(state, side === 1 ? range.from : range.to, side)) {
      if (node.name === name && node.from <= range.from && node.to >= range.to) return node;
    }
  }
  return null;
}

/** Liga ou desliga uma marca em linha: tira os marcadores do trecho já marcado, ou envolve a seleção. */
export function toggleInlineSource(kind) {
  const { node: name, marker } = INLINE_NODES[kind];
  return state =>
    state.changeByRange(range => {
      const found = findAncestor(state, range, name);
      if (found) {
        const open = found.firstChild;
        const close = found.lastChild;
        const openLength = open && open.name.endsWith("Mark") ? open.to - open.from : marker.length;
        const closeLength = close && close.name.endsWith("Mark") && close !== open ? close.to - close.from : marker.length;
        const changes = [
          { from: found.from, to: found.from + openLength },
          { from: found.to - closeLength, to: found.to },
        ];
        const map = pos => Math.min(Math.max(pos - openLength, found.from), found.to - openLength - closeLength);
        return { changes, range: EditorSelection.range(map(range.anchor), map(range.head)) };
      }
      if (range.empty) {
        return {
          changes: { from: range.from, insert: marker + marker },
          range: EditorSelection.cursor(range.from + marker.length),
        };
      }
      // Os marcadores ficam colados ao texto: "**a **" não seria lido como negrito.
      const text = state.sliceDoc(range.from, range.to);
      const from = range.from + (text.length - text.trimStart().length);
      const to = range.to - (text.length - text.trimEnd().length);
      if (from >= to) return { range };
      const lines = [];
      for (let pos = from; pos <= to; ) {
        const line = state.doc.lineAt(pos);
        const start = Math.max(line.from, from);
        const end = Math.min(line.to, to);
        const segment = state.sliceDoc(start, end);
        const lead = segment.length - segment.trimStart().length;
        const trail = segment.length - segment.trimEnd().length;
        if (end - trail > start + lead) lines.push([start + lead, end - trail]);
        pos = line.to + 1;
      }
      const changes = lines.flatMap(([a, b]) => [
        { from: a, insert: marker },
        { from: b, insert: marker },
      ]);
      const added = lines.length * 2 * marker.length;
      return { changes, range: EditorSelection.range(from + marker.length, to + added - marker.length) };
    });
}

const LINE_PREFIX = {
  bullet_list: /^(\s*)([-+*])\s+/,
  ordered_list: /^(\s*)(\d+)[.)]\s+/,
  quote: /^(\s*)>\s?/,
  heading: /^(\s*)(#{1,6})\s+/,
};

function selectedLines(state) {
  const lines = [];
  const seen = new Set();
  for (const range of state.selection.ranges) {
    for (let pos = range.from; pos <= range.to; ) {
      const line = state.doc.lineAt(pos);
      if (!seen.has(line.number)) {
        seen.add(line.number);
        lines.push(line);
      }
      pos = line.to + 1;
    }
  }
  return lines;
}

/** Liga ou desliga um prefixo de bloco (lista, citação, título) em cada linha selecionada. */
export function toggleBlockSource(kind, level = 2) {
  return state => {
    const lines = selectedLines(state).filter((line, index, all) => line.length || all.length === 1);
    const has = line => {
      const match = LINE_PREFIX[kind].exec(line.text);
      return Boolean(match) && (kind !== "heading" || match[2].length === level);
    };
    const remove = lines.every(has);
    const changes = [];
    lines.forEach((line, index) => {
      let indent = /^\s*/.exec(line.text)[0];
      let from = line.from;
      for (const pattern of kind === "quote" ? [LINE_PREFIX.quote] : [LINE_PREFIX.bullet_list, LINE_PREFIX.ordered_list, LINE_PREFIX.heading]) {
        const match = pattern.exec(line.text);
        if (match && (kind !== "quote" || remove)) {
          indent = match[1];
          changes.push({ from: line.from, to: line.from + match[0].length, insert: indent });
          from = null;
          break;
        }
      }
      if (remove) return;
      const prefix = { bullet_list: "- ", ordered_list: `${index + 1}. `, quote: "> ", heading: `${"#".repeat(level)} ` }[kind];
      if (from === null) changes[changes.length - 1].insert = indent + prefix;
      else changes.push({ from: line.from + indent.length, insert: prefix });
    });
    return { changes };
  };
}

/** O link sob a seleção no modo Markdown: o trecho, o rótulo e o endereço. */
export function linkAtSource(state) {
  const range = state.selection.main;
  const node = findAncestor(state, range, "Link");
  if (!node) return { from: range.from, to: range.to, text: state.sliceDoc(range.from, range.to), href: "" };
  const url = node.getChild("URL");
  const marks = node.getChildren("LinkMark");
  const textEnd = marks.length > 1 ? marks[1].from : node.to;
  return {
    from: node.from,
    to: node.to,
    text: state.sliceDoc(node.from + 1, textEnd),
    href: url ? state.sliceDoc(url.from, url.to).replace(/^<|>$/g, "") : "",
    existing: true,
  };
}

function escapeLabel(text) {
  return text.replace(/[\[\]\\]/g, "\\$&");
}

function linkDestination(href) {
  return /[\s<>]/.test(href) ? `<${href.replace(/[<>]/g, encodeURIComponent)}>` : href.replace(/[()]/g, "\\$&");
}

export function setLinkSource(state, link, href, text) {
  const label = text || link.text || href;
  const insert = href ? `[${escapeLabel(label)}](${linkDestination(href)})` : link.text;
  return {
    changes: { from: link.from, to: link.to, insert },
    selection: EditorSelection.cursor(link.from + insert.length),
  };
}

const ACTIVE_NODES = {
  bold: ["StrongEmphasis"],
  italic: ["Emphasis"],
  underline: ["Underline"],
  strike: ["Strikethrough"],
  code: ["InlineCode"],
  heading: ["ATXHeading1", "ATXHeading2", "ATXHeading3", "ATXHeading4", "ATXHeading5", "ATXHeading6"],
  quote: ["Blockquote"],
  link: ["Link"],
};

export function activeSource(state) {
  const active = new Set();
  const nodes = ancestors(state, state.selection.main.head);
  for (const [command, names] of Object.entries(ACTIVE_NODES)) {
    if (nodes.some(node => names.includes(node.name))) active.add(command);
  }
  const list = nodes.find(node => node.name === "BulletList" || node.name === "OrderedList");
  if (list) active.add(list.name === "BulletList" ? "bullet_list" : "ordered_list");
  return active;
}


export const SOURCE_COMMANDS = {
  bold: toggleInlineSource("bold"),
  italic: toggleInlineSource("italic"),
  underline: toggleInlineSource("underline"),
  strike: toggleInlineSource("strike"),
  code: toggleInlineSource("code"),
  heading: toggleBlockSource("heading", 2),
  heading1: toggleBlockSource("heading", 1),
  heading3: toggleBlockSource("heading", 3),
  bullet_list: toggleBlockSource("bullet_list"),
  ordered_list: toggleBlockSource("ordered_list"),
  quote: toggleBlockSource("quote"),
};
