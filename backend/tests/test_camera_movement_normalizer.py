import unittest
from app.services.shot_prompt_compiler import first_movement, movement_present, compiler_input, validate_compiled, insert_dialogue
from test_shot_prompt_compiler import source, prose

class MovementNormalizerTests(unittest.TestCase):
    def test_stationary_aliases_and_qualifiers(self):
        aliases = [None, "", "static", "Static", "none", "none, locked-off", "none, locked-off tripod",
            "no camera movement", "no movement, tripod", "no camera motion", "no motion",
            "locked-off tripod", "locked off tripod", "locked, no movement", "fixed camera",
            "stationary camera", "motionless", "unmoving", "locked–off tripod", "locked‑off tripod"]
        for value in aliases:
            with self.subTest(value=value):
                actual, _ = first_movement(value)
                self.assertEqual(actual,"static")
                self.assertTrue(movement_present(actual,"The camera remains static"))

    def test_all_declared_movement_families_stay_moving(self):
        values = ["dollyin", "dollyout", "tracking", "orbit", "handheld", "craneup", "cranedown",
            "slow push-in", "slow pull out", "slow slide in", "slow pan right", "slow tilt up",
            "slow zoom in", "slow lateral dolly", "subtle handheld drift", "whip pan upward"]
        for value in values:
            with self.subTest(value=value):
                self.assertEqual(first_movement(value)[0],value)
                self.assertFalse(movement_present(value,"The camera remains static"))

    def test_later_static_qualifier_never_overrides_first_move(self):
        for value in ["slow pan, then locked-off", "slow pan then static", "slow pan with fixed framing"]:
            with self.subTest(value=value):
                self.assertEqual(first_movement(value)[0],"slow pan")
        self.assertEqual(first_movement("static then pan")[0],"static")
        self.assertEqual(first_movement("slow push-in then pan")[0],"slow push-in")
        self.assertEqual(first_movement("slow static push")[0],"static")

    def test_actual_compiler_validation_and_source_preservation(self):
        for value in ["none, locked-off tripod", "none, locked-off", "no camera movement"]:
            with self.subTest(value=value):
                original=source();original['shots'][0]['camera_movement']=value
                payload=compiler_input(original,emit=lambda *a:None)
                output=insert_dialogue({'shots':[{'shot_number':1,'compiled_prompt':prose()}]},payload)
                self.assertFalse(any('movement missing' in e for e in validate_compiled(output,payload)))
                self.assertEqual(original['shots'][0]['camera_movement'],value)
