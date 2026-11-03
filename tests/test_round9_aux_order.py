"""Risk preparation must not lose its deadline behind optional decoration."""
import ast
import asyncio
import unittest
from pathlib import Path
from test_work_scheduler import WorkRuntime,Event,provider,until

ROOT=Path(__file__).resolve().parents[1]

def hook_priority(path,method):
    tree=ast.parse(path.read_text('utf-8'))
    for node in ast.walk(tree):
        if isinstance(node,ast.AsyncFunctionDef) and node.name==method:
            for decorator in node.decorator_list:
                if isinstance(decorator,ast.Call) and isinstance(decorator.func,ast.Attribute) and decorator.func.attr=='on_llm_request':
                    return next(ast.literal_eval(kw.value) for kw in decorator.keywords if kw.arg=='priority')
    raise AssertionError('Missing declared hook priority')

class PreparationOrderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.runtime=WorkRuntime({'latency_optimization_enabled':True,'model_concurrency':2}.get,lambda *_:None)
        self.addAsyncCleanup(self.runtime.close)
        self.gates={name:asyncio.Event() for name in ('primary','risk','image','other')}
        self.tasks=[];self.entered=[]

    async def asyncTearDown(self):
        for gate in self.gates.values():gate.set()
        if self.tasks:await asyncio.gather(*self.tasks,return_exceptions=True)

    def start(self,name,kind):
        async def operation():
            self.entered.append(name)
            await self.gates[name].wait()
            return name
        task=asyncio.create_task(self.runtime.scheduler.run(provider(),operation,kind=kind));self.tasks.append(task);return task

    def test_native_hooks_register_risk_before_gallery_preparation(self):
        primary=hook_priority(ROOT/'AstrBot/data/plugins/astrbot_plugin_proactive_agent/main.py','enrich_conversation')
        gallery=hook_priority(ROOT/'AstrBot/data/plugins/astrbot_plugin_smart_imagechat_hub/main.py','on_llm_request')
        self.assertGreater(primary,gallery)

    async def test_primary_and_risk_run_first_and_image_still_completes(self):
        event=Event()
        with self.runtime.reply_work(event):
            with self.runtime.prediction_work(event):risk=self.start('risk','auxiliary')
            await until(lambda:self.runtime.scheduler.status()['waiting']==1)
            with self.runtime.decoration_work(event):image=self.start('image','auxiliary')
            await until(lambda:self.runtime.scheduler.status()['waiting']==2)
            self.assertEqual(self.entered,[])
            primary=self.start('primary','interactive')
            await until(lambda:set(self.entered)=={'primary','risk'})
            self.assertFalse(image.done())
            self.gates['risk'].set();self.assertEqual(await risk,'risk')
            await until(lambda:'image' in self.entered)
            self.assertFalse(primary.done())
            self.gates['primary'].set();self.assertEqual(await primary,'primary')
            self.gates['image'].set();self.assertEqual(await image,'image')
            self.assertEqual(self.entered.count('image'),1)

    async def test_new_reply_does_not_cancel_admitted_image_selection(self):
        event=Event()
        with self.runtime.reply_work(event):
            primary=self.start('primary','interactive');await until(lambda:'primary' in self.entered)
            with self.runtime.decoration_work(event):image=self.start('image','auxiliary')
            await until(lambda:'image' in self.entered)
            with self.runtime.reply_work(Event(target='aiocqhttp:GroupMessage:other',message_id='other')):
                other=self.start('other','interactive')
                await until(lambda:self.runtime.scheduler.status()['waiting']==1)
                self.assertFalse(image.done())
                self.gates['primary'].set();await primary
                await until(lambda:'other' in self.entered)
                self.assertFalse(image.done())
                self.gates['other'].set();await other
            self.gates['image'].set();self.assertEqual(await image,'image')

if __name__=='__main__':unittest.main()
