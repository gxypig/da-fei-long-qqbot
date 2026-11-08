from unittest.mock import patch
from test_fullchain_runtime import Runtime
from test_fullchain_foundation import event
from data.plugins.astrbot_plugin_proactive_agent.fullchain import install_fullchain
from astrbot.core.pipeline.scheduler import PipelineScheduler
from astrbot.core.pipeline.process_stage.stage import ProcessStage
from astrbot.api.message_components import Image

class ReferenceAdmission(Runtime):
    async def test_claimed_supplement_does_not_queue_or_reply_twice(self):
        called=[]
        async def process(stage,e):called.append(e.message_obj.message_id);yield
        async def execute(scheduler,e):
            async for _ in ProcessStage.process(None,e):pass
        with patch.object(ProcessStage,'process',process),patch.object(PipelineScheduler,'execute',execute):
            restore=install_fullchain(self.plugin)
            try:
                for claimed in (True,False):
                    e=event(id_=str(claimed),text='');e.get_messages=lambda:[Image(file='photo.png')]
                    e.is_at_or_wake_command=True;e.stop_event=lambda:None
                    row=self.plugin._incoming_visual.observe(e)
                    if claimed:row['generation_claimed_by']='existing-request'
                    await PipelineScheduler.execute(None,e)
                    job=e.get_extra('qqbot_reply_job')
                    if claimed:
                        self.assertEqual(job.reason,'reference_joined_existing_image_request')
                        self.assertNotIn('reply_queue_ms',job.timings)
                self.assertEqual(called,['False'])
            finally:restore()
