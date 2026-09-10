import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { runInNewContext } from "node:vm";

function loadVaultSourceAndGuard() {
  const source = readFileSync(new URL("../src/pages/CharacterVault.jsx", import.meta.url), "utf8");
  const definition = source.match(/export function isVoiceSelectionSaved[\s\S]*?\n}/)?.[0];
  assert.ok(definition, "CharacterVault must export its saved-voice approval guard");
  const guard = runInNewContext(`(${definition.replace("export ", "")})`);
  return { guard, source };
}

test("approval stays disabled until the current voice selection is saved", () => {
  const { guard, source } = loadVaultSourceAndGuard();

  assert.equal(guard("priya", "priya"), true);
  assert.equal(guard("rahul", "priya"), false);
  assert.equal(guard("", "priya"), false);
  assert.equal(guard("priya", null), false);
  assert.match(source, /disabled=\{busy \|\| !voiceSelectionSaved\}/);
  assert.match(source, /Save your voice selection before approving\./);
});
