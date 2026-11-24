// O registro de comandos da faixa de opções (#825).
//
// Cada comando pertence a um recurso do dialeto, tem um atalho e duas implementações: no modo
// Markdown ele escreve ou tira os marcadores (SOURCE_COMMANDS em source-commands.js, pelo mesmo
// nome, carregado só com o modo Markdown); no modo visual (`visual`) aplica a formatação.
//
// Para acrescentar um recurso: a regra no dialeto (dialect.js e skeleton/common/markdown.py), o
// nó ou a marca em schema.js, a leitura e a escrita em conversion.js, o comando aqui e em
// source-commands.js, e o botão em TOOL_GROUPS de skeleton/web/markdown_widget.py.
import { lift, setBlockType, toggleMark, wrapIn } from "prosemirror-commands";
import { liftListItem, wrapInList } from "prosemirror-schema-list";

// ---------------------------------------------------------------- modo visual (ProseMirror)

function markActive(state, type) {
  const { from, $from, to, empty } = state.selection;
  if (empty) return Boolean(type.isInSet(state.storedMarks || $from.marks()));
  return state.doc.rangeHasMark(from, to, type);
}

function ancestorOf(state, predicate) {
  const { $from } = state.selection;
  for (let depth = $from.depth; depth > 0; depth--) {
    const node = $from.node(depth);
    if (predicate(node)) return { node, depth, pos: $from.before(depth) };
  }
  return null;
}

function toggleHeading(schema, level) {
  return (state, dispatch) => {
    const { $from } = state.selection;
    if ($from.parent.type === schema.nodes.heading && $from.parent.attrs.level === level) {
      return setBlockType(schema.nodes.paragraph)(state, dispatch);
    }
    return setBlockType(schema.nodes.heading, { level })(state, dispatch);
  };
}

function toggleList(schema, type) {
  const lists = [schema.nodes.bullet_list, schema.nodes.ordered_list];
  return (state, dispatch) => {
    const found = ancestorOf(state, node => lists.includes(node.type));
    if (found && found.node.type === type) return liftListItem(schema.nodes.list_item)(state, dispatch);
    if (found) {
      if (dispatch) dispatch(state.tr.setNodeMarkup(found.pos, type, { tight: found.node.attrs.tight }));
      return true;
    }
    return wrapInList(type)(state, dispatch);
  };
}

function toggleQuote(schema) {
  return (state, dispatch) => {
    if (ancestorOf(state, node => node.type === schema.nodes.blockquote)) return lift(state, dispatch);
    return wrapIn(schema.nodes.blockquote)(state, dispatch);
  };
}

function markRange($pos, type) {
  const parent = $pos.parent;
  let found = parent.childAfter($pos.parentOffset);
  if (!found.node || !type.isInSet(found.node.marks)) found = parent.childBefore($pos.parentOffset);
  const mark = found.node && type.isInSet(found.node.marks);
  if (!mark) return null;
  let from = $pos.start() + found.offset;
  let to = from + found.node.nodeSize;
  for (let i = found.index - 1; i >= 0 && mark.isInSet(parent.child(i).marks); i--) from -= parent.child(i).nodeSize;
  for (let i = found.index + 1; i < parent.childCount && mark.isInSet(parent.child(i).marks); i++) to += parent.child(i).nodeSize;
  return { from, to, mark };
}

/** O link sob a seleção no modo visual: o trecho, o texto e o endereço. */
export function linkAtVisual(state) {
  const { from, to } = state.selection;
  const around = markRange(state.selection.$from, state.schema.marks.link);
  if (around && around.from <= from && around.to >= to) {
    return { ...around, text: state.doc.textBetween(around.from, around.to), href: around.mark.attrs.href, existing: true };
  }
  return { from, to, text: state.doc.textBetween(from, to), href: "" };
}

export function setLinkVisual(state, link, href, text) {
  const type = state.schema.marks.link;
  let tr = state.tr;
  if (!href) return tr.removeMark(link.from, link.to, type);
  if (link.from === link.to || (text && text !== link.text)) {
    const label = text || href;
    tr = tr.insertText(label, link.from, link.to);
    return tr.addMark(link.from, link.from + label.length, type.create({ href }));
  }
  return tr.removeMark(link.from, link.to, type).addMark(link.from, link.to, type.create({ href }));
}

export function activeVisual(state) {
  const { schema } = state;
  const active = new Set();
  for (const [command, mark] of [
    ["bold", "strong"],
    ["italic", "em"],
    ["underline", "underline"],
    ["strike", "strike"],
    ["code", "code"],
    ["link", "link"],
  ]) {
    if (schema.marks[mark] && markActive(state, schema.marks[mark])) active.add(command);
  }
  const parent = state.selection.$from.parent;
  if (schema.nodes.heading && parent.type === schema.nodes.heading) active.add("heading");
  const list = ancestorOf(state, node => node.type === schema.nodes.bullet_list || node.type === schema.nodes.ordered_list);
  if (list) active.add(list.node.type === schema.nodes.bullet_list ? "bullet_list" : "ordered_list");
  if (schema.nodes.blockquote && ancestorOf(state, node => node.type === schema.nodes.blockquote)) active.add("quote");
  return active;
}

// ---------------------------------------------------------------- o registro

export const COMMANDS = {
  bold: {
    feature: "emphasis",
    keys: ["Mod-b"],
    visual: schema => toggleMark(schema.marks.strong),
  },
  italic: {
    feature: "emphasis",
    keys: ["Mod-i"],
    visual: schema => toggleMark(schema.marks.em),
  },
  underline: {
    feature: "underline",
    keys: ["Mod-u"],
    visual: schema => toggleMark(schema.marks.underline),
  },
  strike: {
    feature: "strike",
    keys: ["Mod-Shift-x"],
    visual: schema => toggleMark(schema.marks.strike),
  },
  code: {
    feature: "code",
    keys: ["Mod-e"],
    visual: schema => toggleMark(schema.marks.code),
  },
  heading: {
    feature: "heading",
    keys: ["Mod-Alt-2"],
    visual: schema => toggleHeading(schema, 2),
    // Os outros níveis: o comando do modo Markdown (pelo nome) e o do visual.
    levels: {
      "Mod-Alt-1": ["heading1", schema => toggleHeading(schema, 1)],
      "Mod-Alt-3": ["heading3", schema => toggleHeading(schema, 3)],
    },
  },
  bullet_list: {
    feature: "lists",
    keys: ["Mod-Shift-8"],
    visual: schema => toggleList(schema, schema.nodes.bullet_list),
  },
  ordered_list: {
    feature: "lists",
    keys: ["Mod-Shift-7"],
    visual: schema => toggleList(schema, schema.nodes.ordered_list),
  },
  quote: {
    feature: "quote",
    keys: ["Mod-Shift-9"],
    visual: schema => toggleQuote(schema),
  },
  link: { feature: "link", keys: ["Mod-k"], dialog: true },
};

/** Os comandos que o campo aceita, na ordem do registro. */
export function commandsFor(features) {
  const on = new Set(features);
  return Object.entries(COMMANDS).filter(([, command]) => on.has(command.feature));
}
