import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, Mock, AsyncMock
from fastapi import FastAPI
from fastapi.testclient import TestClient
from botocore.exceptions import ClientError
from app.routes.voice_preview_routes import router
from app.services import voice_preview_service as preview
from app.services import generate_voice_previews as batch


class VoicePreviewTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI(); app.include_router(router); self.client = TestClient(app)

    def test_catalog_never_synthesizes_and_keeps_exact_ids(self):
        manifest = {v:{'key':preview.preview_key('Hindi',v),'duration_sec':3} for v in preview.SARVAM_VOICE_IDS}
        with patch.object(preview,'read_manifest',return_value=manifest), patch.object(preview.storage,'asset_url',side_effect=lambda key,**kw:'https://example.com/'+key), patch.object(batch,'_sarvam_tts',new_callable=AsyncMock) as tts:
            result = self.client.get('/api/voice-previews?language=Hindi')
            self.assertEqual(result.status_code,200)
            self.assertEqual([x['voice_id'] for x in result.json()['voices']],list(preview.SARVAM_VOICE_IDS))
            self.assertTrue(all(x['available'] for x in result.json()['voices']))
            tts.assert_not_called()

    def test_unknown_language_and_methods_do_not_generate(self):
        self.assertEqual(self.client.get('/api/voice-previews?language=Unknown').status_code,422)
        self.assertEqual(self.client.post('/api/voice-previews').status_code,405)

    def test_storage_failure_is_actionable(self):
        with patch.object(preview,'read_manifest',side_effect=RuntimeError('offline')):
            result=self.client.get('/api/voice-previews')
            self.assertEqual(result.status_code,503)
            self.assertIn('still choose and save',result.json()['detail'])

    def test_missing_sample_is_not_synthesized(self):
        with patch.object(preview,'read_manifest',return_value={}):
            result=self.client.get('/api/voice-previews').json()
            self.assertTrue(all(not x['available'] and x['url'] is None for x in result['voices']))

    def test_manifest_cannot_redirect_to_unrelated_key(self):
        with patch.object(preview,'read_manifest',return_value={'priya':{'key':'other/asset.wav'}}):
            result=self.client.get('/api/voice-previews').json()
            self.assertFalse(next(x for x in result['voices'] if x['voice_id']=='priya')['available'])

    def test_resume_skips_completed_voice(self):
        manifest={'priya':{'key':preview.preview_key('English','priya')}}
        client=Mock()
        client.put_object.side_effect=ClientError({'Error':{'Code':'PreconditionFailed'}},'PutObject')
        with tempfile.TemporaryDirectory() as folder, patch.object(batch,'read_manifest',return_value=manifest), patch.object(batch.storage,'_s3_client',return_value=client), patch.object(batch.storage,'_bucket',return_value='test'), patch.object(batch,'_sarvam_tts',new_callable=AsyncMock) as tts:
            asyncio.run(batch.generate(Path(folder),['English'],['priya']))
            tts.assert_not_called()
            self.assertEqual(client.put_object.call_args.kwargs['IfNoneMatch'],'*')
