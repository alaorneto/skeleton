// O modo visual (#825): ProseMirror, o texto já formatado e sem marcadores.
import { baseKeymap, chainCommands, exitCode } from "prosemirror-commands";
import { history, redo, undo } from "prosemirror-history";
import { inputRules, textblockTypeInputRule, wrappingInputRule } from "prosemirror-inputrules";
import { keymap } from "prosemirror-keymap";
import { liftListItem, sinkListItem, splitListItem } from "prosemirror-schema-list";
import { EditorState } from "prosemirror-state";
import { EditorView } from "prosemirror-view";

import { createParser, createSerializer, textToSlice } from "./conversion.js";
import { normalizePastedHTML } from "./paste.js";
import { buildSchema } from "./schema.js";

function blockRules(schema) {
  const rules = [];
  const { nodes } = schema;
  if (nodes.bullet_list) rules.push(wrappingInputRule(/^\s*([-+*])\s$/, nodes.bullet_list));
  if (nodes.ordered_list) {
    rules.push(
      wrappingInputRule(
        /^(\d+)\.\s$/,
        nodes.ordered_list,
        match => ({ order: Number(match[1]) }),
        (match, node) => node.childCount + node.attrs.order === Number(match[1]),
      ),
    );
  }
  if (nodes.blockquote) rules.push(wrappingInputRule(/^\s*>\s$/, nodes.blockquote));
  if (nodes.heading) rules.push(textblockTypeInputRule(/^(#{1,3})\s$/, nodes.heading, match => ({ level: match[1].length })));
  return rules;
}

function hardBreak(schema) {
  const type = schema.nodes.hard_break;
  return chainCommands(exitCode, (state, dispatch) => {
    if (dispatch) dispatch(state.tr.replaceSelectionWith(type.create()).scrollIntoView());
    return true;
  });
}

export class VisualMode {
  constructor({ features }) {
    this.schema = buildSchema(features);
    this.parser = createParser(this.schema, features);
    this.serializer = createSerializer(this.schema);
  }

  parse(markdown) {
    return this.parser.parse(markdown);
  }

  serialize(doc) {
    return this.serializer.serialize(doc);
  }

  mount({ parent, markdown, keys, attributes, onChange, onUpdate }) {
    const schema = this.schema;
    const listKeys = {};
    if (schema.nodes.list_item) {
      listKeys.Enter = splitListItem(schema.nodes.list_item);
      listKeys["Mod-["] = liftListItem(schema.nodes.list_item);
      listKeys["Mod-]"] = sinkListItem(schema.nodes.list_item);
    }
    const state = EditorState.create({
      doc: this.parse(markdown),
      plugins: [
        history(),
        inputRules({ rules: blockRules(schema) }),
        keymap(keys),
        keymap({ "Mod-z": undo, "Mod-y": redo, "Mod-Shift-z": redo, "Shift-Enter": hardBreak(schema), ...listKeys }),
        keymap(baseKeymap),
      ],
    });
    const view = new EditorView(parent, {
      state,
      attributes,
      clipboardTextParser: text => textToSlice(this.parser, text),
      transformPastedHTML: normalizePastedHTML,
      dispatchTransaction: transaction => {
        view.updateState(view.state.apply(transaction));
        if (transaction.docChanged) onChange(this.serialize(view.state.doc));
        onUpdate();
      },
      handleDOMEvents: { focus: () => (onUpdate(), false), blur: () => (onUpdate(), false) },
    });
    return view;
  }
}
