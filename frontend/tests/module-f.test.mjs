import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

// Supply Vite's env expression for Node; exercise the actual client serializer.
const source = (await readFile(new URL("../src/api/client.js", import.meta.url), "utf8"))
  .replace('import.meta.env.VITE_BACKEND_URL', '"http://localhost:8016"');
const { regenerateShot } = await import(`data:text/javascript;base64,${Buffer.from(source).toString("base64")}`);

test("regenerate keeps the original bodyless request unless a hint is selected", async (t) => {
  const requests = [];
  t.mock.method(globalThis, "fetch", async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => ({ id: "fixture" }) };
  });
  await regenerateShot("fixture", 2);
  await regenerateShot("fixture", 2, { angle: "", movement: "", lighting: "", composition: "" });
  assert.deepEqual(requests[0], requests[1]);
  assert.deepEqual(requests[0].options, { method: "POST" });
  await regenerateShot("fixture", 2, { movement: "orbit", angle: "" });
  assert.deepEqual(requests[2].options, {
    method: "POST", headers: { "Content-Type": "application/json" }, body: '{"movement":"orbit"}',
  });
});
