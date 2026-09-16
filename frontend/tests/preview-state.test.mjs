import test from 'node:test';
import assert from 'node:assert/strict';
import { previewState, previewSummary } from '../src/utils/previewState.js';

test('planning done is not an image; refresh relies on persisted outputs', () => {
  assert.equal(previewState({status:'done'}).label, 'Waiting');
  assert.equal(previewState({still_frame_status:'generating'}).label, 'Creating preview');
  assert.equal(previewState({still_frame_status:'failed'}).label, 'Preview failed');
  assert.equal(previewState({still_frame_url:'https://asset/image'}).label, 'Preview ready');
  const restored = JSON.parse(JSON.stringify({audio_assembly_pending:true,shots:[{still_frame_url:'https://asset/image'},{still_frame_status:'generating'}]}));
  assert.deepEqual(previewSummary(restored), {total:2,ready:1,failed:0,busy:true});
});
