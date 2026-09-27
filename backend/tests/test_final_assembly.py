import copy
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, Mock

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.db import Base
from app.models import VideoTask
from app.services import final_assembly_service as assembly, job_service as jobs


class AssemblyTests(unittest.TestCase):
    def setUp(self):
        self.job=SimpleNamespace(status='done',aspect_ratio='16:9',quality='480p')
        self.result={'shots':[{'shot_number':i,'video_url':f'https://example.com/{i}.mp4','video_status':'done'} for i in [3,1,2]],'assembly':{'transitions':[{'between':'1-2','type':'cut'},{'between':'2-3','type':'crossfade'}]}}
    def test_missing_precondition_names_all_shots_before_media(self):
        self.result['shots'][0]['video_url']=None;self.result['shots'][2]['video_url']=''
        with patch.object(assembly,'ffmpeg') as ff,patch.object(assembly.httpx,'stream') as download:
            with self.assertRaisesRegex(assembly.AssemblyError,'shot\\(s\\) 2, 3'):
                assembly.prepare(self.job,self.result)
            ff.assert_not_called();download.assert_not_called()
    def test_sort_and_exact_transition_mapping(self):
        plan=assembly.prepare(self.job,self.result)
        self.assertEqual([s['shot_number'] for s in plan['shots']],[1,2,3])
        self.result['assembly']['transitions'][0]['type']='match cut'
        plan=assembly.prepare(self.job,self.result)
        graph,_,_,timeline=assembly.filter_graph(plan,[{'video_duration':2,'audio_duration':2}]*3)
        self.assertEqual(graph.count('xfade='),1);self.assertEqual(graph.count('acrossfade='),1)
        self.assertEqual(timeline['boundaries'][0]['overlap_sec'],0)
        self.assertEqual(timeline['duration'],5.5)
    def test_missing_unknown_duplicate_transition_rejected(self):
        for transitions in [[],[{'between':'1-2','type':'wipe'}],[{'between':'1-2','type':'cut'}]*2]:
            self.result['assembly']['transitions']=transitions
            with self.assertRaises(assembly.AssemblyError):assembly.prepare(self.job,self.result)
    def test_stale_video_rejected_and_signed_url_refresh_not_stale(self):
        first=assembly.fingerprint(self.result,'16:9','480p')
        self.result['shots'][0]['video_url']+='?signature=renewed'
        self.assertEqual(first,assembly.fingerprint(self.result,'16:9','480p'))
        self.result['shots'][0]['video_source_changed']=True
        with self.assertRaises(assembly.AssemblyError):assembly.prepare(self.job,self.result)
    def test_silent_source_can_supply_a_shorter_final_cut_without_regeneration(self):
        self.result['shots'][1]['video_edit_range']={'start_sec':0.4,'end_sec':1.4}
        plan=assembly.prepare(self.job,self.result)
        graph,_,_,timeline=assembly.filter_graph(plan,[{'video_duration':2,'audio_duration':2}]*3)
        self.assertIn('trim=start=0.400000000:end=1.400000000',graph)
        self.assertIn('atrim=start=0.400000000:end=1.400000000',graph)
        self.assertAlmostEqual(timeline['duration'],4.5)
        self.assertNotEqual(assembly.fingerprint(self.result,'16:9','480p'),
                            assembly.fingerprint({**self.result,'shots':[
                                {**shot,'video_edit_range':None} for shot in self.result['shots']]},'16:9','480p'))
    def test_speaking_footage_cannot_be_cut_without_an_audio_timing_plan(self):
        self.result['shots'][0].update(has_dialogue=True,speech_mode='onscreen',
                                       video_edit_range={'start_sec':0.2,'end_sec':1.2})
        with self.assertRaisesRegex(assembly.AssemblyError,'approved audio timing'):
            assembly.prepare(self.job,self.result)
    def test_selection_saves_only_for_current_ready_silent_clip(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        with Session(engine) as db:
            job=jobs.create_job(db,'test')
            current={'video_status':'done','video_key':'clips/current.mp4','video_task_id':'task-1'}
            db.add(VideoTask(job_id=job.id,shot_number=1,status='done',data_json=json.dumps(current)))
            db.commit()
            shot={'shot_number':1,**current,'speech_mode':'none','has_dialogue':False}
            with patch.object(jobs,'job_result',return_value={'shots':[shot]}):
                selected={'start_sec':0.4,'end_sec':1.4}
                self.assertEqual(jobs.set_video_edit_range(db,job.id,1,'clips/current.mp4',selected)['video_edit_range'],selected)
                with self.assertRaisesRegex(ValueError,'changed'):
                    jobs.set_video_edit_range(db,job.id,1,'clips/old.mp4',selected)
                shot['speech_mode']='voiceover'
                with self.assertRaisesRegex(ValueError,'audio timing'):
                    jobs.set_video_edit_range(db,job.id,1,'clips/current.mp4',selected)
            saved=json.loads(db.query(VideoTask).one().data_json)
            self.assertEqual(saved['video_edit_range'],selected)
        engine.dispose()
    def test_no_partial_endpoint_or_background_task(self):
        from app.routes.jobs import assemble_final_video
        from fastapi import HTTPException
        tasks=Mock();self.result['shots'][2]['video_url']=None
        with patch.object(jobs,'get_job',return_value=self.job),patch.object(jobs,'job_result',return_value=self.result),patch.object(jobs,'claim_final_assembly') as claim:
            with self.assertRaises(HTTPException) as error:assemble_final_video('id',tasks,Mock())
            self.assertEqual(error.exception.status_code,409);self.assertIn('shot(s) 2',error.exception.detail)
            claim.assert_not_called();tasks.add_task.assert_not_called()
    def test_durable_claim_and_late_completion_guard(self):
        engine=create_engine('sqlite://');Base.metadata.create_all(engine)
        with Session(engine) as db:
            j=jobs.create_job(db,'test')
            token=jobs.claim_final_assembly(db,j.id,{'source_hash':'first'})
            with self.assertRaisesRegex(ValueError,'already running'):jobs.claim_final_assembly(db,j.id,{'source_hash':'first'})
            jobs.finish_final_assembly(db,j.id,token,status='done')
            newer=jobs.claim_final_assembly(db,j.id,{'source_hash':'second'})
            self.assertNotEqual(token,newer)
            self.assertFalse(jobs.finish_final_assembly(db,j.id,token,status='failed'))
        engine.dispose()
    def test_real_ffmpeg_mixed_fps_audio_cut_crossfade(self):
        import av,numpy as np
        with tempfile.TemporaryDirectory() as folder:
            paths=[]
            for i,(color,hz,fps) in enumerate([('red',440,25),('blue',880,24),('green',1320,30)]):
                p=Path(folder)/f'{i}.mp4';paths.append(p)
                subprocess.run([assembly.ffmpeg(),'-nostdin','-loglevel','error','-y','-f','lavfi','-i',f'color=c={color}:s=320x180:r={fps}','-f','lavfi','-i',f'sine=frequency={hz}:sample_rate=48000','-t','1.2','-c:v','libx264','-c:a','aac',str(p)],check=True,timeout=60)
            plan=assembly.prepare(self.job,self.result);out=Path(folder)/'out.mp4'
            evidence=assembly.render(paths,out,plan)
            self.assertEqual(evidence['local_attempts'],1)
            with av.open(str(out)) as c:
                audio=np.concatenate([f.to_ndarray()[0] for f in c.decode(audio=0)])
            def energy(t,hz):
                chunk=audio[int(t*48000):int((t+.08)*48000)];spec=np.abs(np.fft.rfft(chunk));freq=np.fft.rfftfreq(len(chunk),1/48000)
                return spec[np.argmin(abs(freq-hz))]
            self.assertGreater(energy(.3,440),energy(.3,880)*10)
            middle=evidence['timeline']['shots'][1]['start']+.2
            self.assertGreater(energy(middle,880),energy(middle,440)*10)
            fade=evidence['timeline']['boundaries'][1]['start']+.25
            self.assertGreater(energy(fade,880),1);self.assertGreater(energy(fade,1320),1)
            self.assertAlmostEqual(evidence['final']['audio_duration'],evidence['timeline']['duration'],delta=.1)

    def test_real_silent_clip_selection_trims_picture_and_sound_together(self):
        self.result['shots']=[{'shot_number':1,'video_url':'https://example.com/1.mp4',
            'video_status':'done','speech_mode':'none','video_edit_range':{'start_sec':0.4,'end_sec':1.4}}]
        self.result['assembly']['transitions']=[]
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'source.mp4';target=Path(folder)/'selected.mp4'
            subprocess.run([assembly.ffmpeg(),'-nostdin','-loglevel','error','-y',
                '-f','lavfi','-i','testsrc2=s=320x180:r=30',
                '-f','lavfi','-i','sine=frequency=440:sample_rate=48000',
                '-t','2','-c:v','libx264','-c:a','aac',str(source)],check=True,timeout=60)
            evidence=assembly.render([source],target,assembly.prepare(self.job,self.result))
            self.assertAlmostEqual(evidence['timeline']['duration'],1.0,delta=.04)
            self.assertAlmostEqual(evidence['final']['video_duration'],1.0,delta=.1)
            self.assertAlmostEqual(evidence['final']['audio_duration'],1.0,delta=.1)

if __name__=='__main__':unittest.main()
