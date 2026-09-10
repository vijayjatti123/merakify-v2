import test from "node:test";
import assert from "node:assert/strict";

import {
  buildResolutionItems,
  buildResolutionMap,
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

test("serializes D2 choices to IDs only for D3 job creation", () => {
  const characters = [{ id: "character:ravi", displayName: "Ravi" }, { id: "character:omar", displayName: "Omar" }];
  const locations = [{ id: "location:cafe", displayName: "Cafe" }];
  const resolutions = {
    "character:ravi": { mode: "vault", character: { id: "character-1", image_url: "not-sent" } },
    "character:omar": { mode: "invent", name: "Omar" },
    "location:cafe": { mode: "asset", asset: { id: "asset-1", url: "not-sent" } },
  };

  assert.deepEqual(buildResolutionMap(characters, locations, resolutions), {
    characters: {
      Ravi: { mode: "vault", character_id: "character-1" },
      Omar: { mode: "invent" },
    },
    locations: { Cafe: { mode: "asset", asset_id: "asset-1" } },
  });
});
