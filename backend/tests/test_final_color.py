import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import final_assembly_service as a


class ColorTests(unittest.TestCase):
    def test_none_is_literal_copy_and_grade_changes_fingerprint(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'a', Path(folder)/'b'
            source.write_bytes(b'original corrected video')
            with patch.object(a.subprocess, 'run') as run:
                a.color_encode(source, target, a.CREATIVE_GRADES['None'], lambda _: None)
                run.assert_not_called()
            self.assertEqual(source.read_bytes(), target.read_bytes())
        self.assertNotEqual(a.fingerprint({}, '16:9', '720p', 'None'), a.fingerprint({}, '16:9', '720p', 'Vintage'))

    def test_outlier_correction_bounded_and_single_shot_unchanged(self):
        normal = dict(shot_number=1, YAVG=110, UAVG=128, VAVG=128, SATAVG=20)
        warm = dict(shot_number=2, YAVG=170, UAVG=108, VAVG=148, SATAVG=40)
        _, single = a.correction_plan([warm])
        self.assertEqual(single[0], dict(shot_number=2, y=0, u=0, v=0, saturation=1))
        _, correction = a.correction_plan([normal, warm, normal])
        self.assertEqual(correction[0]['y'], 0)
        self.assertEqual(correction[1]['y'], -16)
        self.assertLessEqual(abs(correction[1]['u']), 6)
        self.assertEqual(correction[1]['saturation'], .85)

    def test_invalid_grade_and_failed_encode_are_bounded(self):
        with self.assertRaises(a.AssemblyError):
            a.apply_color_pipeline('absent', 'absent', {}, 'invented')
        with patch.object(a, 'probe', return_value={}), patch.object(a, 'ffmpeg', return_value='ffmpeg'), patch.object(a.subprocess, 'run', side_effect=a.subprocess.TimeoutExpired('ffmpeg', 900)) as run:
            with self.assertRaises(a.subprocess.TimeoutExpired):
                a.color_encode('a', 'b', 'eq=saturation=.8', lambda _: None)
            self.assertEqual(run.call_count, 2)


if __name__ == '__main__':
    unittest.main()
