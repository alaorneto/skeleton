// Comandos da faixa de opções (#825) nos dois modos, sobre o estado dos editores (sem DOM).
import assert from "node:assert/strict";
import { test } from "node:test";

import { EditorSelection, EditorState as SourceState } from "@codemirror/state";
import { EditorState as VisualState, TextSelection } from "prosemirror-state";

import { COMMANDS, activeVisual, commandsFor, linkAtVisual, setLinkVisual } from "../src/markdown-editor/commands.js";
import { SOURCE_COMMANDS, activeSource, linkAtSource, setLinkSource } from "../src/markdown-editor/source-commands.js";
import { FEATURES } from "../src/markdown-editor/dialect.js";
import { continueMarkup, sourceLanguage } from "../src/markdown-editor/source-mode.js";
import { VisualMode } from "../src/markdown-editor/visual-mode.js";

// ------------------------------------------------------------------ modo Markdown

function source(text, features = FEATURES) {
  // "|" marca o cursor; "«" e "»" a seleção.
  const anchor = text.indexOf("«");
  let doc = text;
  let selection;
  if (anchor >= 0) {
    doc = text.replace("«", "").replace("»", "");
    selection = EditorSelection.single(anchor, text.indexOf("»") - 1);
  } else {
    const cursor = text.indexOf("|");
    doc = text.replace("|", "");
    selection = EditorSelection.single(cursor >= 0 ? cursor : 0);
  }
  return SourceState.create({ doc, selection, extensions: [sourceLanguage(features)] });
}

function run(state, command) {
  const spec = command(state);
  return spec ? state.update(spec).state : state;
}

function text(state) {
  return state.doc.toString();
}

test("negrito envolve a seleção e sai no segundo clique", () => {
  const once = run(source("um «texto» aqui"), SOURCE_COMMANDS.bold);
  assert.equal(text(once), "um **texto** aqui");
  assert.equal(text(run(once, SOURCE_COMMANDS.bold)), "um texto aqui");
});

test("os marcadores ficam colados ao texto", () => {
  assert.equal(text(run(source("um« texto »aqui"), SOURCE_COMMANDS.bold)), "um **texto** aqui");
});

test("itálico dentro do negrito não desfaz o negrito", () => {
  const state = run(source("**«a»**"), SOURCE_COMMANDS.italic);
  assert.equal(text(state), "***a***");
  assert.ok(activeSource(state).has("bold"));
});

test("sem seleção, os marcadores entram com o cursor no meio", () => {
  const state = run(source("a |b"), SOURCE_COMMANDS.underline);
  assert.equal(text(state), "a ++++b");
  assert.equal(state.selection.main.head, 4);
});

test("cursor dentro de um trecho marcado tira a marca do trecho", () => {
  assert.equal(text(run(source("a ~~tac|hado~~ b"), SOURCE_COMMANDS.strike)), "a tachado b");
  assert.equal(text(run(source("a `có|digo` b"), SOURCE_COMMANDS.code)), "a código b");
});

test("seleção em várias linhas marca cada linha", () => {
  assert.equal(text(run(source("«um\ndois»"), SOURCE_COMMANDS.bold)), "**um**\n**dois**");
});

test("listas, título e citação por linha", () => {
  const bullets = run(source("«a\nb»"), SOURCE_COMMANDS.bullet_list);
  assert.equal(text(bullets), "- a\n- b");
  const ordered = run(bullets.update({ selection: EditorSelection.single(0, 7) }).state, SOURCE_COMMANDS.ordered_list);
  assert.equal(text(ordered), "1. a\n2. b");
  assert.equal(text(run(source("«1. a\n2. b»"), SOURCE_COMMANDS.ordered_list)), "a\nb");
  assert.equal(text(run(source("tí|tulo"), SOURCE_COMMANDS.heading)), "## título");
  assert.equal(text(run(source("## tí|tulo"), SOURCE_COMMANDS.heading)), "título");
  assert.equal(text(run(source("# tí|tulo"), SOURCE_COMMANDS.heading)), "## título");
  assert.equal(text(run(source("ci|ta"), SOURCE_COMMANDS.quote)), "> cita");
  assert.equal(text(run(source("> ci|ta"), SOURCE_COMMANDS.quote)), "cita");
});

test("Enter continua listas e citações e encerra no item vazio", () => {
  assert.equal(text(run(source("- a|"), continueMarkup)), "- a\n- ");
  assert.equal(text(run(source("9. a|"), continueMarkup)), "9. a\n10. ");
  assert.equal(text(run(source("> a|"), continueMarkup)), "> a\n> ");
  assert.equal(text(run(source("- |"), continueMarkup)), "");
  assert.equal(continueMarkup(source("texto|")), null);
});

test("link no modo Markdown: lê, troca e tira", () => {
  const state = source("ver [rót|ulo](https://exemplo.test/a) fim");
  const link = linkAtSource(state);
  assert.deepEqual([link.text, link.href, link.existing], ["rótulo", "https://exemplo.test/a", true]);
  assert.equal(text(state.update(setLinkSource(state, link, "/local", "")).state), "ver [rótulo](/local) fim");
  assert.equal(text(state.update(setLinkSource(state, link, "", "")).state), "ver rótulo fim");
  const plain = source("ver «isto» fim");
  assert.equal(text(plain.update(setLinkSource(plain, linkAtSource(plain), "https://a.test/(x)", "")).state), "ver [isto](https://a.test/\\(x\\)) fim");
});

test("estado ativo no modo Markdown", () => {
  const active = activeSource(source("- **a ++b|++**"));
  for (const name of ["bullet_list", "bold", "underline"]) assert.ok(active.has(name), name);
  assert.ok(!active.has("italic"));
});

test("cada comando com recurso tem as duas implementações", () => {
  for (const [name, command] of Object.entries(COMMANDS)) {
    if (command.dialog) continue;
    assert.equal(typeof command.visual, "function", name);
    assert.equal(typeof SOURCE_COMMANDS[name], "function", name);
    for (const [source] of Object.values(command.levels || {})) assert.equal(typeof SOURCE_COMMANDS[source], "function", source);
  }
});

test("recursos desligados não são analisados nem viram botão", () => {
  assert.ok(!activeSource(source("# tí|tulo", ["emphasis"])).has("heading"));
  assert.deepEqual(
    commandsFor(["emphasis", "lists"]).map(([name]) => name),
    ["bold", "italic", "bullet_list", "ordered_list"],
  );
});

// ------------------------------------------------------------------ modo visual

function visual(markdown, features = FEATURES) {
  const mode = new VisualMode({ features });
  return { mode, state: VisualState.create({ doc: mode.parse(markdown) }) };
}

function apply(mode, state, command) {
  let next = state;
  command(state, transaction => {
    next = state.apply(transaction);
  });
  return next;
}

function at(state, from, to = from) {
  return state.apply(state.tr.setSelection(TextSelection.create(state.doc, from, to)));
}

test("modo visual: marcas, título, listas e citação", () => {
  const { mode, state } = visual("um texto");
  const bold = apply(mode, at(state, 4, 9), COMMANDS.bold.visual(mode.schema));
  assert.equal(mode.serialize(bold.doc), "um **texto**");
  assert.ok(activeVisual(at(bold, 6)).has("bold"));
  const heading = apply(mode, at(state, 2), COMMANDS.heading.visual(mode.schema));
  assert.equal(mode.serialize(heading.doc), "## um texto");
  assert.equal(mode.serialize(apply(mode, heading, COMMANDS.heading.visual(mode.schema)).doc), "um texto");
  const list = apply(mode, at(state, 2), COMMANDS.bullet_list.visual(mode.schema));
  assert.equal(mode.serialize(list.doc), "- um texto");
  const ordered = apply(mode, list, COMMANDS.ordered_list.visual(mode.schema));
  assert.equal(mode.serialize(ordered.doc), "1. um texto");
  assert.equal(mode.serialize(apply(mode, ordered, COMMANDS.ordered_list.visual(mode.schema)).doc), "um texto");
  const quote = apply(mode, at(state, 2), COMMANDS.quote.visual(mode.schema));
  assert.equal(mode.serialize(quote.doc), "> um texto");
  assert.equal(mode.serialize(apply(mode, quote, COMMANDS.quote.visual(mode.schema)).doc), "um texto");
});

test("modo visual: link lido, trocado e tirado", () => {
  const { mode, state } = visual("ver [rótulo](https://exemplo.test) fim");
  const inside = at(state, 7);
  const link = linkAtVisual(inside);
  assert.deepEqual([link.text, link.href, link.existing], ["rótulo", "https://exemplo.test", true]);
  assert.equal(mode.serialize(inside.apply(setLinkVisual(inside, link, "/x", "")).doc), "ver [rótulo](/x) fim");
  assert.equal(mode.serialize(inside.apply(setLinkVisual(inside, link, "", "")).doc), "ver rótulo fim");
  const empty = at(state, 1);
  assert.equal(mode.serialize(empty.apply(setLinkVisual(empty, linkAtVisual(empty), "/novo", "aqui")).doc).startsWith("[aqui](/novo)"), true);
});

test("modo visual: texto digitado que parece marcação continua texto", () => {
  const { mode } = visual("");
  const schema = mode.schema;
  const doc = schema.node("doc", null, [schema.node("paragraph", null, [schema.text("2 * 3 e a++b++ e 1) x e &amp; e [a](b)")])]);
  const markdown = mode.serialize(doc);
  assert.ok(mode.parse(markdown).eq(doc), markdown);
});
