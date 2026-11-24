// O corpus compartilhado com o servidor (#824): o markdown-it do editor precisa produzir o
// mesmo HTML que skeleton/common/markdown.py (tests/unit/test_markdown.py confere o outro lado).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { FEATURES, isAllowedLink, renderHtml, validateFeatures } from "../src/markdown-editor/dialect.js";

const corpus = JSON.parse(readFileSync(new URL("../../tests/fixtures/markdown/corpus.json", import.meta.url), "utf8"));

for (const entry of corpus) {
  test(`corpus: ${entry.name}`, () => {
    assert.equal(renderHtml(entry.markdown, entry.features), entry.html);
  });
}

test("links", () => {
  for (const url of ["https://exemplo.test", "HTTPS://exemplo.test/a", "/o/x/"]) assert.ok(isAllowedLink(url), url);
  for (const url of ["https://", "http://exemplo.test", "//evil.test", "/\\evil", "mailto:a@b.test", "javascript:x", "relativo", ""]) {
    assert.ok(!isAllowedLink(url), url);
  }
});

test("features", () => {
  assert.deepEqual(validateFeatures(null), [...FEATURES]);
  assert.deepEqual(validateFeatures(["link", "emphasis"]), ["emphasis", "link"]);
  assert.throws(() => validateFeatures(["tables"]), /Unknown/);
  assert.throws(() => validateFeatures(["link", "link"]), /Repeated/);
  assert.throws(() => validateFeatures("link"), /sequence/);
});
