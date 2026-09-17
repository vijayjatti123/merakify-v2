"""API/queue regression tests; provider mocks are not live-generation evidence."""
import base64
import io
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch
from PIL import Image
import test_module_f
from app.services import product_service as products, job_service, still_frame_service as still
from app.services.character_image_service import GeneratedCharacterImage


def image_bytes():
    out=io.BytesIO(); Image.new('RGB',(640,640),'white').save(out,format='PNG'); return out.getvalue()


class ProductTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)
        self.url_patch=patch('app.services.storage_service.asset_url', side_effect=lambda key:'https://example.invalid/'+key)
        self.url_patch.start()
        self.upload_patch=patch('app.services.storage_service.upload_bytes', side_effect=lambda key,*a,**kw:{'key':key,'url':'https://example.invalid/'+key})
        self.upload_patch.start()

    def tearDown(self):
        self.upload_patch.stop(); self.url_patch.stop(); test_module_f.ModuleFTests.tearDown(self)

    def upload(self):
        res=self.client.post('/api/products',data={'name':'Tea tin','crop':'[0,0,0.8,1]'},files={'file':('tin.png',image_bytes(),'image/png')})
        self.assertEqual(res.status_code,201,res.text)
        return res.json()

    def test_unapproved_blocked_and_original_approval_persists_job_snapshot_without_voice(self):
        row=self.upload()
        with self.sessions() as db:
            with self.assertRaises(ValueError): job_service.create_job(db,'Tea ad',product_ids=[row['id']])
        res=self.client.post(f"/api/products/{row['id']}/approve",json={'version':'original'})
        self.assertEqual(res.status_code,200)
        with self.sessions() as db:
            job=job_service.create_job(db,'Tea ad',product_ids=[row['id']])
            refs=products.job_references(db,job.id)
            self.assertEqual(refs[0]['object_key'],f"products/{row['id']}/crop.png")
            self.assertIn('Tea tin',job.brief)
            self.assertIsNone(job.resolutions_json)

    def test_queue_submit_once_refresh_review_then_explicit_approval(self):
        row=self.upload(); base=f"/api/products/{row['id']}"
        with patch.object(products,'fal_request',return_value={'request_id':'one'}) as provider:
            self.client.post(base+'/prepare'); self.client.post(base+'/prepare')
            self.assertEqual(provider.call_count,1)
        self.assertEqual(self.client.post(base+'/approve',json={'version':'original'}).status_code,409)
        with patch.object(products,'fal_request',side_effect=[{'status':'COMPLETED'},{'image':{'url':'data:image/png;base64,'+base64.b64encode(image_bytes()).decode()}}]):
            ready=self.client.get(base).json()
        self.assertEqual(ready['status'],'review'); self.assertIsNone(ready['accepted_url'])
        approved=self.client.post(base+'/approve',json={'version':'prepared'}).json()
        self.assertEqual(approved['status'],'approved')
        self.assertTrue(approved['accepted_url'].endswith('/prepared.png'))

    def test_provider_failure_preserves_original_and_stale_submission_recovers(self):
        row=self.upload(); base=f"/api/products/{row['id']}"
        with patch.object(products,'fal_request',side_effect=RuntimeError('private error')):
            failed=self.client.post(base+'/prepare').json()
        self.assertEqual(failed['status'],'failed'); self.assertNotIn('private error',failed['error'])
        self.assertEqual(failed['original_url'],row['original_url'])
        with self.sessions() as db:
            p=db.get(products.Product,row['id']); p.status='submitting'; p.started_at=datetime.utcnow()-timedelta(minutes=6); db.commit()
        self.assertEqual(self.client.get(base).json()['status'],'failed')
        self.assertEqual(self.client.post(base+'/approve',json={'version':'original'}).status_code,200)

    def test_invalid_crop_and_invalid_file_rejected_before_upload(self):
        for crop,data in [('[-1,0,1,1]',image_bytes()),('[0,0,1,1]',b'not image')]:
            res=self.client.post('/api/products',data={'name':'Tea','crop':crop},files={'file':('a.png',data,'image/png')})
            self.assertEqual(res.status_code,400)

    def test_idea_and_script_api_both_store_product_ids(self):
        row=self.upload(); self.client.post(f"/api/products/{row['id']}/approve",json={'version':'original'})
        for extra in [{},{'script_text':'A tea tin stands on the counter.','resolutions':{'characters':{},'locations':{}}}]:
            response=self.client.post('/api/jobs',json={'brief':'Tea tin ad','ai_model':'Seedance 2.0','product_ids':[row['id']],**extra})
            self.assertEqual(response.status_code,200,response.text)
            with self.sessions() as db:
                refs=products.job_references(db,response.json()['id'])
                self.assertEqual(refs[0]['product_id'],row['id'])

    def test_stale_approval_cannot_switch_an_already_approved_reference(self):
        row=self.upload()
        with self.sessions() as db:
            p=db.get(products.Product,row['id']); p.status='review'; p.prepared_key='prepared.png'; db.commit()
        with self.sessions() as first, self.sessions() as second:
            a=first.get(products.Product,row['id']); b=second.get(products.Product,row['id'])
            products.approve(first,a,'original')
            products.approve(second,b,'prepared')
            self.assertEqual(b.accepted_key,a.crop_key)

    def test_actual_still_path_passes_same_product_to_generation_and_qa(self):
        row=self.upload(); self.client.post(f"/api/products/{row['id']}/approve",json={'version':'original'})
        with self.sessions() as db:
            job=job_service.create_job(db,'Tea tin ad',product_ids=[row['id']]); ident=job.id
        image=GeneratedCharacterImage(image_bytes(),'image/png')
        result={'aspect_ratio':'1:1','continuity':{'characters':[]},'shots':[{'shot_number':1,'compiled_prompt':'Tea tin on a table.','characters_in_shot':[]}]}
        with patch('app.db.SessionLocal',self.sessions), patch.object(still,'_download_reference_image',return_value=image), patch.object(still,'generate_still',return_value=image) as gen, patch.object(still,'check_still',return_value={'approved':True}) as check:
            still.generate_still_frames(result,job_id=ident,emit=lambda *args:None)
        self.assertEqual(result['shots'][0]['still_frame_status'],'ready')
        self.assertEqual(gen.call_args.args[1],check.call_args.args[1])
        self.assertTrue(gen.call_args.args[1][0][2].startswith('product:'))
        self.assertEqual(result['shots'][0]['approved_product_references'][0]['product_id'],row['id'])

    def test_product_label_exception_is_narrow_in_image_generation_and_check(self):
        image=GeneratedCharacterImage(image_bytes(),'image/png')
        refs=[('Product Tea tin',image,'product:approved.png',0,0)]
        response={'candidates':[{'content':{'parts':[{'inlineData':{'mimeType':'image/png','data':base64.b64encode(image.data).decode()}}]}}]}
        with patch.object(still,'_google',return_value=response) as google:
            still.generate_still('Tea tin.',refs,'1:1')
            self.assertIn('Preserve existing product packaging',google.call_args.args[0][-1]['text'])
        with patch.object(still,'_google',return_value={'candidates':[{'content':{'parts':[{'text':'{"approved": true, "reason":"Matches"}'}]}}]}) as google:
            still.check_still('Tea tin.',refs,image)
            self.assertIn('must NOT be rejected',google.call_args.args[0][0]['text'])
        with patch.object(still,'_google',return_value=response) as google:
            still.generate_still('Plain cup.',[],'1:1')
            self.assertNotIn('Preserve existing product packaging',google.call_args.args[0][-1]['text'])

    def test_video_uses_scene_preview_and_preserves_existing_packaging(self):
        from app.services import video_generation_service as video
        shot={'compiled_prompt':'A tea tin on a table.', 'still_frame_url':'https://example.invalid/scene.png',
              'duration_sec':5,'has_dialogue':False,'characters_in_shot':[]}
        plain=video.translate({'aspect_ratio':'16:9','ai_model':'Seedance 2.0'},shot)
        shot['approved_product_references']=[{'product_id':'test'}]
        branded=video.translate({'aspect_ratio':'16:9','ai_model':'Seedance 2.0'},shot)
        self.assertEqual(plain['request']['image_urls'],branded['request']['image_urls'])
        self.assertEqual(branded['request']['image_urls'][0],'https://example.invalid/scene.png')
        self.assertIn('existing packaging',branded['request']['prompt'])
        self.assertNotIn('existing packaging',plain['request']['prompt'])


if __name__=='__main__': unittest.main()
