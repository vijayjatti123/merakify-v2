import io
import wave
import unittest
from unittest.mock import patch, MagicMock

from app.services import seedance_audio_reference as refs, video_generation_service as video
import test_audio_video


def wav(seconds, rate=24000):
    data = io.BytesIO()
    with wave.open(data, 'wb') as output:
        output.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        output.writeframes(b'\x12\x34' * round(seconds * rate))
    return data.getvalue()


class SeedanceReferenceTests(unittest.TestCase):
    def setUp(self):
        test_audio_video.AudioVideoTests.setUp(self)
        self.audio_preflight.stop()
    saved = test_audio_video.AudioVideoTests.saved

    def test_real_short_pcm_is_preserved_exactly_with_only_trailing_silence(self):
        source = wav(26624 / 24000)
        padded, facts = refs.inspect_and_pad(source, 26624 / 24000)
        with wave.open(io.BytesIO(source)) as original, wave.open(io.BytesIO(padded)) as result:
            original_pcm = original.readframes(original.getnframes())
            actual = result.readframes(result.getnframes())
            self.assertEqual(actual[:len(original_pcm)], original_pcm)
            self.assertEqual(set(actual[len(original_pcm):]), {0})
            self.assertEqual(result.getnframes() / result.getframerate(), 3.0)
        self.assertAlmostEqual(facts['trailing_silence_sec'], 3.0 - 26624/24000)

    def test_recent_production_short_lengths_keep_original_samples(self):
        for seconds in (.768, 1.28, 1.7066666666666668, 1.9626666666666666, 2, 2.9):
            source=wav(seconds)
            padded,facts=refs.inspect_and_pad(source,seconds)
            with wave.open(io.BytesIO(source)) as before, wave.open(io.BytesIO(padded)) as after:
                original=before.readframes(before.getnframes())
                actual=after.readframes(after.getnframes())
                self.assertEqual(actual[:len(original)],original)
                self.assertEqual(set(actual[len(original):]),{0})
                self.assertEqual(facts['reference_duration_sec'],3.0)

    def test_valid_references_unchanged_and_bad_lengths_rejected(self):
        for seconds in (3, 3.048, 15):
            self.assertIsNone(refs.inspect_and_pad(wav(seconds), seconds)[0])
        for data, duration in ((wav(0), 0), (wav(15.01), 15.01), (wav(1), 2), (b'invalid', 1)):
            with self.subTest(duration=duration), self.assertRaises(ValueError):
                refs.inspect_and_pad(data, duration)

    def test_each_evolink_tier_sends_padded_copy_and_persists_retry_request(self):
        from app.services import job_service as jobs
        self.result['shots'][0]['dialogue_audio_duration_sec'] = 1.1093333333333333
        jobs.set_result(self.db, self.job.id, self.result)
        download = MagicMock()
        download.__enter__.return_value.iter_bytes.return_value = [wav(26624/24000)]
        for tier in ('seedance_evolink', 'seedance_fast_evolink', 'seedance_mini_evolink'):
            job = jobs.create_job(self.db, 'audio preflight test', ai_model='Seedance 2.0')
            jobs.set_result(self.db, job.id, self.result); jobs.set_status(self.db, job.id, 'done')
            with patch.object(refs.httpx, 'stream', return_value=download), patch.object(refs.storage_service, 'upload_bytes', return_value={'key':'padded.wav'}), patch.object(refs.storage_service, 'asset_url', return_value='https://example.com/padded.wav'), patch.object(video, 'provider', return_value={'id':tier}) as call:
                video.start(self.db, job.id, 1, audio_model=tier)
                request = call.call_args.args[2]
                self.assertEqual(request['audio_urls'], ['https://example.com/padded.wav'])
                self.assertEqual(request['duration'], 5)  # Planned 4.2s is preserved, rounded for provider.
                saved = jobs.video_source(self.db, job.id, 1)[1]
                self.assertEqual(saved['video_retry_request']['audio_urls'], request['audio_urls'])
                self.assertEqual(saved['dialogue_audio_url'], 'https://example.com/approved.wav')
                with self.assertRaises(ValueError): video.start(self.db, job.id, 1, audio_model=tier)
                self.assertEqual(call.call_count, 1)

    def test_bad_reference_fails_before_paid_call_with_retryable_state(self):
        with patch.object(refs, 'prepare', side_effect=ValueError('Speech reference unreadable; retry')), patch.object(video, 'provider') as call:
            with self.assertRaisesRegex(ValueError, 'unreadable'):
                video.start(self.db, self.job.id, 1)
            call.assert_not_called()
        self.assertEqual(self.saved()['video_status'], 'failed')
        self.assertIn('retry', self.saved()['video_error'])

    def test_fal_kling_and_silent_requests_do_not_enter_evolink_preflight(self):
        with patch.object(refs.httpx, 'stream') as call:
            for t in ({'provider':'fal','audio_model':'seedance_fal'}, {'provider':'fal','audio_model':'kling_voice_fal'}, {'provider':'evolink'}):
                self.assertIsNone(refs.prepare(t, {}, 'test'))
            call.assert_not_called()

    def test_unsigned_pcm_padding_uses_silence_midpoint(self):
        output=io.BytesIO()
        with wave.open(output,'wb') as source:
            source.setparams((1,1,8000,0,'NONE','not compressed'))
            source.writeframes(b'\x90'*8000)
        padded,_=refs.inspect_and_pad(output.getvalue(),1)
        with wave.open(io.BytesIO(padded)) as result:
            self.assertEqual(result.readframes(8000),b'\x90'*8000)
            self.assertEqual(set(result.readframes(10000)),{128})

    def test_real_mp3_short_reference_is_decodable_and_padded(self):
        import subprocess, tempfile
        from pathlib import Path
        from app.services.hedra_video_service import ffmpeg
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'speech.mp3'
            subprocess.run([ffmpeg(),'-v','error','-f','lavfi','-i','sine=frequency=440:sample_rate=24000',
                            '-t','1','-c:a','libmp3lame',str(path)],check=True,timeout=30)
            padded,evidence=refs.inspect_and_pad(path.read_bytes(),1)
        self.assertAlmostEqual(evidence['source_duration_sec'],1,places=3)
        with wave.open(io.BytesIO(padded)) as result:
            self.assertEqual(result.getnframes()/result.getframerate(),3.0)
