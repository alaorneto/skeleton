// Ida e volta Markdown → modo visual → Markdown (#825): o texto que o modo visual escreve tem a
// mesma formatação do original (o mesmo documento; a ordem de marcas aninhadas pode mudar) e
// fica estável depois de um ciclo.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { createParser, createSerializer } from "../src/markdown-editor/conversion.js";
import { FEATURES, renderHtml } from "../src/markdown-editor/dialect.js";
import { buildSchema } from "../src/markdown-editor/schema.js";

const corpus = JSON.parse(readFileSync(new URL("../../tests/fixtures/markdown/corpus.json", import.meta.url), "utf8"));

function converter(features = FEATURES) {
  const schema = buildSchema(features);
  const parser = createParser(schema, features);
  const serializer = createSerializer(schema);
  return { schema, parser, roundTrip: text => serializer.serialize(parser.parse(text)), serializer };
}

for (const entry of corpus) {
  test(`ida e volta: ${entry.name}`, () => {
    const features = entry.features ?? FEATURES;
    const { parser, roundTrip } = converter(features);
    const once = roundTrip(entry.markdown);
    assert.ok(parser.parse(once).eq(parser.parse(entry.markdown)), once);
    assert.equal(roundTrip(once), once);
  });
}

const EXTRA = [
  "a\n\n\n\nb",
  "linha\\n\\nduas quebras",
  "texto com 1+1=2 e x++y e ++ solto",
  "&amp; literal e &copy;",
  "1) não é lista\n2. é lista",
  "* a\n* b\n\n1. x",
  "- a\n  - b\n    - c\n- d",
  "> - a\n> - b\n>\n> texto",
  "[r](https://exemplo.test/a b) [p](https://exemplo.test/(x))",
  "`a` **`b`** *c `d` e*",
  "_a_b_ snake_case_name",
  "# **forte** no título\ntexto",
  "\# não é título",
  "- [ ] não é tarefa",
];

for (const text of EXTRA) {
  test(`ida e volta extra: ${JSON.stringify(text)}`, () => {
    const { parser, roundTrip } = converter();
    const once = roundTrip(text);
    assert.ok(parser.parse(once).eq(parser.parse(text)), once);
    assert.equal(roundTrip(once), once);
  });
}

test("recursos desligados não existem no schema", () => {
  const { schema, roundTrip } = converter(["emphasis"]);
  assert.ok(schema.marks.strong && schema.marks.em);
  for (const name of ["underline", "strike", "code", "link"]) assert.equal(schema.marks[name], undefined, name);
  for (const name of ["heading", "blockquote", "bullet_list", "list_item"]) assert.equal(schema.nodes[name], undefined, name);
  assert.equal(renderHtml(roundTrip("# a\n- b ++c++"), ["emphasis"]), renderHtml("# a\n- b ++c++", ["emphasis"]));
});
