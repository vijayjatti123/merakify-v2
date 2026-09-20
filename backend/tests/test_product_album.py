"""Album contract/recovery tests. Providers and storage are mocked, SQLite is real."""
import base64
import uuid
import unittest
from unittest.mock import patch
from test_products import image_bytes
import test_module_f
from app.models import Product, ProductView
from app.services import product_album_service as service, job_service, product_service

class AlbumTests(unittest.TestCase):
    def setUp(self):
        test_module_f.ModuleFTests.setUp(self)
        self.patches = [patch.object(service, 'SessionLocal', self.sessions),
                        patch.object(service.storage, 'asset_url', side_effect=lambda key: 'https://example.invalid/'+key),
                        patch.object(service.storage, 'upload_bytes')]
        for p in self.patches: p.start()
        with self.sessions() as db:
            p=Product(name='Tea tin',original_key='original.png',crop_key='crop.png',accepted_key='master.png',status='approved')
            db.add(p); db.commit(); self.pid=p.id
        self.base=f'/api/products/{self.pid}/album'
    def tearDown(self):
        for p in self.patches: p.stop()
        test_module_f.ModuleFTests.tearDown(self)
    def view(self, **kw):
        with self.sessions() as db:
            row=ProductView(product_id=self.pid,request_key=str(uuid.uuid4()),angle='side',provenance='inferred',model='standard',**kw)
            db.add(row);db.commit();return row.id
    def state(self, ident):
        with self.sessions() as db:
            r=db.get(ProductView,ident)
            return dict(status=r.status,attempts=r.attempts,key=r.object_key,status_url=r.status_url,response_url=r.response_url)
    def test_queue_idempotency_and_changed_request_rejected(self):
        payload=dict(angles=['side','top'],model='standard',request_key=str(uuid.uuid4()),max_generation_usd=.48)
        a=self.client.post(self.base+'/prepare',json=payload)
        b=self.client.post(self.base+'/prepare',json=payload)
        self.assertEqual(a.status_code,202,a.text); self.assertEqual(a.json(),b.json())
        payload['angles']=['side']
        self.assertEqual(self.client.post(self.base+'/prepare',json=payload).status_code,409)

    def test_generation_and_verifier_use_one_persisted_contract(self):
        payload=dict(angles=['three_quarter'],model='standard',request_key=str(uuid.uuid4()),max_generation_usd=.24)
        response=self.client.post(self.base+'/prepare',json=payload)
        self.assertEqual(response.status_code,202,response.text)
        public=response.json()['views'][0]
        with self.sessions() as db:
            row=db.get(ProductView,public['id'])
            persisted=row.generation_contract
            generation=service.generation_prompt(row)
            verification=service.verification_instruction(row)
        serialized=__import__('json').dumps(persisted,ensure_ascii=False,sort_keys=True)
        self.assertIn(serialized,generation)
        self.assertIn(serialized,verification)
        self.assertEqual(public['generation_requirements'],persisted)
        self.assertIn('25-35 degrees',persisted['requested_view'])
        self.assertIn('add no unseen text',persisted['reference_policy'])

    def test_every_angle_has_distinct_measurable_staging(self):
        contracts=[service.view_contract(angle,1)['requested_view'] for angle in service.ANGLES]
        self.assertEqual(len(contracts),len(set(contracts)))
        for requirement in contracts:
            self.assertGreater(len(requirement),45)
    def test_saved_queue_urls_used_and_image_saved(self):
        ident=self.view(status='queued',source_keys=['master.png'])
        urls=dict(request_id='one',status_url='https://queue.fal.run/fal-ai/nano-banana-2/requests/one/status',response_url='https://queue.fal.run/fal-ai/nano-banana-2/requests/one')
        with patch.object(service,'fal',return_value=urls) as call: service.process(ident)
        self.assertEqual(call.call_count,1)
        self.assertEqual(self.state(ident)['status_url'],urls['status_url'])
        encoded='data:image/png;base64,'+base64.b64encode(image_bytes()).decode()
        with patch.object(service,'fal',side_effect=[{'status':'COMPLETED'},{'images':[{'url':encoded}]}]) as call:
            service.process(ident)
        self.assertEqual([c.kwargs['url'] for c in call.call_args_list],[urls['status_url'],urls['response_url']])
        self.assertEqual(self.state(ident)['status'],'verifying')
        self.assertTrue(self.state(ident)['key'])
    def test_verification_retry_never_generates_even_when_check_fails(self):
        ident=self.view(status='verifying',object_key='candidate.png',attempts=1)
        with patch.object(service,'verify',side_effect=TimeoutError()): service.process(ident)
        self.assertEqual(self.state(ident)['status'],'verification_unavailable')
        self.assertEqual(self.client.post(self.base+f'/{ident}/verify').status_code,200)
        with patch.object(service,'verify',return_value={'branding':{'status':'fail','evidence':'Changed logo'}}), patch.object(service,'fal') as generate:
            service.process(ident); service.process(ident)
        generate.assert_not_called()
        self.assertEqual(self.state(ident),dict(status='rejected',attempts=1,key='candidate.png',status_url=None,response_url=None))
    def test_initial_correction_is_bounded(self):
        ident=self.view(status='verifying',object_key='candidate.png',attempts=1)
        with patch.object(service,'verify',return_value={'branding':{'status':'fail','evidence':'Changed logo'}}): service.process(ident)
        self.assertEqual(self.state(ident)['status'],'queued')
        with self.sessions() as db:
            row=db.get(ProductView,ident);row.status='verifying';row.attempts=2;db.commit()
        with patch.object(service,'verify',return_value={'branding':{'status':'fail','evidence':'Changed logo'}}): service.process(ident)
        self.assertEqual(self.state(ident)['status'],'rejected')
    def test_acknowledgement_withdraw_and_immutable_job_snapshot(self):
        ident=self.view(status='review',object_key='candidate.png')
        self.assertEqual(self.client.post(self.base+'/approve',json={'view_ids':[ident]}).status_code,409)
        self.assertEqual(self.client.post(self.base+'/approve',json={'view_ids':[ident],'inferred_details_reviewed':True}).status_code,200)
        with self.sessions() as db:
            job=job_service.create_job(db,'Tea ad',product_ids=[self.pid]); jid=job.id
            before=product_service.job_references(db,jid)
        self.assertEqual(self.client.post(self.base+f'/{ident}/withdraw').status_code,200)
        with self.sessions() as db:
            self.assertEqual(service.approved_snapshot(db,self.pid),[])
            self.assertEqual(product_service.job_references(db,jid),before)
    def test_uncertainty_visible_and_no_image_cannot_be_approved(self):
        ident=self.view(status='verifying',object_key='candidate.png')
        verdict={'branding':{'status':'uncertain','evidence':'Rear label not visible in source'}}
        with patch.object(service,'verify',return_value=verdict): service.process(ident)
        data=self.client.get(self.base).json()['views'][0]
        self.assertEqual(data['status'],'review');self.assertEqual(data['verdict'],verdict)
        empty=self.view(status='review')
        self.assertEqual(self.client.post(self.base+'/approve',json={'view_ids':[empty],'inferred_details_reviewed':True}).status_code,409)
    def test_untrusted_queue_url_rejected(self):
        for url in ['http://queue.fal.run/fal-ai/test','https://evil.invalid/fal-ai/test','https://queue.fal.run@evil.invalid/fal-ai/test']:
            with self.assertRaises(ValueError): service.queue_url(url)

    def test_legacy_schema_upgrade_is_idempotent(self):
        from sqlalchemy import create_engine, inspect
        from app import db as database
        engine = create_engine('sqlite://')
        with engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE job_products (job_id TEXT)')
            connection.exec_driver_sql('CREATE TABLE product_views (id TEXT)')
            connection.exec_driver_sql("INSERT INTO product_views VALUES ('legacy')")
        with patch.object(database, 'engine', engine):
            database.ensure_product_album_columns()
            database.ensure_product_album_columns()
        self.assertTrue({'status_url', 'response_url', 'generation_contract', 'verification_only'} <=
                        {c['name'] for c in inspect(engine).get_columns('product_views')})
        with engine.connect() as connection:
            self.assertEqual(connection.exec_driver_sql('SELECT verification_only FROM product_views').scalar(), 0)
        engine.dispose()

if __name__=='__main__': unittest.main()
