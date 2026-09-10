import test from "node:test";
import assert from "node:assert/strict";

import {
  buildResolutionItems,
  normalizeResolutionKey,
  preferredDisplayName,
} from "../src/utils/scriptResolution.js";

test("prefers an existing readable occurrence over an all-caps occurrence", () => {
  const source = "EXT. GRACE - DAY\nThe bus stops in Grace.";
  assert.equal(preferredDisplayName(source, "GRACE"), "Grace");
});

test("does not manufacture title case when only all-caps occurs", () => {
  assert.equal(preferredDisplayName("EXT. MCDONALD LAB - DAY", "MCDONALD LAB"), "MCDONALD LAB");
});

test("preserves naturally styled and non-Latin names", () => {
  assert.equal(preferredDisplayName("iPhone waits beside मां.", "iPhone"), "iPhone");
  assert.equal(preferredDisplayName("iPhone waits beside मां.", "मां"), "मां");
});

test("groups NFC and full-casefold-equivalent extracted names", () => {
  const items = buildResolutionItems("Weiß meets WEISS.", ["Weiß", "WEISS"], "character");
  assert.equal(normalizeResolutionKey("Weiß"), normalizeResolutionKey("WEISS"));
  assert.deepEqual(items, [
    {
      id: "character:weiss",
      key: "weiss",
      type: "character",
      extractedName: "Weiß",
      displayName: "Weiß",
    },
  ]);
});
