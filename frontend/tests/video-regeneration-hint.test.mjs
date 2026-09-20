import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

test("the visible change field feeds the primary regenerate-video action", async () => {
  const source = await readFile(new URL("../src/pages/JobView.jsx", import.meta.url), "utf8");
  assert.match(source, /const hint = \(videoHints\[shotNumber\] \|\| ""\)\.trim\(\)/);
  assert.match(source, /await regenerateShotVideo\(jobId, shot, hint\)/);
  assert.doesNotMatch(source, /handleRegenerate\(shot\.shot_number, true\)/);
  assert.match(source, /What would you like to change\? \(optional\)/);
  assert.match(source, /shot\.video_status === "review_required" && !lockedAudioReview\(shot\)/);
  assert.match(source, /videoReviewGuidance\(shot\)/);
  assert.doesNotMatch(source, /!\["error", "done"\]\.includes\(shot\.video_status\)/);
});
