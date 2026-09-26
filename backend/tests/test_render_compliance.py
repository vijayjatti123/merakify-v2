import io
import json
import unittest
from unittest.mock import patch
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import VideoTask
from app.services import job_service as jobs, render_compliance_service as gate, video_generation_service as video, hedra_video_service as hedra


def verdict(style='pass', scale='pass'):
    return {'verdict': {k: {'status': s, 'observed': 'sample pixels', 'reason': 'photographic instead of anime' if s == 'mismatch' else s} for k,s in [('style',style),('scale',scale),('staging','pass')]}}


class ComplianceTests(unittest.TestCase):
    def test_video_checker_uses_the_same_shot_specific_look_as_generation(self):
        jobs.set_result(self.db, self.job.id, {'continuity': {
            'characters': [{'name': 'Yamaraj'}],
            'visual_style': {'rendering': 'Natural live action, lifelike skin texture',
                             'lighting_motif': 'Workshop light, glow around Yamaraj'}}})
        expected = gate.snapshot(self.db, self.job.id, {
            'characters_in_shot': [], 'speech_mode': 'none'})
        self.assertIn('Workshop light', expected['visual_style']['lighting_motif'])
        self.assertNotIn('Yamaraj', str(expected['visual_style']))
        self.assertEqual(expected['speech'], {'mode': 'none'})

    def test_product_inventory_reaches_video_checker_and_corrective_prompt(self):
        expected = gate.snapshot(self.db, self.job.id, {
            'characters_in_shot': [], 'speech_mode': 'none',
            'shot_direction': {'product_props': 'One Bluewater bottle on the workbench; same unit held at end.',
                               'critical_outcome': 'The bottle stays intact after the drop.'}})
        self.assertEqual(expected['staging']['product_props'],
            'One Bluewater bottle on the workbench; same unit held at end.')
        correction = gate.visual_retry_instruction(expected, ['staging'])
        self.assertIn('approved number and placement', correction)
        self.assertIn('One Bluewater bottle', correction)

    def test_product_only_retry_is_concise_and_does_not_repeat_character_ban(self):
        expected={'visible_characters': [], 'staging': {
            'start':'Bucket alone on a workbench', 'critical_outcome':'Bucket label stays visible',
            'forbidden_geometry':['Any human hands or characters entering frame']}}
        correction=gate.visual_retry_instruction(expected, ['staging'])
        self.assertIn('approved opening subjects', correction)
        self.assertIn('Bucket label stays visible', correction)
        self.assertNotIn('human hands', correction)
        self.assertNotIn('characters entering frame', correction)
        self.assertNotIn('{', correction)

    def setUp(self):
        self.engine=create_engine('sqlite://');Base.metadata.create_all(self.engine)
        self.db=Session(self.engine)
        self.job=jobs.create_job(self.db,'Module T test',visual_style='Cartoon / Anime')
        jobs.set_status(self.db,self.job.id,'done')
        jobs.claim_video(self.db,self.job.id,1,{'video_status':'processing','video_task_id':'first',
            'video_compliance_expected':{'visual_style':'Cartoon / Anime'},'video_retry_request':{'prompt':'unchanged'},'video_compliance_retries':0})
        jobs.update_video(self.db,self.job.id,1,video_status='processing')
        self.shot={'shot_number':1,'video_task_id':'first'}
    def tearDown(self):
        self.db.close();self.engine.dispose()
    def data(self):
        self.db.expire_all();return json.loads(self.db.query(VideoTask).one().data_json)
    def check(self):return gate.accept(self.db,self.job.id,self.shot,io.BytesIO(b'video'))
    def test_pass_no_regeneration_cached_after_storage_failure(self):
        with patch.object(gate,'inspect',return_value=verdict()) as vision,patch.object(video,'provider') as api:
            self.assertTrue(self.check());self.assertTrue(self.check())
            vision.assert_called_once();api.assert_not_called()

    def test_persisted_frame_observations_do_not_block_completed_video(self):
        check = verdict()
        check['verdict']['frame_observations'] = [
            {'frame_index': 1, 'observed': 'carpenter on stool',
             'later_beat_already_visible': False, 'reason': 'opening position'},
            {'frame_index': 2, 'observed': 'carpenter falling',
             'later_beat_already_visible': False, 'reason': 'action advances'},
        ]
        # Reproduce the production recovery path: the check was already saved
        # before the completion worker failed, so polling must reuse it.
        jobs.video_check_state(self.db, self.job.id, 1, 'first', check=check)
        with patch.object(gate, 'inspect', side_effect=AssertionError('must reuse saved check')), \
             patch.object(video, 'provider') as api:
            self.assertTrue(self.check())
        api.assert_not_called()
        self.assertEqual(self.data()['video_compliance_checks']['first'], check)

    def test_computed_check_survives_exhausted_database_save_retries(self):
        from sqlalchemy.orm import Query
        original = Query.update
        remaining = [3]
        def conflict(query, values, **kwargs):
            if remaining[0]:
                remaining[0] -= 1
                return 0
            return original(query, values, **kwargs)
        cache = {}
        with patch.object(Query,'update',conflict), patch.object(gate,'inspect',return_value=verdict()) as vision:
            with self.assertRaises(jobs.VideoCheckConflict):
                gate.accept(self.db,self.job.id,self.shot,io.BytesIO(b'video'),check_cache=cache)
            self.assertTrue(gate.accept(self.db,self.job.id,self.shot,io.BytesIO(b'video'),check_cache=cache))
            vision.assert_called_once()
        self.assertEqual(self.data()['video_compliance_checks']['first'], verdict())
    def test_mismatch_one_retry_then_stops_for_review(self):
        with patch.object(gate,'inspect',return_value=verdict('mismatch')),patch.object(video,'provider',return_value={'id':'second'}) as api:
            self.assertFalse(self.check());self.assertFalse(self.check())
            self.assertEqual(api.call_count,1)
            self.assertIn('Automatic corrective retry',api.call_args.args[2]['prompt'])
            self.assertIn('Cartoon / Anime',api.call_args.args[2]['prompt'])
            self.shot['video_task_id']='second'
            self.assertFalse(self.check());self.assertEqual(api.call_count,1)
            self.assertEqual(self.data()['video_status'], 'review_required')
    def test_misspelled_product_label_is_advisory_without_paid_video_retry(self):
        check = verdict()
        check['verdict']['staging'] = {'status':'mismatch', 'observed':'bucket lettering',
                                       'reason':'Violates forbidden geometry rule against misspelled brand markings'}
        with patch.object(gate, 'inspect', return_value=check), patch.object(video, 'provider') as api:
            self.assertTrue(self.check())
            api.assert_not_called()
        self.assertEqual(self.data()['video_status'], 'processing')
        self.assertEqual(self.data()['video_compliance_retries'], 0)
        self.assertIn('Product lettering may be inaccurate', self.data()['video_warnings'][0])
    def test_saved_label_video_can_resume_without_a_new_render(self):
        check = verdict()
        check['verdict']['staging'] = {'status':'mismatch', 'observed':'bucket lettering',
                                       'reason':'Violates forbidden geometry rule against misspelled brand markings'}
        jobs.video_check_state(self.db, self.job.id, 1, 'first', check=check)
        jobs.stop_video(self.db, self.job.id, 1, 'first')
        jobs.resume_saved_label_video(self.db, self.job.id, 1, 'first')
        self.assertEqual(self.data()['video_status'], 'processing')
        self.assertFalse(self.data().get('video_stop_requested'))
        with patch.object(gate, 'inspect', side_effect=AssertionError('reuse saved verdict')), \
             patch.object(video, 'provider') as api:
            self.assertTrue(self.check())
        api.assert_not_called()

    def test_silent_speech_is_removed_from_saved_video_before_paid_retry(self):
        check = verdict()
        issue = {'kind': 'extra_speech', 'start_sec': 3.0, 'end_sec': 4.9,
                 'evidence': 'invented words'}
        check['speech_check'] = {'verdict': {'status': 'mismatch', 'confidence': .95,
                                             'transcript': 'invented words', 'reason': 'extra voice',
                                             'issues': [issue]}}
        check['verdict']['speech'] = {'status': 'mismatch', 'observed': 'invented words',
                                       'reason': 'Unexpected speech in a silent shot.'}
        jobs.update_video(self.db, self.job.id, 1,
                          video_compliance_expected={'speech': {'mode': 'none'}},
                          video_compliance_retries=1)
        jobs.video_check_state(self.db, self.job.id, 1, 'first', check=check)
        jobs.update_video(self.db, self.job.id, 1, video_status='review_required')
        jobs.resume_saved_label_video(self.db, self.job.id, 1, 'first')
        media = io.BytesIO(b'original')
        with patch('app.services.speech_compliance_service.remove_unwanted_speech', return_value=b'cleaned') as repair, \
             patch('app.services.speech_compliance_service.extract_audio', return_value=(b'audio', 5.0)), \
             patch('app.services.speech_compliance_service.inspect_audio', return_value={
                 'verdict': {'status': 'pass', 'confidence': 1.0, 'transcript': '',
                             'reason': 'no speech', 'issues': []}}), \
             patch.object(video, 'provider') as api:
            self.assertTrue(gate.accept(self.db, self.job.id, self.shot, media, check_cache={}))
            api.assert_not_called()
        self.assertEqual(media.getvalue(), b'cleaned')
        self.assertEqual(self.data()['video_speech_check']['status'], 'pass')
        self.assertEqual(self.data()['video_audio_cleanup']['issues'], [issue])
        repair.assert_called_once()

    def test_failed_silent_cleanup_stops_without_another_paid_render(self):
        check = verdict()
        check['speech_check'] = {'verdict': {'status': 'mismatch', 'confidence': .95,
            'transcript': 'unexpected', 'reason': 'extra voice', 'issues': [
                {'kind': 'extra_speech', 'start_sec': 3, 'end_sec': 4, 'evidence': 'word'}]}}
        check['verdict']['speech'] = {'status': 'mismatch', 'reason': 'Unexpected speech in a silent shot.'}
        jobs.update_video(self.db, self.job.id, 1,
                          video_compliance_expected={'speech': {'mode': 'none'}})
        jobs.video_check_state(self.db, self.job.id, 1, 'first', check=check)
        with patch('app.services.speech_compliance_service.remove_unwanted_speech',
                   side_effect=ValueError('cannot repair')), patch.object(video, 'provider') as api:
            self.assertFalse(self.check())
            api.assert_not_called()
        self.assertEqual(self.data()['video_status'], 'review_required')
        self.assertEqual(self.data()['video_compliance_retries'], 0)
        with self.assertRaisesRegex(ValueError, 'different correction'):
            jobs.resume_saved_label_video(self.db, self.job.id, 1, 'first')
    def test_user_stop_prevents_later_worker_updates_and_retries(self):
        self.assertTrue(jobs.stop_video(self.db, self.job.id, 1, 'first'))
        self.assertEqual(self.data()['video_status'], 'review_required')
        self.assertTrue(self.data()['video_stop_requested'])
        self.assertFalse(jobs.update_video(self.db, self.job.id, 1, video_status='done'))
        with patch.object(gate, 'inspect') as vision, patch.object(video, 'provider') as api:
            self.assertFalse(self.check())
            vision.assert_not_called(); api.assert_not_called()
    def test_user_stop_recovers_indexed_status_mismatch(self):
        row = self.db.query(VideoTask).one()
        row.status = 'review_required'  # UI JSON still reports processing.
        self.db.commit()
        self.assertTrue(jobs.stop_video(self.db, self.job.id, 1, 'first'))
        self.assertEqual(self.data()['video_status'], 'review_required')
        self.assertTrue(self.data()['video_stop_requested'])
    def test_worker_release_does_not_reopen_terminal_review(self):
        token = 'leased-once'
        self.assertIsNotNone(jobs.video_worker_lease(self.db, self.job.id, 1, 'first', token))
        jobs.update_video(self.db, self.job.id, 1, video_status='review_required',
                          video_error='Unexpected speech in a silent shot.')
        jobs.video_worker_lease(self.db, self.job.id, 1, None, token, release=True)
        self.assertEqual(self.data()['video_status'], 'review_required')
        self.assertEqual(self.db.query(VideoTask).one().status, 'review_required')
    def test_checker_failure_fails_open_no_paid_retry(self):
        with patch.object(gate,'inspect',side_effect=TimeoutError()),patch.object(video,'provider') as api:
            self.assertTrue(self.check());api.assert_not_called()
            self.assertIn('not verified (TimeoutError)',self.data()['video_warnings'][0])
    def test_corrected_retry_passes(self):
        with patch.object(gate,'inspect',side_effect=[verdict(scale='mismatch'),verdict()]),patch.object(video,'provider',return_value={'id':'second'}) as api:
            self.assertFalse(self.check());self.shot['video_task_id']='second';self.assertTrue(self.check());api.assert_called_once()
    def test_hedra_same_audio_request_and_no_seedance(self):
        request={'input':{'audio':{'url':'same-audio'},'start_image':{'url':'same-image'}}}
        jobs.update_video(self.db,self.job.id,1,video_provider='hedra',video_retry_request=request,video_reference_source='module_o_still')
        with patch.object(gate,'inspect',return_value=verdict('mismatch')),patch.object(hedra,'api',return_value={'job_id':'hedra-second'}) as api,patch.object(video,'provider') as seed:
            self.assertFalse(self.check());seed.assert_not_called();self.assertEqual(api.call_args.kwargs['body'],request)
    def test_legacy_portrait_retry_blocked(self):
        jobs.update_video(self.db,self.job.id,1,video_provider='hedra',video_reference_source='vault_style_resolved')
        with patch.object(gate,'inspect',return_value=verdict('mismatch')), patch.object(hedra,'api') as api:
            with self.assertRaisesRegex(hedra.MediaValidationError, 'portrait retry was blocked'):
                self.check()
            api.assert_not_called()

    def test_uncertain_retry_does_not_spend_again(self):
        with patch.object(gate,'inspect',return_value=verdict('mismatch')),patch.object(video,'provider',side_effect=TimeoutError()) as api:
            self.assertTrue(self.check());self.assertTrue(self.check());api.assert_called_once()
            self.assertTrue(self.data()['video_retry_submission_unknown'])
    def test_claim_is_once_across_sessions(self):
        first=jobs.video_check_state(self.db,self.job.id,1,'first',claim_retry=True)
        self.assertEqual(first['video_compliance_retries'],1)
        with Session(self.engine) as other:
            self.assertIsNone(jobs.video_check_state(other,self.job.id,1,'first',claim_retry=True))

    def test_malformed_vision_json_is_not_a_style_rejection(self):
        with patch.object(gate,'frames',return_value=[(0,{'inlineData':{}})]), patch.object(gate,'_google',return_value={'candidates':[{'content':{'parts':[{'inlineData':{'data':'image'}}]}}]}):
            with self.assertRaises(ValueError):gate.inspect(io.BytesIO(),{'visual_style':'Cartoon / Anime'})
    def test_missing_still_cannot_produce_a_scale_rejection(self):
        response={**verdict(scale='mismatch')['verdict'],
                  'frame_observations':[{'frame_index':1,'observed':'person waiting',
                                         'later_beat_already_visible':False,'reason':'opening frame'}]}
        raw={'candidates':[{'content':{'parts':[{'text':json.dumps(response)}]}}]}
        with patch.object(gate,'frames',return_value=[(0,{'inlineData':{}})]),patch.object(gate,'_google',return_value=raw):
            result=gate.inspect(io.BytesIO(),{'visual_style':'Cartoon / Anime'})
            self.assertEqual(result['verdict']['scale']['status'],'unverified')
    def test_early_later_beat_forces_staging_mismatch(self):
        response={**verdict()['verdict'], 'frame_observations':[
            {'frame_index':1,'observed':'jumper at door','later_beat_already_visible':False,'reason':'opening'},
            {'frame_index':2,'observed':'deployed canopy','later_beat_already_visible':True,'reason':'early payoff'}]}
        raw={'candidates':[{'content':{'parts':[{'text':json.dumps(response)}]}}]}
        with patch.object(gate,'frames',return_value=[(0,{'inlineData':{}}),(2,{'inlineData':{}})]), \
             patch.object(gate,'_google',return_value=raw) as checker:
            result=gate.inspect(io.BytesIO(),{'staging':{'action_beats':['jump','freefall','deploy'],
                                                         'planned_duration_sec':6}})
        self.assertEqual(result['verdict']['staging']['status'],'mismatch')
        self.assertTrue(checker.call_args.kwargs['verification'])
        self.assertEqual(result['model'],gate.settings.gemini_preview_check_model)
    def test_snapshot_uses_real_job_field_not_result_prose(self):
        value=gate.snapshot(self.db,self.job.id,{'camera_angle':'eye-level close-up'})
        self.assertEqual(value['visual_style'],'Cartoon / Anime')

if __name__=='__main__':unittest.main()
