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
