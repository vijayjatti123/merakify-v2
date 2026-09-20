import io
import math
import struct
import subprocess
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.services import approved_audio_lock, speech_compliance_service


def wav_tone(seconds=.4, frequency=440, rate=24000):
    out = io.BytesIO()
    with wave.open(out, 'wb') as audio:
        audio.setparams((1, 2, rate, 0, 'NONE', 'not compressed'))
        audio.writeframes(b''.join(struct.pack('<h', int(12000 * math.sin(2 * math.pi * frequency * n / rate)))
                                   for n in range(int(seconds * rate))))
    return out.getvalue()


class ApprovedAudioLockTests(unittest.TestCase):
    def test_task_scoped_approved_audio_fields_are_supported(self):
        with patch.object(approved_audio_lock.storage_service, 'asset_url', return_value='https://example.test/approved.wav') as url:
            self.assertEqual(approved_audio_lock._audio_url({'video_approved_audio_key':'approved.wav'}),
                             'https://example.test/approved.wav')
            url.assert_called_once_with('approved.wav')

    def test_generated_soundtrack_is_replaced_by_approved_line_and_silence(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'source.mp4'
            subprocess.run([approved_audio_lock._ffmpeg(), '-nostdin', '-y', '-v', 'error',
                '-f', 'lavfi', '-i', 'color=c=black:s=320x180:d=1', '-f', 'lavfi',
                '-i', 'sine=frequency=880:duration=1', '-c:v', 'libx264', '-pix_fmt',
                'yuv420p', '-c:a', 'aac', str(source)], check=True, timeout=60)
            approved = wav_tone()
            response = MagicMock(); response.__enter__.return_value.iter_bytes.return_value = [approved]
            response.__enter__.return_value.raise_for_status.return_value = None
            media = io.BytesIO(source.read_bytes())
            with patch.object(approved_audio_lock.storage_service, 'asset_url', return_value='https://example.test/approved.wav'), \
                 patch.object(approved_audio_lock.httpx, 'stream', return_value=response):
                result = approved_audio_lock.apply(media, {'dialogue_audio_key': 'approved.wav'})
            pcm, duration = speech_compliance_service.extract_audio(media)
            self.assertEqual(result['policy'], 'approved-dialogue-plus-silence-v1')
            self.assertGreater(duration, .95)
            with wave.open(io.BytesIO(pcm), 'rb') as decoded:
                samples = struct.unpack('<' + 'h' * decoded.getnframes(), decoded.readframes(decoded.getnframes()))
            early = samples[:int(.35 * 24000)]
            crossings = sum((a < 0) != (b < 0) for a, b in zip(early, early[1:]))
            self.assertTrue(250 < crossings < 400)  # 440Hz approved tone, not the generated 880Hz tone.
            tail = samples[int(.65 * 24000):]
            self.assertLess(sum(abs(value) for value in tail) / len(tail), 80)  # padded silence


if __name__ == '__main__':
    unittest.main()
