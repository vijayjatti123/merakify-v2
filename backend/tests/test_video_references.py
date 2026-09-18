import copy
import unittest
from unittest.mock import patch
from app.services import video_references as refs, video_generation_service as video, audio_video_service as audio
from app.video_models import AUTOMATIC

class ReferenceTests(unittest.TestCase):
    def setUp(self):
        self.shot=dict(shot_number=1, description='Meera waits; Tara enters.', characters_in_shot=['Meera','Tara'], opening_characters=['Meera'], still_frame_url='https://example.com/scene.jpg', still_frame_status='ready', compiled_prompt='Tara enters and Meera looks up.', duration_sec=5, has_dialogue=False)
        self.result=dict(ai_model='Seedance 2.0', video_model=AUTOMATIC, quality='720p', aspect_ratio='16:9', continuity={'characters':[dict(name=n,character_id=n,image_url=f'https://example.com/{n}.jpg',reference_sheet_url=f'https://example.com/{n}-views.jpg') for n in ('Meera','Tara')]})
    def build(self, **kwargs):
        return refs.build(self.result,self.shot,limit=kwargs.get('limit',10),tag_style=kwargs.get('tag_style','omni'),refresh=lambda u:u)
    def test_full_cast_and_views_with_entrance_and_exact_order(self):
        before=copy.deepcopy((self.result,self.shot)); r=self.build()
        self.assertEqual(r['images'],['https://example.com/'+x+'.jpg' for x in ('scene','Meera','Tara','Meera-views','Tara-views')])
        self.assertEqual([e['tag'] for e in r['manifest']],[f'<IMAGE_REF_{n}>' for n in range(5)])
        self.assertIn('enters later',r['manifest'][2]['roles'][0]['instruction'])
        self.assertIn('SAME Tara',r['manifest'][4]['roles'][0]['instruction'])
        self.assertEqual(before,(self.result,self.shot))
    def test_seedance_indices_and_dedup(self):
        self.result['continuity']['characters'][0]['reference_sheet_url']='https://example.com/Meera.jpg?token=another'
        r=self.build(tag_style='fal');self.assertEqual(len(r['images']),4)
        self.assertEqual(r['manifest'][1]['tag'],'@Image2');self.assertEqual(len(r['manifest'][1]['roles']),2)
        self.assertEqual(self.build(tag_style='evolink')['manifest'][2]['tag'],'@image3')
    def test_required_limit_blocks_optional_limit_warns(self):
        with self.assertRaisesRegex(ValueError,'required'):self.build(limit=2)
        r=self.build(limit=3);self.assertEqual(len(r['images']),3);self.assertEqual(len(r['warnings']),2)
    def test_missing_later_identity_and_ambiguous_binding_rejected(self):
        self.result['continuity']['characters'][1].pop('image_url')
        with self.assertRaisesRegex(ValueError,'identity'):self.build()
        self.result['continuity']['characters'].append(self.result['continuity']['characters'][0])
        with self.assertRaisesRegex(ValueError,'Ambiguous'):self.build()
    def test_style_sheet_not_silently_mixed(self):
        self.result['continuity']['characters'][0]['style_variant_id']='anime'
        r=self.build();self.assertEqual(len(r['images']),4);self.assertIn('style variant',r['warnings'][0])
    def test_global_prose_does_not_select_products(self):
        self.shot['compiled_prompt']+=' Use Fevicol style.'
        self.shot['approved_product_references']=[dict(name='Fevicol',product_id='p',object_key='p.png')]
        with patch('app.services.storage_service.asset_url',return_value='https://example.com/p.png') as url:
            self.assertEqual(len(self.build()['images']),5);url.assert_not_called()
            self.shot['description']+=' Fevicol bucket on table.'
            r=self.build();self.assertEqual(r['manifest'][3]['roles'][0]['kind'],'product')
    def test_unbound_tags_and_length_rejected(self):
        r=self.build()
        for prompt in ('Use <IMAGE_REF_8>', 'Use @Image1', 'x'*20001):
            with self.assertRaises(ValueError):refs.check_prompt(prompt,r['manifest'],omni=True)
    def test_automatic_uses_mini_with_full_reference_set(self):
        t=video.translate(self.result,self.shot)
        self.assertEqual(t['model'],'seedance-2.0-mini-reference-to-video');self.assertNotIn('image_url',t['request']);self.assertEqual(len(t['request']['image_urls']),5)

class HydrationTests(unittest.TestCase):
    def test_legacy_job_hydrates_views_by_id_without_persisting_changes(self):
        import json
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.db import Base
        from app.models import Character
        from app.services import job_service as jobs
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        with Session(engine) as db:
            db.add(Character(id='real-id',name='Internal',description='Test',image_url='https://example.com/person.jpg',image_source='uploaded',status='approved',reference_sheet_url='https://example.com/views.jpg'))
            db.commit()
            job=jobs.create_job(db,'Test',visual_style='Natural')
            result={'shots':[{'shot_number':1}], 'continuity':{'characters':[{'name':'Alias','character_id':'real-id','image_url':'https://example.com/person.jpg'}]}}
            jobs.set_result(db,job.id,result);jobs.set_status(db,job.id,'done')
            original=job.result_json
            hydrated,shot=jobs.video_source(db,job.id,1)
            self.assertEqual(hydrated['continuity']['characters'][0]['reference_sheet_url'],'https://example.com/views.jpg')
            db.refresh(job);self.assertEqual(job.result_json,original)
            result['continuity']['characters'][0]['style_variant_id']='anime'
            jobs.set_result(db,job.id,result)
            hydrated,_=jobs.video_source(db,job.id,1)
            self.assertNotIn('reference_sheet_url',hydrated['continuity']['characters'][0])
        engine.dispose()

    def test_durable_manifest_and_retry_keep_exact_reference_request(self):
        import io,json
        from sqlalchemy import create_engine
        from sqlalchemy.orm import Session
        from app.db import Base
        from app.models import VideoTask
        from app.services import job_service as jobs, render_compliance_service as gate
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        shot=dict(shot_number=1,description='Meera waves',characters_in_shot=['Meera'],duration_sec=5,compiled_prompt='Meera waves.',still_frame_url='https://example.com/scene.jpg',still_frame_status='ready',has_dialogue=False)
        result=dict(shots=[shot],generation_approved=True,continuity={'characters':[dict(name='Meera',character_id='id',image_url='https://example.com/meera.jpg')]})
        with Session(engine) as db, patch.object(video.settings,'evolink_api_key','test'):
            job=jobs.create_job(db,'test',ai_model='Seedance 2.0',video_model=AUTOMATIC,quality='720p')
            jobs.set_result(db,job.id,result);jobs.set_status(db,job.id,'done')
            with patch.object(video,'provider',return_value={'id':'one'}) as submit:
                video.start(db,job.id,1)
                sent=submit.call_args.args
            data=json.loads(db.query(VideoTask).one().data_json)
            self.assertEqual(data['video_reference_manifest'][1]['roles'][0]['id'],'id')
            self.assertEqual(sent,('POST','/v1/videos/generations',data['video_retry_request']))
            mismatch={'verdict':{'style':{'status':'pass','observed':'photo','reason':'match'},'scale':{'status':'mismatch','observed':'wide','reason':'medium required'}}}
            with patch.object(gate,'inspect',return_value=mismatch),patch.object(video,'provider',return_value={'id':'two'}) as submit:
                self.assertFalse(gate.accept(db,job.id,{'shot_number':1,'video_task_id':'one'},io.BytesIO()))
                self.assertEqual(submit.call_args.args,sent)
        engine.dispose()

if __name__=='__main__':unittest.main()
