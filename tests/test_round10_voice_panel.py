import asyncio,copy,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock,patch
from quart import Quart
from test_maintenance import Main
from data.plugins.astrbot_plugin_proactive_agent.voice_panel import register_voice_panel,revision
from data.plugins.astrbot_plugin_proactive_agent.fish_voice import FREE,PAID,BASE

class Config(dict):
    def save_config(self):self.saved=copy.deepcopy(dict(self))

class VoicePanel(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.audio=Path(self.temp.name)/'local.wav';self.audio.write_bytes(b'RIFFtest')
        profiles=[{'id':mode,'type':'fishaudio_tts_api','model':model,'api_base':BASE,
                   'api_key':'fixture-secret-key','fishaudio-tts-reference-id':'a'*32,'enable':True}
                  for mode,model in [('free',FREE),('paid',PAID)]]
        profiles.append({'id':'mimo','type':'mimo_tts_api','api_key':'fixture-mimo-secret','enable':True})
        self.config=Config(provider=profiles,provider_tts_settings={'provider_id':'free','enable':True,'trigger_probability':.05},unrelated='keep')
        def instance(p):return SimpleNamespace(provider_config=p,headers={'model':p.get('model')},get_model=lambda:p.get('model'),api_base=BASE,reference_id='a'*32)
        manager=SimpleNamespace(inst_map={p['id']:instance(p) for p in profiles},update_provider=AsyncMock())
        self.app=Quart(__name__)
        def register(path,handler,methods,description):self.app.add_url_rule('/'+path.rsplit('/',1)[-1],view_func=handler,methods=methods)
        self.plugin=SimpleNamespace(_work=SimpleNamespace(busy=lambda:False),context=SimpleNamespace(get_config=lambda:self.config,provider_manager=manager,register_web_api=register))
        register_voice_panel(self.plugin);self.client=self.app.test_client()

    async def post(self,**body):return await self.client.post('/panel_voice_action',json=body)

    async def test_status_never_exposes_keys_and_preserves_probability(self):
        response=await self.client.get('/panel_voice');raw=await response.get_data(as_text=True)
        self.assertNotIn('fixture-secret',raw);self.assertNotIn('fixture-mimo-secret',raw)
        data=(await response.get_json())['data'];self.assertTrue(data['key_configured']);self.assertEqual(data['auto_probability'],.05)

    async def test_paid_needs_explicit_confirmation_and_switch_is_revision_checked(self):
        response=await self.post(action='switch',mode='paid',revision=revision(self.config))
        self.assertEqual(response.status_code,400);self.assertEqual(self.config['provider_tts_settings']['provider_id'],'free')
        self.assertEqual((await self.post(action='switch',mode='mimo',revision='old')).status_code,409)
        response=await self.post(action='switch',mode='paid',confirm_paid=True,revision=revision(self.config))
        self.assertEqual(response.status_code,200);self.assertEqual(self.config['provider_tts_settings']['provider_id'],'paid')
        response=await self.post(action='switch',mode='mimo',revision=revision(self.config))
        self.assertEqual(response.status_code,200);self.assertEqual(self.config.saved['unrelated'],'keep')

    async def test_active_reply_prevents_mid_synthesis_switch(self):
        self.plugin._work.busy=lambda:True
        response=await self.post(action='switch',mode='mimo',revision=revision(self.config))
        self.assertEqual(response.status_code,409);self.assertEqual(self.config['provider_tts_settings']['provider_id'],'free')

    async def test_save_failure_keeps_previous_selection(self):
        with patch.object(self.config,'save_config',side_effect=OSError('private upstream body')):
            response=await self.post(action='switch',mode='mimo',revision=revision(self.config))
        self.assertEqual(response.status_code,503);self.assertEqual(self.config['provider_tts_settings']['provider_id'],'free')
        self.assertNotIn('private upstream',await response.get_data(as_text=True))

    async def test_local_audition_defaults_free_and_a_b_a_cannot_duplicate(self):
        synth=AsyncMock(return_value=(str(self.audio),{'model':FREE,'state':'synthesized','elapsed_ms':1}))
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',synth):
            a=await self.post(action='audition',text='晚安',request_id='request-a')
            b=await self.post(action='audition',text='在呢',request_id='request-b')
            again=await self.post(action='audition',text='晚安',request_id='request-a')
        self.assertEqual(a.status_code,200);self.assertEqual(b.status_code,200);self.assertEqual(again.status_code,409)
        self.assertEqual(synth.await_count,2);self.assertEqual(synth.await_args_list[0].args[0].get_model(),FREE)
        self.assertIn('audio',(await a.get_json())['data']);self.assertEqual(self.config['provider_tts_settings']['provider_id'],'free')

    async def test_error_response_has_reason_without_provider_body_or_secret(self):
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',AsyncMock(side_effect=RuntimeError('401 fixture-secret-key'))):
            response=await self.post(action='audition',request_id='request-c')
        raw=await response.get_data(as_text=True)
        self.assertEqual(response.status_code,503);self.assertIn('authentication',raw);self.assertNotIn('fixture-secret-key',raw)

    async def test_paid_audition_without_confirmation_never_calls_provider(self):
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',AsyncMock()) as synth:
            response=await self.post(action='audition',mode='paid',request_id='request-paid')
        self.assertEqual(response.status_code,400);synth.assert_not_awaited()

    async def test_expression_save_is_partial_versioned_and_rollback_safe(self):
        before=copy.deepcopy(self.config)
        r=await self.post(action='expression',enabled=False,revision=revision(self.config))
        self.assertEqual(r.status_code,200)
        self.assertFalse(self.config['provider_tts_settings']['fish_expression_enabled'])
        self.assertEqual(self.config['provider'],before['provider'])
        self.assertEqual(self.config['provider_tts_settings']['trigger_probability'],.05)
        self.assertEqual((await self.post(action='expression',enabled=True,revision='old')).status_code,409)
        with patch.object(self.config,'save_config',return_value=False):
            r=await self.post(action='expression',enabled=True,revision=revision(self.config))
        self.assertEqual(r.status_code,503)
        self.assertFalse(self.config['provider_tts_settings']['fish_expression_enabled'])

    async def test_preview_is_local_side_effect_free_and_does_not_need_paid_consent(self):
        before=copy.deepcopy(self.config)
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',AsyncMock()) as synth:
            r=await self.post(action='preview',mode='paid',text='晚安')
        self.assertEqual(r.status_code,200);synth.assert_not_awaited()
        d=(await r.get_json())['data'];self.assertEqual(d['preview']['text'],'[soft tone]晚安')
        self.assertEqual(d['network_requests'],0);self.assertEqual(self.config,before)
        self.assertFalse(hasattr(self.plugin,'_voice_recent'))

    async def test_preview_and_audition_respect_unsaved_optout_without_saving(self):
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',AsyncMock(return_value=(str(self.audio),{}))) as synth:
            r=await self.post(action='audition',text='晚安',expression_enabled=False,request_id='expression-off')
        self.assertEqual(r.status_code,200)
        self.assertEqual((await r.get_json())['data']['preview']['text'],'晚安')
        self.assertFalse(synth.call_args.kwargs['expression'])
        self.assertNotIn('fish_expression_enabled',self.config['provider_tts_settings'])

    async def test_expression_bad_input_rejected(self):
        self.assertEqual((await self.post(action='expression',enabled='false',revision=revision(self.config))).status_code,400)
        self.assertEqual((await self.post(action='preview',text='好',instruction=['not text'])).status_code,400)

    async def test_inflight_audition_cannot_be_submitted_twice(self):
        entered=asyncio.Event();finish=asyncio.Event()
        async def synth(*a,**kw):entered.set();await finish.wait();return str(self.audio),{'model':FREE,'state':'synthesized'}
        with patch('data.plugins.astrbot_plugin_proactive_agent.voice_panel.synthesize',synth):
            task=asyncio.create_task(self.post(action='audition',request_id='request-first'))
            await entered.wait()
            try:self.assertEqual((await self.post(action='audition',request_id='request-next')).status_code,409)
            finally:finish.set();await task
