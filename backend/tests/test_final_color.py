import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.services import final_assembly_service as a


class ColorTests(unittest.TestCase):
    def test_hard_cut_lookup_preserves_coefficients_and_exclusive_end(self):
        timeline = {'shots': [{'start': 0., 'duration': 4.}, {'start': 4., 'duration': 5.}],
                    'boundaries': [{'overlap_sec': 0}]}
        adjustments = [dict(y=9.2446444116, u=-6, v=6, saturation=.85),
                       dict(y=0, u=0, v=0, saturation=1)]
        value = a.correction_filter(timeline, adjustments)
        self.assertTrue(value.startswith('lutyuv='))
        self.assertIn('9.244644412', value)
        self.assertIn('(1+(-0.150000000))', value)
        self.assertIn("enable='gte(t,0.000000000)*lt(t,4.000000000)'", value)
        self.assertEqual(value.count('lutyuv='), 1)

    def test_crossfade_keeps_continuous_original_correction(self):
        timeline = {'shots': [{'start': 0., 'duration': 4.}, {'start': 3.5, 'duration': 5.}],
                    'boundaries': [{'overlap_sec': .5}]}
        adjustments = [dict(y=12, u=-6, v=6, saturation=.85), dict(y=-16, u=0, v=0, saturation=1)]
        value = a.correction_filter(timeline, adjustments)
        self.assertTrue(value.startswith('geq='))
        self.assertIn('clip((4.000000000-T)/0.500000000,0,1)', value)
        self.assertIn('clip((T-3.500000000)/0.500000000,0,1)', value)

    def test_overlapping_intervals_without_transition_do_not_stack_lookups(self):
        timeline = {'shots': [{'start': 0., 'duration': 4.}, {'start': 3., 'duration': 5.}],
                    'boundaries': [{'overlap_sec': 0}]}
        adjustments = [dict(y=12, u=0, v=0, saturation=1)] * 2
        self.assertTrue(a.correction_filter(timeline, adjustments).startswith('geq='))

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
