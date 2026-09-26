import test from 'node:test';
import assert from 'node:assert/strict';
import { friendlyMessage, videoReviewGuidance, videoReviewWarnings } from '../src/utils/presentation.js';

const providerError = JSON.stringify({code:'invalid_parameters', message:'Invalid parameters. Please check that resolution, duration, prompt length, and other parameters are within the model\'s supported range.'});
const warnings = [
  'Audio-guided generation is experimental. Review the words, voice and mouth timing; the model may alter the recording.',
  'Video duration is 4s for 0.768s of reference speech; generated audio is retained without forced trimming or replacement.',
];
test('actual production rejection is an input failure, never speech-timing advice', () => {
  assert.match(friendlyMessage(providerError), /rejected this shot's inputs/);
  assert.doesNotMatch(friendlyMessage(providerError), /listen|timing may differ/i);
  assert.equal(friendlyMessage('Provider duration validation failed', 'Video failed'), 'Video failed');
});
test('no result review notices while waiting or after a failed request', () => {
  for (const video_status of [undefined, 'submitting', 'processing', 'failed', 'review_required']) {
    assert.deepEqual(videoReviewWarnings({video_status, video_warnings:warnings, experimental_audio_sync:true}), []);
  }
});
test('successful speech video has one non-error listening note, no duration bookkeeping', () => {
  const result=videoReviewWarnings({video_status:'done',video_url:'/video.mp4',experimental_audio_sync:true,video_warnings:warnings});
  assert.equal(result.length,1);
  assert.match(result[0], /Listen to the dialogue/);
});
test('actual review findings retained and deduplicated; silent clips have no speech caution', () => {
  assert.deepEqual(videoReviewWarnings({video_status:'done',video_url:'/video.mp4'}), []);
  const result=videoReviewWarnings({video_status:'done',video_url:'/video.mp4',video_warnings:['style mismatch','style mismatch']});
  assert.equal(result.length,1); assert.match(result[0], /visual style/);
});
test('speech mismatch and checker outage stay distinct from generic timing advice', () => {
  for (const [status, expected] of [['mismatch', /does not match/], ['unverified', /couldn.t verify/]]) {
    const notes = videoReviewWarnings({video_status:'done', video_url:'/v.mp4', experimental_audio_sync:true,
      video_speech_check:{status}, video_warnings:['WARNING: Render compliance: not verified for speech; accepting video for user review.']});
    assert.equal(notes.length, 1);
    assert.match(notes[0], expected);
    assert.doesNotMatch(notes[0], /timing may differ/);
  }
});
test('stopped clips explain the cause and the user action in plain language', () => {
  const speech = videoReviewGuidance({video_error:'speech: Approved dialogue does not match generated speech.'});
  assert.match(speech.message, /saved voice recording/);
  assert.match(speech.action, /automatically/);
  const staging = videoReviewGuidance({video_error:'staging: contradicts required support "Kabir left hand grips doorframe" during bottle receipt; speech: mismatch'});
  assert.match(staging.message, /positioned/);
  assert.equal(staging.detail, 'Required detail not maintained: Kabir left hand grips doorframe.');
  assert.match(staging.action, /Edit this shot/);
});
