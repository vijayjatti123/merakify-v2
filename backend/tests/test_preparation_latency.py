import copy
import io
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from PIL import Image
from app.services import camera_direction, planning_patch, preview_plan, preview_display, storage_service
from app.services import still_frame_service as still
from app.services.shot_prompt_compiler import merge_correction
from app.services.preview_reference_cache import ReferenceCache, source_identity
from app.services.character_image_service import GeneratedCharacterImage


class PreparationLatencyTests(unittest.TestCase):
    def test_dependency_priority_unlocks_long_chain_without_changing_edges(self):
        ordered = [{'shot_number': n} for n in (1, 2, 3, 4)]
        deps = {1: set(), 2: set(), 3: {2}, 4: {3}}
        before = copy.deepcopy(deps)
        ranked = still.dependency_priority(ordered, deps, set(deps))
        ready = [s['shot_number'] for s in ranked if not deps[s['shot_number']]]
        self.assertEqual(ready, [2, 1])
        self.assertEqual(deps, before)

    def test_actual_camera_finding_is_narrow_despite_preserve_dialogue_text(self):
        shots = [dict(shot_number=1, dialogue_text='Keep these exact words.', camera_direction={
            'movement': 'zoom', 'direction': 'in', 'speed': 'slow', 'stabilization': 'locked'})]
        qa = camera_direction.check_plan({'approved': True, 'issues': []}, shots)
        self.assertIn('all dialogue', qa['issues'][0]['fix_instruction'])
        self.assertEqual(planning_patch.patch_permissions(shots, qa['issues']), {1: ['camera_direction']})
        with self.assertRaises(ValueError):
            planning_patch.apply_patch_response(shots, {'patches': [{'shot_number': 1,
                'changes': {'dialogue_text': 'Wrong'}}]}, {1: ['camera_direction']})
        # A genuine semantic dialogue issue in the same QA still needs full repair.
        qa['issues'].append({'shot_number': 1, 'problem': 'Split dialogue line'})
        self.assertIsNone(planning_patch.patch_permissions(shots, qa['issues']))

    def test_correction_echo_cannot_change_sibling_and_order_is_normalized(self):
        kept = [{'shot_number': 1, 'compiled_prompt': 'accepted'}]
        response = {'shots': [{'shot_number': 3, 'compiled_prompt': 'fixed 3'},
            {'shot_number': 1, 'compiled_prompt': 'malicious change'}, {'shot_number': 2, 'compiled_prompt': 'fixed 2'}]}
        merged = merge_correction(response, [2, 3], kept, [1, 2, 3], Mock())
        self.assertEqual([s['compiled_prompt'] for s in merged['shots']], ['accepted', 'fixed 2', 'fixed 3'])
        for numbers in ([2], [2, 2, 3], [2, 3, 99], [True, 2, 3]):
            with self.assertRaises(ValueError):
                merge_correction({'shots': [{'shot_number': n} for n in numbers]}, [2, 3], kept, [1, 2, 3], Mock())

    def test_still_projection_does_not_leak_ending_and_keeps_product_exception(self):
        facts = {'characters': [], 'description': 'Person opens eyes and smiles',
            'state_at_shot_start': 'Eyes closed', 'state_at_shot_end': 'Eyes open smiling',
            'camera_movement': 'dolly forward', 'lighting': 'dim opening light'}
        before = copy.deepcopy(facts)
        text = preview_plan.preview_visual(facts)
        self.assertIn('Eyes closed', text)
        self.assertNotIn('opens eyes', text)
        self.assertNotIn('Eyes open smiling', text)
        self.assertNotIn('dolly forward', text)
        self.assertIn('approved product image references', text)
        self.assertEqual(facts, before)

    def test_reference_single_flight_failure_recovery_and_memory_bound(self):
        cache = ReferenceCache(max_bytes=8)
        image = GeneratedCharacterImage(b'1234', 'image/png')
        entered, release = threading.Event(), threading.Event()
        def loader():
            entered.set()
            self.assertTrue(release.wait(2))
            return image
        with ThreadPoolExecutor(2) as pool:
            first = pool.submit(cache.get, 'same', loader)
            self.assertTrue(entered.wait(2))
            second = pool.submit(cache.get, 'same', lambda: self.fail('duplicate download'))
            release.set()
            self.assertIs(first.result()[0], image)
            self.assertIs(second.result()[0], image)
        with self.assertRaises(ValueError):
            cache.get('bad', lambda: (_ for _ in ()).throw(ValueError()))
        self.assertEqual(cache.get('bad', lambda: image), (image, False))
        cache.seed('third', image)
        self.assertLessEqual(sum(len(f.result().data) for f in cache.entries.values()), 8)

    def test_only_owned_signatures_are_deduplicated(self):
        with patch.object(storage_service.settings, 'aws_s3_bucket', 'test'), patch.object(storage_service.settings, 'aws_region', 'ap-south-1'):
            self.assertEqual(source_identity('https://test.s3.ap-south-1.amazonaws.com/a?X-Amz-Signature=1'),
                             source_identity('https://test.s3.ap-south-1.amazonaws.com/a?X-Amz-Signature=2'))
            self.assertNotEqual(source_identity('https://external/a?image=1'), source_identity('https://external/a?image=2'))
            self.assertNotEqual(source_identity('https://test.s3.ap-south-1.amazonaws.com/a?versionId=1'),
                                source_identity('https://test.s3.ap-south-1.amazonaws.com/a?versionId=2'))

    def test_signing_is_stable_during_polling_but_renews_before_expiry(self):
        client = Mock()
        client.generate_presigned_url.side_effect = ['first', 'renewed']
        with patch.object(storage_service, '_s3_client', return_value=client), patch.object(storage_service, '_bucket', return_value='isolated'), \
             patch.object(storage_service.time, 'monotonic', side_effect=[100, 105, 109]):
            self.assertEqual(storage_service.asset_url('key', 10), 'first')
            self.assertEqual(storage_service.asset_url('key', 10), 'first')
            self.assertEqual(storage_service.asset_url('key', 10), 'renewed')
        self.assertEqual(client.generate_presigned_url.call_count, 2)

    def test_display_webp_is_smaller_valid_and_original_untouched(self):
        out = io.BytesIO()
        Image.new('RGB', (1376, 768), '#bca871').save(out, 'JPEG', quality=95)
        image = GeneratedCharacterImage(out.getvalue(), 'image/jpeg')
        before, uploads = image.data, []
        def upload(**kwargs):
            uploads.append(kwargs)
            return {'key': kwargs['key'], 'url': 'https://test/' + kwargs['key']}
        with patch.object(storage_service, 'upload_bytes', side_effect=upload):
            result = preview_display.store_variants(image, 'original.jpg', emit=Mock(), shot_number=1)
        self.assertEqual(image.data, before)
        self.assertEqual([v['width'] for v in result['variants']], [480, 960])
        for uploaded in uploads:
            self.assertLess(len(uploaded['body']), len(before))
            self.assertEqual(uploaded['content_type'], 'image/webp')
            with Image.open(io.BytesIO(uploaded['body'])) as decoded:
                self.assertEqual(decoded.format, 'WEBP')

    def test_established_entity_unlocks_parallelism_but_action_cut_stays_ordered(self):
        for continuous in (False, True):
            result = {'continuity': {'props': [{'name': 'Cup'}]}, 'shots': [
                {'shot_number': n, 'scene_number': 1 if continuous else n,
                 'description': 'Cup on table', 'characters_in_shot': []} for n in (1, 2, 3)],
                'assembly': {'transitions': [{'between': '1-2', 'type': 'cut'}, {'between': '2-3', 'type': 'cut'}]}}
            rendezvous = threading.Barrier(2)
            completed, lock = set(), threading.Lock()
            def worker(snapshot, *, shot_numbers, **kwargs):
                number = next(iter(shot_numbers))
                if number > 1:
                    self.assertEqual(snapshot['entity_references']['props:cup']['shot_number'], 1)
                    if continuous:
                        self.assertIn(number - 1, completed)
                    else:
                        rendezvous.wait(timeout=3)  # Fails if shared entity still chains shots 2 -> 3.
                shot = next(s for s in snapshot['shots'] if s['shot_number'] == number)
                shot.update(still_frame_key=f'key-{number}', still_frame_url=f'https://test/{number}',
                            still_frame_status='ready', still_frame_source_hash=still.shot_fingerprint(shot))
                if number == 1:
                    snapshot['entity_references']['props:cup'] = {'shot_number': 1, 'key': 'key-1',
                        'url': 'https://test/1', 'source_hash': shot['still_frame_source_hash']}
                with lock:
                    completed.add(number)
            with patch.object(still, '_generate_still_frames_serial', side_effect=worker):
                still.generate_still_frames(result, job_id='isolated', emit=Mock())
            self.assertEqual(completed, {1, 2, 3})
            self.assertEqual(set(result['shots'][2]['preview_dependencies']), {'1', '2'} if continuous else {'1'})

    def test_failed_seed_waits_for_next_qa_confirmed_anchor(self):
        result = {'continuity': {'props': [{'name': 'Cup'}]}, 'shots': [
            {'shot_number':n,'scene_number':n,'description':'Cup on table','characters_in_shot':[]} for n in (1,2,3)]}
        order=[]
        def worker(snapshot, *, shot_numbers, **kwargs):
            number=next(iter(shot_numbers));order.append(number)
            shot=next(s for s in snapshot['shots'] if s['shot_number']==number)
            if number == 1:
                shot['still_frame_status']='failed';return
            if number == 2:
                self.assertNotIn('props:cup',snapshot['entity_references'])
                snapshot['entity_references']['props:cup']={'shot_number':2,'key':'2','url':'https://test/2'}
            else:
                self.assertEqual(snapshot['entity_references']['props:cup']['shot_number'],2)
            shot.update(still_frame_status='ready',still_frame_url=f'https://test/{number}',still_frame_key=str(number))
        with patch.object(still,'_generate_still_frames_serial',side_effect=worker):
            still.generate_still_frames(result,job_id='isolated',emit=Mock())
        self.assertEqual(order,[1,2,3])
        self.assertEqual(set(result['shots'][2]['preview_dependencies']),{'2'})


if __name__ == '__main__':
    unittest.main()
