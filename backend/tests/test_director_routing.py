import unittest
import json
from unittest.mock import Mock, patch
import httpx
from app.agents import llm_client as llm, prompts, execution
from app.config import settings


PLAN = {'ad_direction': {'takeaway':'A calm moment', 'visual_approach':'Natural', 'pacing':'Quiet', 'sound_direction':'Silence'}, 'shots':[]}


def response(stop='stop', text=None, status=200):
    if text is None: text=json.dumps(PLAN)
    return Mock(status_code=status, json=Mock(return_value={'id':'audit', 'model':settings.director_model,
        'provider':'Google', 'usage':{'completion_tokens':10},
        'choices':[{'finish_reason':stop,'message':{'content':text}}]}))


class DirectorRoutingTests(unittest.TestCase):
    def setUp(self):
        for name,value in [('open_router_api_key','test-key'),('planning_structured_outputs',False)]:
            p=patch.object(settings,name,value);p.start();self.addCleanup(p.stop)
        self.http=Mock()
        self.http.post.return_value=response()
        p=patch.object(llm.httpx,'Client');self.factory=p.start();self.addCleanup(p.stop)
        self.factory.return_value.__enter__.return_value=self.http

    def test_only_creative_family_changes_provider(self):
        for p in (prompts.CINEMATOGRAPHY_AGENT,prompts.CINEMATOGRAPHY_FIX,
                  prompts.CINEMATOGRAPHY_PATCH,prompts.CINEMATOGRAPHY_TRIM):
            self.assertEqual(llm.model_route(p),('openrouter',settings.director_model))
        for p in (prompts.QA_AGENT,prompts.SHOT_ASSEMBLER,prompts.SHOT_PROMPT_COMPILER,prompts.CONTINUITY_AGENT):
            self.assertEqual(llm.model_route(p),('anthropic',llm.REASONING_MODEL))
        self.assertEqual(llm.model_route(prompts.FORMAT_CLASSIFIER,True),('anthropic',llm.FAST_MODEL))

    def test_director_uses_actual_prompt_and_private_provider_route(self):
        with patch.object(llm,'_client') as anthropic:
            self.assertEqual(llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'approved story'),PLAN)
            anthropic.messages.create.assert_not_called()
        request=self.http.post.call_args.kwargs
        self.assertEqual(request['json']['messages'][1]['content'],'approved story')
        self.assertEqual(request['json']['provider'],{'allow_fallbacks':False,'data_collection':'deny','require_parameters':True})

    def test_truncation_retries_once_with_same_payload_and_remaining_budget(self):
        self.http.post.side_effect=[response('length','partial'),response()]
        observed=[]
        llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story',max_tokens=100,truncation_retry_tokens=200,on_response=observed.append)
        first,second=[c.kwargs['json'] for c in self.http.post.call_args_list]
        self.assertEqual(second,{**first,'max_tokens':200})
        self.assertEqual([m['will_retry'] for m in observed],[True,False])
        self.assertLessEqual(self.factory.call_args_list[1].kwargs['timeout'],self.factory.call_args_list[0].kwargs['timeout'])

    def test_errors_never_silently_use_anthropic_or_leak_provider_body(self):
        for bad in (response('length','SECRET'),response('content_filter','SECRET'),
                    response('stop','SECRET broken'),response(status=429)):
            self.http.post.reset_mock();self.http.post.return_value=bad
            with patch.object(llm,'_client') as anthropic, self.assertRaises((ValueError,RuntimeError)) as caught:
                llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story')
            self.assertNotIn('SECRET',str(caught.exception))
            self.assertEqual(self.http.post.call_count,1)
            anthropic.messages.create.assert_not_called()

    def test_missing_key_and_timeout_are_explicit(self):
        with patch.object(settings,'open_router_api_key',''),self.assertRaisesRegex(ValueError,'OPEN_ROUTER_API_KEY'):
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story')
        self.http.post.assert_not_called()
        self.http.post.side_effect=httpx.ReadTimeout('private request details')
        with self.assertRaisesRegex(TimeoutError,'Please retry'):
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story')
        self.assertEqual(self.http.post.call_count,1)

    def test_optional_schema_is_sent_and_validated(self):
        self.http.post.return_value=response(text='{"shots":[]}')
        with patch.object(settings,'planning_structured_outputs',True),self.assertRaises(ValueError):
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story')
        self.assertEqual(self.http.post.call_args.kwargs['json']['response_format']['type'],'json_schema')

    def test_format_repair_is_once_preserves_content_and_has_safe_diagnostics(self):
        malformed=json.dumps(PLAN).replace('"shots": []', '"shots": [],')
        self.http.post.side_effect=[response(text=malformed),response()]
        events=[]
        self.assertEqual(llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'private original brief',on_response=events.append),PLAN)
        requests=[c.kwargs['json'] for c in self.http.post.call_args_list]
        self.assertEqual(len(requests),2)
        self.assertEqual(requests[1]['messages'][1]['content'],malformed)
        self.assertIn('Repair JSON syntax ONLY',requests[1]['messages'][0]['content'])
        self.assertEqual(requests[0]['response_format'],requests[1]['response_format'])
        self.assertNotIn('private original brief',str(events))
        self.assertNotIn('A calm moment',str(events))
        diagnostic=next(e for e in events if e.get('error_type')=='invalid_json')
        self.assertIsInstance(diagnostic['column'],int)
        self.assertLessEqual(self.factory.call_args_list[1].kwargs['timeout'],self.factory.call_args_list[0].kwargs['timeout'])

    def test_format_repair_cannot_rewrite_content_or_loop(self):
        bad=json.dumps(PLAN).replace('"shots": []', '"shots": [],')
        for second in (response(text=bad), response(text=json.dumps(PLAN).replace('A calm moment','New story'))):
            self.http.post.reset_mock();self.http.post.side_effect=[response(text=bad),second]
            with self.assertRaises(ValueError): llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story')
            self.assertEqual(self.http.post.call_count,2)

    def test_truncation_and_format_recovery_do_not_stack(self):
        bad=json.dumps(PLAN).replace('"shots": []', '"shots": [],')
        self.http.post.side_effect=[response('length','partial'),response(text=bad)]
        with self.assertRaises(ValueError):
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story',max_tokens=100,truncation_retry_tokens=200)
        self.assertEqual(self.http.post.call_count,2)

    def test_expired_deadline_prevents_format_repair(self):
        bad=json.dumps(PLAN).replace('"shots": []', '"shots": [],')
        self.http.post.return_value=response(text=bad)
        events=[]
        with patch.object(llm.time,'monotonic',side_effect=[0,0,0,11]), self.assertRaises(ValueError):
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story',request_timeout=10,on_response=events.append)
        self.assertEqual(self.http.post.call_count,1)
        self.assertFalse(events[-1]['will_retry'])

    def test_schema_failure_is_safe_and_does_not_invent_missing_content(self):
        self.http.post.return_value=response(text='{"shots":[],"private_field":"secret story"}')
        events=[]
        with self.assertRaises(ValueError) as caught:
            llm.call_agent(prompts.CINEMATOGRAPHY_AGENT,'story',on_response=events.append)
        self.assertEqual(self.http.post.call_count,1)
        self.assertEqual(events[-1]['error_type'],'schema_violation')
        self.assertNotIn('secret story',str(events)+str(caught.exception))

    def test_model_changes_invalidate_checkpoint(self):
        from app.services import job_service
        keys=[]
        with patch.object(job_service,'get_agent_checkpoint',return_value=None),patch.object(job_service,'save_agent_checkpoint') as save:
            for model in ('google/test-a','google/test-b'):
                with patch.object(settings,'director_model',model):
                    execution.execute(lambda *a,**k:{'shots':[]},prompts.CINEMATOGRAPHY_AGENT,'same story',{},
                                      (None,'isolated',lambda *args:None,0))
                keys.append(save.call_args.args[2])
        self.assertNotEqual(*keys)
